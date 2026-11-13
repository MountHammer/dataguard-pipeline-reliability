"""Focused tests for CSV ingestion (test plan §5 schema contract tests)."""

import csv

import pytest

from dataguard.generator.generate import PRESETS, generate_dataset, write_dataset
from dataguard.pipeline.ingestion import TRACE_COLUMNS, SourceDataError, load_source_data
from dataguard.schemas import SCHEMAS


@pytest.fixture
def source_dir(tmp_path):
    """A folder holding a freshly generated valid sample dataset."""
    config = PRESETS["sample"]
    write_dataset(generate_dataset(config), config, tmp_path)
    return tmp_path


def rewrite_csv(path, change):
    """Read a CSV as rows, apply ``change(rows)``, and write it back."""
    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    rows = change(rows)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        csv.writer(handle, lineterminator="\n").writerows(rows)


# --- Successful ingestion --------------------------------------------------

def test_successful_ingestion_returns_four_tables(source_dir):
    data = load_source_data(source_dir)
    assert list(data) == ["customers", "products", "orders", "order_items"]
    assert [len(data[name]) for name in data] == [20, 15, 40, 100]


def test_returned_columns_are_schema_columns_plus_trace_columns(source_dir):
    data = load_source_data(source_dir)
    for name, schema in SCHEMAS.items():
        assert list(data[name].columns) == list(schema.column_names) + list(TRACE_COLUMNS)


def test_values_stay_as_strings_and_empty_stays_empty(source_dir):
    customers = load_source_data(source_dir)["customers"]
    assert all(isinstance(v, str) for v in customers["signup_date"])
    assert "" in customers["email"].tolist()            # NULL email kept as "", not NaN
    assert customers["email"].notna().all()


def test_trace_columns(source_dir):
    orders = load_source_data(source_dir)["orders"]
    assert (orders["source_file"] == "orders.csv").all()
    assert orders["source_row_number"].tolist() == list(range(2, 42))   # header is line 1
    assert (orders["ingestion_error"] == "").all()


def test_malformed_value_is_kept_not_converted(source_dir):
    path = source_dir / "order_items.csv"
    rewrite_csv(path, lambda rows: [rows[0], [*rows[1][:3], "abc", *rows[1][4:]], *rows[2:]])
    items = load_source_data(source_dir)["order_items"]
    assert items.loc[0, "quantity"] == "abc"


def test_column_order_is_normalized(source_dir):
    original = load_source_data(source_dir)["products"]
    # Reverse the column order of every line in products.csv.
    rewrite_csv(source_dir / "products.csv", lambda rows: [list(reversed(r)) for r in rows])
    reordered = load_source_data(source_dir)["products"]
    assert list(reordered.columns) == list(original.columns)
    assert reordered.equals(original)


def test_header_only_file_gives_empty_table(source_dir):
    rewrite_csv(source_dir / "customers.csv", lambda rows: rows[:1])
    customers = load_source_data(source_dir)["customers"]
    assert customers.empty
    assert list(customers.columns) == list(SCHEMAS["customers"].column_names) + list(TRACE_COLUMNS)


def test_expected_folder_is_never_read(source_dir):
    (source_dir / "_expected").mkdir()
    (source_dir / "_expected" / "expected_labels.csv").write_text("not,a,source,file\n")
    assert len(load_source_data(source_dir)) == 4


def test_wrong_field_count_is_kept_as_malformed_row(source_dir):
    rewrite_csv(source_dir / "orders.csv", lambda rows: [rows[0], rows[1] + ["extra"], *rows[2:]])
    orders = load_source_data(source_dir)["orders"]
    assert len(orders) == 40                                   # row kept, not dropped
    assert orders.loc[0, "ingestion_error"] == "MALFORMED_ROW: expected 5 fields, found 6"
    assert orders.loc[0, "source_row_number"] == 2
    assert (orders.loc[1:, "ingestion_error"] == "").all()     # other rows unaffected


# --- Batch-level failures --------------------------------------------------

def test_missing_file_raises(source_dir):
    (source_dir / "orders.csv").unlink()
    with pytest.raises(SourceDataError, match="orders.csv: required source file not found"):
        load_source_data(source_dir)


def test_missing_column_raises(source_dir):
    # Drop the last column (net_amount) from every line.
    rewrite_csv(source_dir / "order_items.csv", lambda rows: [r[:-1] for r in rows])
    with pytest.raises(SourceDataError, match=r"missing required columns \['net_amount'\]"):
        load_source_data(source_dir)


def test_unexpected_column_raises(source_dir):
    rewrite_csv(source_dir / "customers.csv", lambda rows: [rows[0] + ["loyalty_points"]] + [r + ["0"] for r in rows[1:]])
    with pytest.raises(SourceDataError, match=r"unexpected columns \['loyalty_points'\]"):
        load_source_data(source_dir)


def test_empty_file_without_header_raises(source_dir):
    (source_dir / "products.csv").write_text("")
    with pytest.raises(SourceDataError, match="no header row"):
        load_source_data(source_dir)


def test_unparseable_file_raises(source_dir):
    # An unclosed quote means row boundaries cannot be established.
    with open(source_dir / "customers.csv", "a", encoding="utf-8", newline="") as handle:
        handle.write('CUST-999999,"Broken name,x@example.com,Pune,VIP,2024-01-01\n')
    with pytest.raises(SourceDataError, match="cannot be parsed"):
        load_source_data(source_dir)
