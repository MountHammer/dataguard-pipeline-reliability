"""Tests for DuckDB warehouse publishing (test plan §10, §11, §12)."""

from decimal import Decimal

import duckdb
import pandas as pd
import pytest

from dataguard.generator.generate import PRESETS, GeneratorConfig, generate_dataset, write_dataset
from dataguard.pipeline.ingestion import SourceDataError, load_source_data
from dataguard.pipeline.validation import calculate_amounts, validate_source_data
from dataguard.warehouse.warehouse import (
    TABLE_NAMES,
    completed_revenue,
    connect,
    initialize_warehouse,
    publish_snapshot,
    publish_warehouse,
    table_row_counts,
    write_quarantine,
)


@pytest.fixture
def source_dir(tmp_path):
    config = PRESETS["sample"]
    write_dataset(generate_dataset(config), config, tmp_path / "source")
    return tmp_path / "source"


@pytest.fixture
def validated(source_dir):
    """(valid, quarantined) for the clean sample."""
    return validate_source_data(load_source_data(source_dir))


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "warehouse.duckdb"


def query(db_path, sql):
    with connect(db_path) as con:
        return con.execute(sql).fetchall()


def snapshot(db_path):
    """Every table's full contents, sorted by its key, for comparisons."""
    keys = {"dim_customer": "customer_key", "dim_product": "product_key", "dim_order": "order_key",
            "dim_date": "date_key", "fact_sales": "sales_key"}
    with connect(db_path) as con:
        return {t: con.execute(f"SELECT * FROM {t} ORDER BY {k}").df() for t, k in keys.items()}


def assert_same_snapshot(first, second):
    for table in first:
        pd.testing.assert_frame_equal(first[table], second[table])


# --- Initialization and schema --------------------------------------------

def test_initialize_creates_five_tables(db_path):
    initialize_warehouse(db_path)
    tables = {row[0] for row in query(db_path, "SELECT table_name FROM information_schema.tables")}
    assert tables == set(TABLE_NAMES)
    assert all(count == 0 for count in table_row_counts(db_path).values())


def test_column_types_follow_data_dictionary(db_path):
    initialize_warehouse(db_path)
    types = dict(((t, c), d) for t, c, d in query(
        db_path, "SELECT table_name, column_name, data_type FROM information_schema.columns"))
    assert types[("dim_customer", "signup_date")] == "DATE"
    assert types[("dim_order", "order_timestamp")] == "TIMESTAMP WITH TIME ZONE"
    assert types[("dim_product", "is_active")] == "BOOLEAN"
    assert types[("dim_product", "list_price")] == "DECIMAL(12,2)"
    assert types[("fact_sales", "quantity")] == "INTEGER"
    assert types[("fact_sales", "discount_pct")] == "DECIMAL(5,4)"
    assert types[("fact_sales", "net_amount")] == "DECIMAL(14,2)"
    assert types[("fact_sales", "sales_key")] == "BIGINT"
    assert types[("dim_date", "date_key")] == "INTEGER"


# --- Loading valid records ----------------------------------------------------

def test_valid_sample_loads_into_every_table(validated, db_path):
    valid, _ = validated
    counts = publish_warehouse(valid, db_path)
    assert counts["dim_customer"] == 20
    assert counts["dim_product"] == 15
    assert counts["dim_order"] == 40
    assert counts["fact_sales"] == 100
    first, last, days = query(db_path, "SELECT MIN(full_date), MAX(full_date), COUNT(*) FROM dim_date")[0]
    assert days == (last - first).days + 1                       # continuous calendar


def test_fact_rows_equal_accepted_order_items(validated, db_path):
    valid, _ = validated
    valid["order_items"] = valid["order_items"].iloc[10:]         # pretend 10 were rejected
    counts = publish_warehouse(valid, db_path)
    assert counts["fact_sales"] == len(valid["order_items"]) == 90


def test_every_fact_key_resolves_to_a_dimension(validated, db_path):
    publish_warehouse(validated[0], db_path)
    orphans = query(db_path, """
        SELECT COUNT(*) FROM fact_sales f
        LEFT JOIN dim_order o    ON o.order_key = f.order_key
        LEFT JOIN dim_customer c ON c.customer_key = f.customer_key
        LEFT JOIN dim_product p  ON p.product_key = f.product_key
        LEFT JOIN dim_date d     ON d.date_key = f.date_key
        WHERE o.order_key IS NULL OR c.customer_key IS NULL OR p.product_key IS NULL OR d.date_key IS NULL
    """)[0][0]
    assert orphans == 0


