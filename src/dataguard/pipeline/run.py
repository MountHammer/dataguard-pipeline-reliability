"""Run the complete DataGuard pipeline once.

    ingest -> validate -> count and check -> publish -> report

This module only connects the existing stages. It does not contain any
ingestion, validation or warehouse logic of its own:

    load_source_data()       dataguard.pipeline.ingestion
    validate_source_data()   dataguard.pipeline.validation
    publish_snapshot()       dataguard.warehouse.warehouse

Every run, successful or not, produces:
    - one appended line in reports/pipeline_runs.jsonl (run history,
      data dictionary §13; earlier lines are never changed)
    - reports/latest_run_report.md (a readable summary of this run)

Status rules:
    SUCCESS only after the warehouse transaction has committed and the
    quarantine files are written. Any exception makes the run FAILED;
    the error is logged with its traceback and stored in the run record.
    A batch-level ingestion error stops the run before anything is
    published, and a publishing error is rolled back by the warehouse,
    so the previous warehouse stays as it was.

Usage:
    python scripts/run_pipeline.py
    python scripts/run_pipeline.py --source data/raw --database database/big.duckdb
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from dataguard.pipeline.ingestion import load_source_data
from dataguard.pipeline.validation import validate_source_data
from dataguard.schemas import SCHEMAS
from dataguard.warehouse.warehouse import completed_revenue, publish_snapshot

logger = logging.getLogger("dataguard")

DEFAULT_SOURCE_DIR = Path("data/sample")
DEFAULT_DB_PATH = Path("database/dataguard.duckdb")
DEFAULT_QUARANTINE_DIR = Path("data/quarantine")
DEFAULT_REPORTS_DIR = Path("reports")

RUN_HISTORY_FILE = "pipeline_runs.jsonl"
REPORT_FILE = "latest_run_report.md"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_pipeline(source_dir: Path | str = DEFAULT_SOURCE_DIR,
                 db_path: Path | str = DEFAULT_DB_PATH,
                 quarantine_dir: Path | str = DEFAULT_QUARANTINE_DIR,
                 reports_dir: Path | str = DEFAULT_REPORTS_DIR) -> dict:
    """Run the pipeline once and return its run record.

    The returned dict is exactly the line appended to pipeline_runs.jsonl,
    plus ``report_path``. Check ``result["status"]`` - this function
    records failures instead of raising them, so a FAILED run is still
    fully documented.
    """
    reports_dir = Path(reports_dir)
    run_id = str(uuid.uuid4())
    started_at = utc_now()
    timer = time.perf_counter()

    record = new_run_record(run_id, started_at, source_dir, db_path, quarantine_dir)
    stage = "ingestion"
    logger.info("run %s started (source=%s)", run_id, source_dir)

    try:
        # 1. Ingest. A SourceDataError here is batch-level: stop, publish nothing.
        data = load_source_data(source_dir)

        # 2. Validate.
        stage = "validation"
        valid, quarantined = validate_source_data(data)

        # 3. Count and check that every input row is either valid or rejected.
        stage = "quality checks"
        record.update(quality_metrics(data, valid, quarantined))
        check_counts_reconcile(record)

        # 4. Publish. The warehouse commits in one transaction or rolls back.
        stage = "publishing"
        quarantined = {name: frame.assign(run_id=run_id) for name, frame in quarantined.items()}
        warehouse_counts = publish_snapshot(valid, quarantined, db_path, quarantine_dir)
        record["warehouse_published"] = True
        record["warehouse_row_counts"] = warehouse_counts
        record["output_row_count"] = warehouse_counts["fact_sales"]
        record["completed_net_revenue"] = str(completed_revenue(db_path)["net_revenue"])

        stage = "post-publish checks"
        if record["output_row_count"] != record["entity_counts"]["order_items"]["valid"]:
            raise RuntimeError("fact_sales row count does not match accepted order items")

        record["status"] = "SUCCESS"

    except Exception as error:                       # recorded, logged and reported
        record["status"] = "FAILED"
        record["failure_stage"] = stage
        record["error_type"] = type(error).__name__
        record["error_message"] = str(error)
        record["failure_reason"] = f"{stage}: {type(error).__name__}: {error}"
        logger.exception("run %s FAILED during %s", run_id, stage)

    record["ended_at_utc"] = utc_now()
    record["execution_time_seconds"] = round(time.perf_counter() - timer, 3)

    report_path = write_run_outputs(record, reports_dir)
    logger.info("run %s finished with status %s in %.2fs", run_id, record["status"],
                record["execution_time_seconds"])
    return {**record, "report_path": str(report_path)}


# ---------------------------------------------------------------------------
# Run record and metrics
# ---------------------------------------------------------------------------


def utc_now() -> str:
    """Current time as ISO-8601 UTC with a Z suffix, e.g. 2026-11-13T18:20:05Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_run_record(run_id: str, started_at: str, source_dir, db_path, quarantine_dir) -> dict:
    """A run record with every field present, filled in as the run progresses.

    Counts start at 0 so a run that fails early still has a complete,
    honest record (data dictionary §13: counts reflect progress).
    """
    return {
        "run_id": run_id,
        "status": "FAILED",                      # only set to SUCCESS at the very end
        "started_at_utc": started_at,
        "ended_at_utc": None,
        "execution_time_seconds": None,
        "source_dir": str(source_dir),
        "database_path": str(db_path),
        "quarantine_dir": str(quarantine_dir),
        "input_row_count": 0,
        "valid_row_count": 0,
        "rejected_row_count": 0,
        "output_row_count": 0,
        "acceptance_rate_pct": 0.0,
        "rejection_rate_pct": 0.0,
        "entity_counts": {name: {"input": 0, "valid": 0, "rejected": 0} for name in SCHEMAS},
        "rejection_reasons": {},
        "warehouse_published": False,
        "warehouse_row_counts": {},
        "completed_net_revenue": None,
        "failure_stage": None,
        "error_type": None,
        "error_message": None,
        "failure_reason": "",
    }


