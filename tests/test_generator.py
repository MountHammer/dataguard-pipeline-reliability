"""Essential tests for the synthetic data generator (test plan §4.1, GEN001-GEN017)."""

import re
from dataclasses import replace
from decimal import ROUND_HALF_UP, Decimal

import pandas as pd
import pytest

from dataguard.generator.generate import (
    PRESETS,
    GeneratorConfig,
    generate_dataset,
    main,
    write_dataset,
)
from dataguard.schemas import (
    CUSTOMER_SEGMENTS,
    ORDER_STATUSES,
    PAYMENT_METHODS,
    SCHEMAS,
)

SAMPLE = PRESETS["sample"]
# Big enough that ~5-10% null rates and inactive products reliably appear.
MEDIUM = GeneratorConfig(n_customers=500, n_products=100, n_orders=1_000, n_order_items=2_500)


@pytest.fixture(scope="module")
def sample():
    return generate_dataset(SAMPLE)


@pytest.fixture(scope="module")
def medium():
    return generate_dataset(MEDIUM)


def read_raw(folder, entity):
    """Read a written CSV exactly as text (no type conversion, no NaN)."""
    return pd.read_csv(folder / SCHEMAS[entity].file_name, dtype=str, keep_default_na=False)


# --- Structure and counts --------------------------------------------------

def test_gen001_columns_match_schema(sample):
    for name, schema in SCHEMAS.items():
        assert tuple(sample[name].columns) == schema.column_names


def test_gen002_row_counts_match_config(sample):
    assert len(sample["customers"]) == SAMPLE.n_customers
    assert len(sample["products"]) == SAMPLE.n_products
    assert len(sample["orders"]) == SAMPLE.n_orders
    assert len(sample["order_items"]) == SAMPLE.n_order_items


def test_gen003_primary_keys_unique_and_formatted(sample):
    patterns = {"customers": r"CUST-\d{6}", "products": r"PROD-\d{6}",
                "orders": r"ORD-\d{8}", "order_items": r"ITEM-\d{9}"}
    for name, schema in SCHEMAS.items():
        keys = sample[name][schema.primary_key]
        assert keys.is_unique
        assert keys.str.fullmatch(patterns[name]).all()


# --- Relationships ---------------------------------------------------------

def test_gen004_foreign_keys_resolve(sample):
    assert sample["orders"]["customer_id"].isin(sample["customers"]["customer_id"]).all()
    assert sample["order_items"]["order_id"].isin(sample["orders"]["order_id"]).all()
    assert sample["order_items"]["product_id"].isin(sample["products"]["product_id"]).all()


def test_gen005_items_per_order(medium):
    items = medium["order_items"]
    per_order = items.groupby("order_id").size()
    assert len(per_order) == MEDIUM.n_orders                      # no order without items
    assert per_order.between(1, MEDIUM.max_items_per_order).all()
    assert not items.duplicated(["order_id", "product_id"]).any()  # distinct products per order


def test_gen006_orders_not_before_signup(medium):
    merged = medium["orders"].merge(medium["customers"], on="customer_id")
    order_dates = merged["order_timestamp"].map(lambda ts: ts.date())
    assert (order_dates >= merged["signup_date"]).all()


def test_gen007_allowed_values(medium):
    assert medium["customers"]["customer_segment"].isin(CUSTOMER_SEGMENTS).all()
    assert medium["orders"]["order_status"].isin(ORDER_STATUSES).all()
    assert medium["orders"]["payment_method"].isin(PAYMENT_METHODS).all()


# --- Money and generator invariants -----------------------------------------

def test_gen008_financials_and_invariants(medium):
    items = medium["order_items"]
    products = medium["products"].set_index("product_id")
    for row in items.itertuples():
        gross = row.unit_price * row.quantity
        discount = (gross * row.discount_pct).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        assert row.quantity > 0 and row.unit_price > 0
        assert Decimal(0) <= row.discount_pct <= Decimal(1)
        assert row.gross_amount == gross
        assert row.net_amount == gross - discount
        assert row.unit_price == products.at[row.product_id, "list_price"]   # invariant
        assert products.at[row.product_id, "is_active"]                      # invariant
    assert (~products["is_active"]).sum() == MEDIUM.n_inactive_products > 0  # inactive exist


def test_gen008_rounding_is_half_up():
    # 10.10 * 0.05 = 0.505 -> 0.51 with ROUND_HALF_UP (0.50 with half-even).
    assert (Decimal("10.10") * Decimal("0.0500")).quantize(Decimal("0.01"), ROUND_HALF_UP) == Decimal("0.51")


def test_gen009_nullability(medium):
    for name, schema in SCHEMAS.items():
        frame = medium[name]
        for column in schema.required_columns:
            assert frame[column].notna().all(), f"{name}.{column} has missing values"
            assert (frame[column].astype(str) != "").all(), f"{name}.{column} has empty text"
        for column in schema.nullable_columns:
            assert frame[column].isna().any(), f"{name}.{column} never empty"
    emails = medium["customers"]["email"].dropna()
    assert emails.str.fullmatch(r"[a-z0-9.]+@example\.com").all()


