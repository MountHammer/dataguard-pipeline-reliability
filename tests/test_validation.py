"""Tests for validation and quarantine (test plan §4, §6, §7, §8, §9)."""

import csv
from decimal import Decimal

import pandas as pd
import pytest

from dataguard.generator.generate import PRESETS, generate_dataset, write_dataset
from dataguard.pipeline.ingestion import load_source_data
from dataguard.pipeline.validation import calculate_amounts, validate_source_data, validate_table
from dataguard.schemas import ORDERS, SCHEMAS


@pytest.fixture
def source_dir(tmp_path):
    config = PRESETS["sample"]
    write_dataset(generate_dataset(config), config, tmp_path)
    return tmp_path


@pytest.fixture
def data(source_dir):
    """Freshly ingested, valid sample data (all values are text)."""
    return load_source_data(source_dir)


def reasons(quarantined, entity):
    """All reason codes for an entity, as a flat list."""
    return [code for value in quarantined[entity]["reason_code"] for code in value.split(";")]


def set_item(data, row, **values):
    """Change one or more fields of one order item."""
    items = data["order_items"]
    for column, value in values.items():
        items.loc[row, column] = value


def set_item_amounts(data, row, quantity, unit_price, discount_pct, gross, net):
    set_item(data, row, quantity=quantity, unit_price=unit_price,
             discount_pct=discount_pct, gross_amount=gross, net_amount=net)


# --- Valid data ------------------------------------------------------------

def test_valid_sample_passes_completely(data):
    valid, quarantined = validate_source_data(data)
    assert {name: len(valid[name]) for name in valid} == {
        "customers": 20, "products": 15, "orders": 40, "order_items": 100}
    assert all(q.empty for q in quarantined.values())


def test_valid_rows_keep_original_text_and_trace_columns(data):
    valid, _ = validate_source_data(data)
    for name, schema in SCHEMAS.items():
        assert list(valid[name].columns) == list(schema.column_names) + ["source_file", "source_row_number"]
    first = data["order_items"].iloc[0]
    assert valid["order_items"].loc[0, "unit_price"] == first["unit_price"]   # still text, unchanged


def test_every_quarantined_row_has_a_reason_code(data):
    data["customers"].loc[0, "customer_name"] = ""
    data["products"].loc[0, "list_price"] = "abc"
    _, quarantined = validate_source_data(data)
    for frame in quarantined.values():
        assert (frame["reason_code"] != "").all()


# --- Required values and data types ----------------------------------------

def test_missing_required_value_rejected(data):
    data["customers"].loc[0, "customer_name"] = ""
    valid, quarantined = validate_source_data(data)
    assert reasons(quarantined, "customers") == ["MISSING_REQUIRED_VALUE"]
    assert len(valid["customers"]) == 19


def test_empty_nullable_value_accepted(data):
    data["customers"].loc[0, "city"] = ""
    data["products"].loc[0, "brand"] = ""
    _, quarantined = validate_source_data(data)
    assert quarantined["customers"].empty and quarantined["products"].empty


@pytest.mark.parametrize("entity, column, bad_value", [
    ("products", "list_price", "abc"),                     # UT015 malformed numeric
    ("products", "list_price", "NaN"),                     # UT017 non-finite
    ("products", "list_price", "inf"),
    ("products", "list_price", "12.345"),                  # too many decimals
    ("products", "is_active", "yes"),
    ("customers", "signup_date", "2024-02-30"),            # impossible date
    ("customers", "signup_date", "15/03/2024"),
    ("orders", "order_timestamp", "2024-03-15 09:41:07"),  # no T / Z
    ("order_items", "quantity", "1.5"),
])
def test_invalid_data_type_rejected(data, entity, column, bad_value):
    data[entity].loc[0, column] = bad_value
    _, quarantined = validate_source_data(data)
    assert "INVALID_DATA_TYPE" in reasons(quarantined, entity)


@pytest.mark.parametrize("entity, column, bad_value", [
    ("customers", "customer_segment", "GOLD"),
    ("orders", "order_status", "SHIPPED"),
    ("orders", "payment_method", "CASH"),
])
def test_value_outside_allowed_set_rejected(data, entity, column, bad_value):
    data[entity].loc[0, column] = bad_value
    _, quarantined = validate_source_data(data)
    assert reasons(quarantined, entity) == ["INVALID_ALLOWED_VALUE"]