def quality_metrics(data: dict[str, pd.DataFrame], valid: dict[str, pd.DataFrame],
                    quarantined: dict[str, pd.DataFrame]) -> dict:
    """Counts, acceptance/rejection rates and reason-code totals.

    Rates are percentages rounded to 2 decimals. With no input rows both
    rates are 0.0, so an empty batch never divides by zero.
    """
    entity_counts = {
        name: {"input": len(data[name]), "valid": len(valid[name]), "rejected": len(quarantined[name])}
        for name in SCHEMAS
    }
    total_input = sum(c["input"] for c in entity_counts.values())
    total_valid = sum(c["valid"] for c in entity_counts.values())
    total_rejected = sum(c["rejected"] for c in entity_counts.values())

    # A row can have several codes ("A;B"); count each code once per row.
    reasons: dict[str, int] = {}
    for frame in quarantined.values():
        for codes in frame["reason_code"]:
            for code in codes.split(";"):
                reasons[code] = reasons.get(code, 0) + 1

    return {
        "input_row_count": total_input,
        "valid_row_count": total_valid,
        "rejected_row_count": total_rejected,
        "acceptance_rate_pct": percentage(total_valid, total_input),
        "rejection_rate_pct": percentage(total_rejected, total_input),
        "entity_counts": entity_counts,
        "rejection_reasons": dict(sorted(reasons.items())),
    }


def percentage(part: int, whole: int) -> float:
    """part / whole as a percentage with 2 decimals; 0.0 when whole is 0."""
    return round(100 * part / whole, 2) if whole else 0.0


def check_counts_reconcile(record: dict) -> None:
    """Every input row must end up either valid or rejected - never lost."""
    for name, counts in record["entity_counts"].items():
        if counts["input"] != counts["valid"] + counts["rejected"]:
            raise RuntimeError(f"{name}: {counts['input']} input rows but "
                               f"{counts['valid']} valid + {counts['rejected']} rejected")


# ---------------------------------------------------------------------------
# Outputs: run history (append-only) and a readable report
# ---------------------------------------------------------------------------


def write_run_outputs(record: dict, reports_dir: Path) -> Path:
    """Append the record to the JSONL history and write the Markdown report."""
    reports_dir.mkdir(parents=True, exist_ok=True)

    # "a" = append: earlier runs are never overwritten.
    with open(reports_dir / RUN_HISTORY_FILE, "a", encoding="utf-8", newline="\n") as history:
        history.write(json.dumps(record) + "\n")

    report_path = reports_dir / REPORT_FILE
    report_path.write_text(build_report(record), encoding="utf-8", newline="\n")
    return report_path