def test_fact_links_to_the_right_customer_and_date(validated, db_path):
    valid, _ = validated
    publish_warehouse(valid, db_path)
    rows = query(db_path, """
        SELECT f.order_item_id, c.customer_id, strftime(o.order_timestamp, '%Y-%m-%dT%H:%M:%SZ'), d.full_date
        FROM fact_sales f
        JOIN dim_order o ON o.order_key = f.order_key
        JOIN dim_customer c ON c.customer_key = f.customer_key
        JOIN dim_date d ON d.date_key = f.date_key
    """)
    orders = valid["orders"].set_index("order_id")
    items = valid["order_items"].set_index("order_item_id")
    for item_id, customer_id, timestamp, full_date in rows:
        order = orders.loc[items.at[item_id, "order_id"]]
        assert customer_id == order["customer_id"]
        assert timestamp == order["order_timestamp"]
        assert str(full_date) == order["order_timestamp"][:10]   # UTC calendar date


def test_values_are_typed_and_empty_text_becomes_null(validated, db_path):
    valid, _ = validated
    publish_warehouse(valid, db_path)
    empty_emails = (valid["customers"]["email"] == "").sum()
    assert query(db_path, "SELECT COUNT(*) FROM dim_customer WHERE email IS NULL")[0][0] == empty_emails > 0
    assert query(db_path, "SELECT COUNT(*) FROM dim_customer WHERE email = ''")[0][0] == 0
    inactive = (valid["products"]["is_active"] == "false").sum()
    assert query(db_path, "SELECT COUNT(*) FROM dim_product WHERE NOT is_active")[0][0] == inactive


def test_discount_amount_matches_calculate_amounts(validated, db_path):
    publish_warehouse(validated[0], db_path)
    rows = query(db_path, "SELECT quantity, unit_price, discount_pct, gross_amount, discount_amount, net_amount FROM fact_sales")
    for quantity, price, pct, gross, discount, net in rows:
        assert (gross, discount, net) == calculate_amounts(quantity, price, pct)
        assert gross == net + discount


# --- Surrogate keys -----------------------------------------------------------

def test_surrogate_keys_follow_sorted_business_keys(validated, db_path):
    publish_warehouse(validated[0], db_path)
    rows = query(db_path, "SELECT customer_key, customer_id FROM dim_customer ORDER BY customer_key")
    assert [key for key, _ in rows] == list(range(1, 21))
    assert [cid for _, cid in rows] == sorted(cid for _, cid in rows)


def test_surrogate_keys_do_not_depend_on_row_order(validated, tmp_path):
    valid, _ = validated
    shuffled = {name: frame.sample(frac=1, random_state=1).reset_index(drop=True) for name, frame in valid.items()}
    publish_warehouse(valid, tmp_path / "a.duckdb")
    publish_warehouse(shuffled, tmp_path / "b.duckdb")
    assert_same_snapshot(snapshot(tmp_path / "a.duckdb"), snapshot(tmp_path / "b.duckdb"))


# --- Revenue ------------------------------------------------------------------

def test_revenue_includes_only_completed_orders(validated, db_path):
    valid, _ = validated
    publish_warehouse(valid, db_path)
    items = valid["order_items"].merge(valid["orders"][["order_id", "order_status"]], on="order_id")
    completed = items[items["order_status"] == "COMPLETED"]
    assert 0 < len(completed) < len(items)                      # sample has other statuses too

    revenue = completed_revenue(db_path)
    assert revenue["order_items"] == len(completed)
    assert revenue["net_revenue"] == sum(Decimal(v) for v in completed["net_amount"])
    assert revenue["gross_revenue"] == sum(Decimal(v) for v in completed["gross_amount"])


def test_no_completed_orders_means_zero_revenue(validated, db_path):
    valid, _ = validated
    valid["orders"] = valid["orders"].assign(order_status="PENDING")
    publish_warehouse(valid, db_path)
    assert completed_revenue(db_path)["net_revenue"] == 0
    assert table_row_counts(db_path)["fact_sales"] == 100       # pending sales are still facts


# --- Quarantine files -----------------------------------------------------------

