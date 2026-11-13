"""End-to-end tests for the pipeline runner (test plan §10, §11, §12)."""

import csv
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from dataguard.generator.generate import PRESETS, GeneratorConfig, generate_dataset, write_dataset
from dataguard.pipeline import run as run_module
from dataguard.pipeline.run import main, percentage, run_pipeline
from dataguard.warehouse import warehouse
from dataguard.warehouse.warehouse import connect, table_row_counts

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def paths(tmp_path):
    """Isolated source, database, quarantine and reports locations."""
    config = PRESETS["sample"]
    write_dataset(generate_dataset(config), config, tmp_path / "source")
    return {
        "source_dir": tmp_path / "source",
        "db_path": tmp_path / "db" / "warehouse.duckdb",
        "quarantine_dir": tmp_path / "quarantine",
        "reports_dir": tmp_path / "reports",
    }


def run(paths, **overrides):
    return run_pipeline(**{**paths, **overrides})


def history(paths):
    lines = (paths["reports_dir"] / "pipeline_runs.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def rewrite_csv(path, change):
    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    with open(path, "w", newline="", encoding="utf-8") as handle:
        csv.writer(handle, lineterminator="\n").writerows(change(rows))


def warehouse_snapshot(db_path):
    with connect(db_path) as con:
        return {t: con.execute(f"SELECT * FROM {t} ORDER BY 1").df() for t in warehouse.TABLE_NAMES}


def assert_same_snapshot(first, second):
    for table in first:
        pd.testing.assert_frame_equal(first[table], second[table])


# --- 1. Successful run -------------------------------------------------------

def test_successful_run(paths):
    result = run(paths)
    assert result["status"] == "SUCCESS"
    assert result["warehouse_published"] is True
    assert result["input_row_count"] == 175 == result["valid_row_count"]
    assert result["rejected_row_count"] == 0
    assert result["output_row_count"] == 100
    assert result["warehouse_row_counts"]["fact_sales"] == 100
    assert result["error_type"] is None and result["failure_reason"] == ""
    assert result["started_at_utc"].endswith("Z") and result["ended_at_utc"].endswith("Z")
    assert result["execution_time_seconds"] >= 0
    assert Path(result["report_path"]).read_text(encoding="utf-8").count(result["run_id"]) == 1
    assert history(paths) == [{k: v for k, v in result.items() if k != "report_path"}]


# --- 2 and 3. Run IDs and append-only history -------------------------------

def test_each_run_gets_a_unique_run_id(paths):
    ids = {run(paths)["run_id"] for _ in range(3)}
    assert len(ids) == 3


def test_history_is_appended_not_overwritten(paths):
    first = run(paths)
    first_line = (paths["reports_dir"] / "pipeline_runs.jsonl").read_text(encoding="utf-8")
    second = run(paths)
    text = (paths["reports_dir"] / "pipeline_runs.jsonl").read_text(encoding="utf-8")
    assert text.startswith(first_line)                          # first line untouched
    assert [r["run_id"] for r in history(paths)] == [first["run_id"], second["run_id"]]


# --- 4 and 5. Counts and rates ----------------------------------------------

@pytest.fixture
def paths_with_two_rejects(paths):
    """Sample data with exactly 2 bad rows: a bad quantity and a duplicate product."""
    source = paths["source_dir"]
    rewrite_csv(source / "order_items.csv",
                lambda rows: [rows[0], [*rows[1][:3], "abc", *rows[1][4:]], *rows[2:]])
    rewrite_csv(source / "products.csv", lambda rows: rows + [rows[1]])   # identical copy
    return paths


def test_accepted_and_rejected_counts(paths_with_two_rejects):
    result = run(paths_with_two_rejects)
    assert result["status"] == "SUCCESS"
    assert result["entity_counts"] == {
        "customers": {"input": 20, "valid": 20, "rejected": 0},
        "products": {"input": 16, "valid": 15, "rejected": 1},
        "orders": {"input": 40, "valid": 40, "rejected": 0},
        "order_items": {"input": 100, "valid": 99, "rejected": 1},
    }
    assert (result["input_row_count"], result["valid_row_count"], result["rejected_row_count"]) == (176, 174, 2)
    assert result["rejection_reasons"] == {"DUPLICATE_KEY": 1, "INVALID_DATA_TYPE": 1}
    assert result["output_row_count"] == 99


def test_acceptance_and_rejection_rates(paths_with_two_rejects):
    result = run(paths_with_two_rejects)
    assert result["acceptance_rate_pct"] == 98.86                  # 174 / 176
    assert result["rejection_rate_pct"] == 1.14                    # 2 / 176
    assert percentage(0, 0) == 0.0                                 # no division by zero


def test_quarantine_records_carry_run_id_and_source(paths_with_two_rejects):
    result = run(paths_with_two_rejects)
    items = pd.read_csv(paths_with_two_rejects["quarantine_dir"] / "order_items_quarantine.csv",
                        dtype=str, keep_default_na=False)
    row = items.iloc[0]
    assert row["run_id"] == result["run_id"]
    assert (row["source_file"], row["source_row_number"], row["quantity"]) == ("order_items.csv", "2", "abc")
    assert row["reason_code"] == "INVALID_DATA_TYPE"


# --- 6. Empty input ------------------------------------------------------------

def test_empty_input(tmp_path, paths):
    empty = GeneratorConfig(n_customers=0, n_products=0, n_orders=0, n_order_items=0)
    write_dataset(generate_dataset(empty), empty, tmp_path / "empty")
    result = run(paths, source_dir=tmp_path / "empty")
    assert result["status"] == "SUCCESS"
    assert result["input_row_count"] == result["output_row_count"] == 0
    assert result["acceptance_rate_pct"] == result["rejection_rate_pct"] == 0.0
    assert all(count == 0 for count in table_row_counts(paths["db_path"]).values())


# --- 7 and 8. Ingestion failures -------------------------------------------------

def test_missing_source_file_gives_failed_run_with_metadata(paths):
    (paths["source_dir"] / "orders.csv").unlink()
    result = run(paths)
    assert result["status"] == "FAILED"
    assert result["failure_stage"] == "ingestion"
    assert result["error_type"] == "SourceDataError"
    assert "orders.csv" in result["error_message"]
    assert result["warehouse_published"] is False
    assert result["output_row_count"] == 0
    assert history(paths)[-1]["status"] == "FAILED"                 # failure is recorded
    assert "## Failure" in Path(result["report_path"]).read_text(encoding="utf-8")


def test_failed_ingestion_does_not_touch_existing_warehouse(paths):
    assert run(paths)["status"] == "SUCCESS"
    before = warehouse_snapshot(paths["db_path"])
    quarantine_before = (paths["quarantine_dir"] / "orders_quarantine.csv").read_bytes()

    rewrite_csv(paths["source_dir"] / "customers.csv", lambda rows: [rows[0] + ["surprise"]] + rows[1:])
    result = run(paths)

    assert result["status"] == "FAILED" and result["error_type"] == "SourceDataError"
    assert_same_snapshot(before, warehouse_snapshot(paths["db_path"]))
    assert (paths["quarantine_dir"] / "orders_quarantine.csv").read_bytes() == quarantine_before


# --- 9. Publishing failure ---------------------------------------------------

def test_failed_publishing_gives_failed_metadata_and_keeps_warehouse(paths, monkeypatch):
    assert run(paths)["status"] == "SUCCESS"
    before = warehouse_snapshot(paths["db_path"])

    # Make the LAST load statement fail, after the tables were dropped and
    # the dimensions reloaded inside the warehouse transaction.
    monkeypatch.setattr(warehouse, "LOAD_SQL", warehouse.LOAD_SQL + ["SELECT CAST('x' AS INTEGER)"])
    result = run(paths)

    assert result["status"] == "FAILED"
    assert result["failure_stage"] == "publishing"
    assert result["error_type"] == "ConversionException"
    assert result["warehouse_published"] is False
    assert result["output_row_count"] == 0
    assert result["input_row_count"] == 175                     # progress before the failure
    assert history(paths)[-1]["run_id"] == result["run_id"]
    assert_same_snapshot(before, warehouse_snapshot(paths["db_path"]))


def test_unexpected_error_is_logged_not_swallowed(paths, monkeypatch, caplog):
    def broken_validation(data):
        raise RuntimeError("validator crashed")
    monkeypatch.setattr(run_module, "validate_source_data", broken_validation)
    result = run(paths)
    assert result["status"] == "FAILED" and result["failure_stage"] == "validation"
    assert result["failure_reason"] == "validation: RuntimeError: validator crashed"
    assert "validator crashed" in caplog.text                   # traceback logged


# --- 10. Reruns ------------------------------------------------------------------

def test_repeated_runs_do_not_duplicate_warehouse_records(paths):
    run(paths)
    first = warehouse_snapshot(paths["db_path"])
    second_result = run(paths)
    assert second_result["output_row_count"] == 100              # not 200
    assert_same_snapshot(first, warehouse_snapshot(paths["db_path"]))


# --- 11. Command line --------------------------------------------------------------

def test_cli_main_runs_the_pipeline(paths, capsys):
    exit_code = main(["--source", str(paths["source_dir"]), "--database", str(paths["db_path"]),
                      "--quarantine", str(paths["quarantine_dir"]), "--reports", str(paths["reports_dir"])])
    assert exit_code == 0
    assert "Status     : SUCCESS" in capsys.readouterr().out


def test_cli_script_runs_and_reports_failure_with_exit_code(paths):
    args = [sys.executable, str(PROJECT_ROOT / "scripts" / "run_pipeline.py"),
            "--source", str(paths["source_dir"]), "--database", str(paths["db_path"]),
            "--quarantine", str(paths["quarantine_dir"]), "--reports", str(paths["reports_dir"])]
    ok = subprocess.run(args, capture_output=True, text=True, cwd=PROJECT_ROOT)
    assert ok.returncode == 0, ok.stderr
    assert "Status     : SUCCESS" in ok.stdout

    (paths["source_dir"] / "products.csv").unlink()
    failed = subprocess.run(args, capture_output=True, text=True, cwd=PROJECT_ROOT)
    assert failed.returncode == 1
    assert "Status     : FAILED" in failed.stdout
    assert [r["status"] for r in history(paths)] == ["SUCCESS", "FAILED"]