def build_report(record: dict) -> str:
    """A short Markdown summary of one run."""
    lines = [
        "# DataGuard pipeline run report",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Run ID | `{record['run_id']}` |",
        f"| Status | **{record['status']}** |",
        f"| Started (UTC) | {record['started_at_utc']} |",
        f"| Ended (UTC) | {record['ended_at_utc']} |",
        f"| Duration | {record['execution_time_seconds']} s |",
        f"| Source directory | `{record['source_dir']}` |",
        f"| Database | `{record['database_path']}` |",
        f"| Warehouse published | {'yes' if record['warehouse_published'] else 'no'} |",
        "",
        "## Record counts",
        "",
        "| Entity | Input | Accepted | Rejected |",
        "|---|---:|---:|---:|",
    ]
    for name, counts in record["entity_counts"].items():
        lines.append(f"| {name} | {counts['input']:,} | {counts['valid']:,} | {counts['rejected']:,} |")
    lines += [
        f"| **Total** | **{record['input_row_count']:,}** | **{record['valid_row_count']:,}** "
        f"| **{record['rejected_row_count']:,}** |",
        "",
        f"Acceptance rate: **{record['acceptance_rate_pct']}%** · "
        f"Rejection rate: **{record['rejection_rate_pct']}%**",
        "",
    ]

    if record["rejection_reasons"]:
        lines += ["## Rejection reasons", "", "| Reason code | Rows |", "|---|---:|"]
        lines += [f"| {code} | {count:,} |" for code, count in record["rejection_reasons"].items()]
        lines.append("")

    if record["warehouse_row_counts"]:
        lines += ["## Warehouse tables", "", "| Table | Rows |", "|---|---:|"]
        lines += [f"| {table} | {count:,} |" for table, count in record["warehouse_row_counts"].items()]
        lines += ["", f"Completed net revenue (INR): **{record['completed_net_revenue']}**", ""]

    if record["status"] == "FAILED":
        lines += ["## Failure", "",
                  f"- Stage: {record['failure_stage']}",
                  f"- Error type: `{record['error_type']}`",
                  f"- Message: {record['error_message']}",
                  "- The previous warehouse contents were left unchanged.", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Run the pipeline from the command line. Exit code 0 = SUCCESS, 1 = FAILED."""
    parser = argparse.ArgumentParser(description="Run the DataGuard pipeline once.")
    parser.add_argument("--source", default=str(DEFAULT_SOURCE_DIR),
                        help=f"folder with the four source CSV files (default: {DEFAULT_SOURCE_DIR})")
    parser.add_argument("--database", default=str(DEFAULT_DB_PATH),
                        help=f"DuckDB warehouse file (default: {DEFAULT_DB_PATH})")
    parser.add_argument("--quarantine", default=str(DEFAULT_QUARANTINE_DIR),
                        help=f"folder for rejected records (default: {DEFAULT_QUARANTINE_DIR})")
    parser.add_argument("--reports", default=str(DEFAULT_REPORTS_DIR),
                        help=f"folder for run history and report (default: {DEFAULT_REPORTS_DIR})")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = run_pipeline(args.source, args.database, args.quarantine, args.reports)

    print()
    print(f"Run ID     : {result['run_id']}")
    print(f"Status     : {result['status']}")
    print(f"Records    : {result['input_row_count']:,} read, {result['valid_row_count']:,} accepted, "
          f"{result['rejected_row_count']:,} rejected ({result['rejection_rate_pct']}%)")
    if result["status"] == "SUCCESS":
        print(f"Fact rows  : {result['output_row_count']:,} published to {result['database_path']}")
    else:
        print(f"Error      : {result['failure_reason']}")
    print(f"Report     : {result['report_path']}")
    print(f"History    : {Path(args.reports) / RUN_HISTORY_FILE}")
    return 0 if result["status"] == "SUCCESS" else 1


if __name__ == "__main__":
    sys.exit(main())