def test_quarantine_files_are_written(source_dir, tmp_path):
    data = load_source_data(source_dir)
    data["order_items"].loc[4, "quantity"] = "abc"
    data["customers"].loc[0, "email"] = "broken"
    _, quarantined = validate_source_data(data)

    paths = write_quarantine(quarantined, tmp_path / "quarantine")
    assert sorted(p.name for p in paths.values()) == [
        "customers_quarantine.csv", "order_items_quarantine.csv",
        "orders_quarantine.csv", "products_quarantine.csv"]

    items = pd.read_csv(paths["order_items"], dtype=str, keep_default_na=False)
    bad = items[items["reason_code"] == "INVALID_DATA_TYPE"].iloc[0]
    assert bad["quantity"] == "abc"                             # original value kept
    assert bad["source_file"] == "order_items.csv"
    assert bad["source_row_number"] == "6"
    assert "not a whole number" in bad["reason_detail"]
    assert list(items.columns)[-4:] == ["source_file", "source_row_number", "reason_code", "reason_detail"]

    products = pd.read_csv(paths["products"], dtype=str)        # nothing rejected: header only
    assert products.empty and "reason_code" in products.columns
    assert not list((tmp_path / "quarantine").glob("*.tmp"))


# --- Idempotency and safe refresh -------------------------------------------------

def test_rerun_of_same_snapshot_gives_identical_warehouse(validated, db_path):
    valid, quarantined = validated
    publish_snapshot(valid, quarantined, db_path, db_path.parent / "q")
    first = snapshot(db_path)
    publish_snapshot(valid, quarantined, db_path, db_path.parent / "q")
    second = snapshot(db_path)
    assert table_row_counts(db_path)["fact_sales"] == 100       # not 200
    assert_same_snapshot(first, second)


def test_failed_publish_leaves_previous_warehouse_untouched(validated, db_path, tmp_path):
    valid, quarantined = validated
    quarantine_dir = tmp_path / "q"
    publish_snapshot(valid, quarantined, db_path, quarantine_dir)
    before = snapshot(db_path)
    quarantine_before = (quarantine_dir / "orders_quarantine.csv").read_bytes()

    # Bypass validation with a price that Python accepts but that is too big
    # for DECIMAL(12,2). The fact INSERT fails AFTER the tables were dropped
    # and the dimensions reloaded inside the transaction.
    broken = {name: frame.copy() for name, frame in valid.items()}
    broken["customers"] = broken["customers"].iloc[:-1].assign(customer_name="CHANGED")
    broken["order_items"].loc[0, "unit_price"] = "123456789012.00"
    broken_quarantine = {n: f.assign(reason_code="X") for n, f in quarantined.items()}
    with pytest.raises(duckdb.Error):
        publish_snapshot(broken, broken_quarantine, db_path, quarantine_dir)

    assert_same_snapshot(before, snapshot(db_path))              # warehouse restored
    assert (quarantine_dir / "orders_quarantine.csv").read_bytes() == quarantine_before


def test_missing_dimension_row_fails_instead_of_dropping_facts(validated, db_path):
    valid, _ = validated
    broken = dict(valid)
    broken["products"] = valid["products"].iloc[1:]              # a referenced product vanishes
    with pytest.raises(duckdb.ConstraintException):
        publish_warehouse(broken, db_path)


def test_failed_ingestion_publishes_nothing(validated, source_dir, db_path):
    publish_warehouse(validated[0], db_path)
    before = snapshot(db_path)
    (source_dir / "products.csv").unlink()
    with pytest.raises(SourceDataError):
        valid, quarantined = validate_source_data(load_source_data(source_dir))
        publish_snapshot(valid, quarantined, db_path, db_path.parent / "q")   # never reached
    assert_same_snapshot(before, snapshot(db_path))


def test_empty_snapshot_publishes_empty_tables(tmp_path, db_path):
    config = GeneratorConfig(n_customers=0, n_products=0, n_orders=0, n_order_items=0)
    write_dataset(generate_dataset(config), config, tmp_path / "empty")
    valid, quarantined = validate_source_data(load_source_data(tmp_path / "empty"))
    counts = publish_snapshot(valid, quarantined, db_path, tmp_path / "q")
    assert all(count == 0 for count in counts.values())
    assert completed_revenue(db_path)["net_revenue"] == 0