def test_invalid_email_rejected(data):
    data["customers"].loc[0, "email"] = "not-an-email"
    _, quarantined = validate_source_data(data)
    assert reasons(quarantined, "customers") == ["INVALID_EMAIL"]


# --- Numeric ranges and money ------------------------------------------------

@pytest.mark.parametrize("column, bad_value", [
    ("quantity", "0"), ("quantity", "-5"),                 # UT008, UT009
    ("discount_pct", "-0.0100"), ("discount_pct", "1.0100"),  # UT013, UT014
    ("unit_price", "0.00"),
])
def test_out_of_range_order_item_values_rejected(data, column, bad_value):
    set_item(data, 0, **{column: bad_value})
    _, quarantined = validate_source_data(data)
    assert "OUT_OF_RANGE" in reasons(quarantined, "order_items")


def test_zero_list_price_rejected(data):
    data["products"].loc[0, "list_price"] = "0.00"          # UT005
    _, quarantined = validate_source_data(data)
    assert "OUT_OF_RANGE" in reasons(quarantined, "products")


def test_zero_and_full_discount_accepted(data):
    set_item_amounts(data, 0, "2", "100.00", "0.0000", "200.00", "200.00")   # BR005
    set_item_amounts(data, 1, "2", "100.00", "1.0000", "200.00", "0.00")     # BR006
    _, quarantined = validate_source_data(data)
    assert quarantined["order_items"].empty


def test_rounding_is_half_up(data):
    # Data dictionary §7 example: 10.10 * 0.05 = 0.505 -> discount 0.51, net 9.59.
    set_item_amounts(data, 0, "1", "10.10", "0.0500", "10.10", "9.59")
    set_item_amounts(data, 1, "1", "10.10", "0.0500", "10.10", "9.60")       # half-even answer
    _, quarantined = validate_source_data(data)
    assert quarantined["order_items"]["source_row_number"].tolist() == [3]
    assert reasons(quarantined, "order_items") == ["NET_AMOUNT_MISMATCH"]


def test_gross_and_net_mismatch_rejected(data):
    set_item_amounts(data, 0, "2", "100.00", "0.1000", "201.00", "180.00")   # BR002
    set_item_amounts(data, 1, "2", "100.00", "0.1000", "200.00", "181.00")   # BR004
    _, quarantined = validate_source_data(data)
    assert reasons(quarantined, "order_items") == ["GROSS_AMOUNT_MISMATCH", "NET_AMOUNT_MISMATCH"]


def test_calculate_amounts():
    # 3 x 24091.29 = 72273.87; 20% = 14454.774 -> 14454.77; net 57819.10
    assert calculate_amounts(3, Decimal("24091.29"), Decimal("0.2000")) == (
        Decimal("72273.87"), Decimal("14454.77"), Decimal("57819.10"))


# --- Foreign keys and business rules ----------------------------------------

def test_invalid_customer_reference_rejected(data):
    data["orders"].loc[0, "customer_id"] = "CUST-999999"    # FK002
    _, quarantined = validate_source_data(data)
    assert reasons(quarantined, "orders") == ["INVALID_FOREIGN_KEY"]


def test_invalid_order_and_product_references_rejected(data):
    set_item(data, 0, order_id="ORD-99999999")              # FK006
    set_item(data, 1, product_id="PROD-999999")             # FK004
    _, quarantined = validate_source_data(data)
    assert reasons(quarantined, "order_items") == ["INVALID_FOREIGN_KEY", "INVALID_FOREIGN_KEY"]


def test_rejected_order_cascades_to_its_items(data):
    order_id = data["orders"].loc[0, "order_id"]
    data["orders"].loc[0, "order_status"] = "SHIPPED"
    valid, quarantined = validate_source_data(data)
    item_count = (data["order_items"]["order_id"] == order_id).sum()
    assert len(quarantined["order_items"]) == item_count > 0
    assert set(reasons(quarantined, "order_items")) == {"INVALID_FOREIGN_KEY"}
    assert order_id not in set(valid["order_items"]["order_id"])