# --- Written files ---------------------------------------------------------

def test_gen010_csv_serialization(tmp_path, sample):
    write_dataset(sample, SAMPLE, tmp_path)
    for name, schema in SCHEMAS.items():
        raw_bytes = (tmp_path / schema.file_name).read_bytes()
        assert b"\r" not in raw_bytes                                 # LF only
        raw_bytes.decode("utf-8")                                     # valid UTF-8
        assert raw_bytes.split(b"\n")[0].decode() == ",".join(schema.column_names)

    customers = read_raw(tmp_path, "customers")
    products = read_raw(tmp_path, "products")
    orders = read_raw(tmp_path, "orders")
    items = read_raw(tmp_path, "order_items")
    assert customers["signup_date"].str.fullmatch(r"\d{4}-\d{2}-\d{2}").all()
    assert orders["order_timestamp"].str.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z").all()
    assert products["is_active"].isin(["true", "false"]).all()
    assert products["list_price"].str.fullmatch(r"\d+\.\d{2}").all()
    for column in ["unit_price", "gross_amount", "net_amount"]:
        assert items[column].str.fullmatch(r"\d+\.\d{2}").all()
    assert items["discount_pct"].str.fullmatch(r"[01]\.\d{4}").all()
    assert items["quantity"].str.fullmatch(r"[1-9]\d*").all()
    assert (tmp_path / "generation_metadata.json").exists()


def test_gen011_same_seed_gives_identical_files(tmp_path):
    for folder in ("run1", "run2"):
        write_dataset(generate_dataset(SAMPLE), SAMPLE, tmp_path / folder)
    for name in [s.file_name for s in SCHEMAS.values()] + ["generation_metadata.json"]:
        assert (tmp_path / "run1" / name).read_bytes() == (tmp_path / "run2" / name).read_bytes()


def test_gen012_different_seed_gives_different_data(sample):
    other = generate_dataset(replace(SAMPLE, seed=7))
    assert not other["customers"].equals(sample["customers"])
    assert not other["order_items"].equals(sample["order_items"])


def test_gen013_customers_unchanged_when_item_count_changes(sample):
    more_items = generate_dataset(replace(SAMPLE, n_order_items=150))
    assert more_items["customers"].equals(sample["customers"])
    assert more_items["products"].equals(sample["products"])


# --- Configuration ---------------------------------------------------------

@pytest.mark.parametrize("changes", [
    {"n_customers": -1},
    {"n_orders": 101},                                      # more orders than items
    {"n_order_items": 241},                                 # > 40 orders * 6 max items
    {"max_items_per_order": 0},
    {"n_customers": 0},                                     # orders but no customers
    {"n_products": 1, "inactive_product_ratio": 1.0},      # orders but no active products
    {"n_products": 2, "inactive_product_ratio": 0.0},      # 100 items > 40 orders * 2 products
])
def test_gen014_impossible_configs_rejected(changes):
    with pytest.raises(ValueError):
        replace(SAMPLE, **changes)


def test_gen014_all_zero_counts_write_header_only_files(tmp_path):
    empty = GeneratorConfig(n_customers=0, n_products=0, n_orders=0, n_order_items=0)
    write_dataset(generate_dataset(empty), empty, tmp_path)
    for schema in SCHEMAS.values():
        text = (tmp_path / schema.file_name).read_text(encoding="utf-8")
        assert text == ",".join(schema.column_names) + "\n"


def test_gen015_cli_creates_files_and_refuses_overwrite(tmp_path, capsys):
    out = str(tmp_path / "cli")
    assert main(["--preset", "sample", "--out", out]) == 0
    assert all((tmp_path / "cli" / s.file_name).exists() for s in SCHEMAS.values())
    assert main(["--preset", "sample", "--out", out]) == 1          # refuses without --overwrite
    assert "already contains" in capsys.readouterr().err
    assert main(["--preset", "sample", "--out", out, "--overwrite"]) == 0


# --- Volume and label leakage ----------------------------------------------

@pytest.mark.slow
def test_gen016_large_dataset(tmp_path):
    config = PRESETS["perf-100k"]
    tables = generate_dataset(config)
    assert len(tables["order_items"]) == 100_000
    assert tables["order_items"]["order_item_id"].is_unique
    assert tables["order_items"]["order_id"].isin(tables["orders"]["order_id"]).all()
    assert tables["order_items"]["product_id"].isin(tables["products"]["product_id"]).all()
    write_dataset(tables, config, tmp_path)


def test_gen017_no_label_leakage(tmp_path, sample):
    write_dataset(sample, SAMPLE, tmp_path)
    for name, schema in SCHEMAS.items():
        assert tuple(read_raw(tmp_path, name).columns) == schema.column_names
    assert not (tmp_path / "_expected").exists()