def test_order_before_signup_rejected(data):
    customer_id = data["orders"].loc[0, "customer_id"]
    customers = data["customers"]
    customers.loc[customers["customer_id"] == customer_id, "signup_date"] = "2025-12-31"
    data["orders"].loc[0, "order_timestamp"] = "2025-12-30T23:59:59Z"       # FK007
    _, quarantined = validate_source_data(data)
    assert "ORDER_BEFORE_SIGNUP" in reasons(quarantined, "orders")


def test_inactive_product_and_different_price_are_accepted(data):
    # Data dictionary §6: generator invariants, not validation rules.
    product_id = data["order_items"].loc[0, "product_id"]
    products = data["products"]
    products.loc[products["product_id"] == product_id, "is_active"] = "false"          # FK009
    products.loc[products["product_id"] == product_id, "list_price"] = "1.00"          # BR011
    _, quarantined = validate_source_data(data)
    assert all(q.empty for q in quarantined.values())


# --- Duplicate keys (data dictionary §8) -------------------------------------

def add_copy(frame, row, line_number, **changes):
    copy = frame.iloc[[row]].assign(source_row_number=line_number, **changes)
    return pd.concat([frame, copy], ignore_index=True)


def test_identical_duplicate_keeps_first_occurrence(data):
    data["customers"] = add_copy(data["customers"], 0, line_number=22)        # DU002
    valid, quarantined = validate_source_data(data)
    assert len(valid["customers"]) == 20
    assert valid["customers"].loc[0, "source_row_number"] == 2                 # first kept
    assert quarantined["customers"]["source_row_number"].tolist() == [22]
    assert reasons(quarantined, "customers") == ["DUPLICATE_KEY"]


def test_conflicting_duplicate_quarantines_all_copies(data):
    customer_id = data["customers"].loc[0, "customer_id"]
    data["customers"] = add_copy(data["customers"], 0, line_number=22, city="Somewhere Else")  # DU003
    valid, quarantined = validate_source_data(data)
    assert customer_id not in set(valid["customers"]["customer_id"])
    assert sorted(quarantined["customers"]["source_row_number"]) == [2, 22]
    assert set(reasons(quarantined, "customers")) == {"CONFLICTING_DUPLICATE_KEY"}


def test_first_occurrence_is_decided_by_line_number_not_load_order(data):
    # The copy is placed FIRST in the DataFrame but has the later line number.
    orders = data["orders"]
    copy = orders.iloc[[0]].assign(source_row_number=99)
    data["orders"] = pd.concat([copy, orders], ignore_index=True)
    _, quarantined = validate_source_data(data)
    assert quarantined["orders"]["source_row_number"].tolist() == [99]


# --- Malformed rows and traceability ---------------------------------------

def test_malformed_ingestion_row_is_quarantined(source_dir):
    path = source_dir / "orders.csv"
    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    rows[3] = rows[3] + ["extra field"]                                         # line 4
    with open(path, "w", newline="", encoding="utf-8") as handle:
        csv.writer(handle, lineterminator="\n").writerows(rows)

    _, quarantined = validate_source_data(load_source_data(source_dir))
    orders_q = quarantined["orders"]
    assert reasons(quarantined, "orders") == ["MALFORMED_ROW"]
    assert orders_q.loc[0, "source_row_number"] == 4
    assert "expected 5 fields, found 6" in orders_q.loc[0, "reason_detail"]


def test_quarantined_rows_keep_original_values_and_source(data):
    item_id = data["order_items"].loc[5, "order_item_id"]
    set_item(data, 5, quantity="abc")
    valid, quarantined = validate_source_data(data)
    row = quarantined["order_items"].iloc[0]
    assert row["order_item_id"] == item_id
    assert row["quantity"] == "abc"                            # original text preserved
    assert row["source_file"] == "order_items.csv"
    assert row["source_row_number"] == 7                       # DataFrame row 5 = line 7
    assert "quantity: 'abc' is not a whole number" in row["reason_detail"]
    assert len(valid["order_items"]) == 99                     # the other rows carry on


def test_input_without_expected_columns_is_rejected():
    with pytest.raises(ValueError, match="run ingestion first"):
        validate_table(pd.DataFrame({"order_id": ["ORD-00000001"]}), ORDERS)
