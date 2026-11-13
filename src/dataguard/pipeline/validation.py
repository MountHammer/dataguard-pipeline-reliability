"""Validate ingested records and split them into valid and quarantined rows.

Input:  the dict of four DataFrames from ingestion.load_source_data()
        (every value is still the original text from the CSV).
Output: two dicts keyed by entity name -
        valid       rows that passed every check (original text kept)
        quarantined rows that failed, with reason_code and reason_detail

The checks, in the order they run for each table:
    1. Malformed row    - ingestion could not map the fields (MALFORMED_ROW)
    2. Field checks     - required values, data types, allowed values,
                          numeric ranges (driven by schemas.py)
    3. Duplicate keys   - data dictionary §8
    4. Table rules      - foreign keys and business rules (§6, §7),
                          run only on rows that passed steps 1-3

Tables are validated parents first. Foreign keys are checked against
the parent rows that were ACCEPTED, so if an order is rejected, its
order items are rejected too (architecture §5).

All problems are row-level: one bad row never stops the run. Missing
files or columns are batch-level problems and are caught earlier, in
ingestion.

Deliberately NOT checked (generator invariants only, §6): an order item
may reference an inactive product, and its unit_price may differ from
the product's current list_price.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

import pandas as pd

from dataguard.schemas import (
    CUSTOMERS,
    ORDER_ITEMS,
    ORDERS,
    PRODUCTS,
    ColumnSpec,
    EntitySchema,
    LogicalType,
)

# ---------------------------------------------------------------------------
# Reason codes written to quarantined rows
# ---------------------------------------------------------------------------

MALFORMED_ROW = "MALFORMED_ROW"                          # wrong number of fields
MISSING_REQUIRED_VALUE = "MISSING_REQUIRED_VALUE"        # empty non-nullable field
INVALID_DATA_TYPE = "INVALID_DATA_TYPE"                  # e.g. "abc" in quantity
INVALID_ALLOWED_VALUE = "INVALID_ALLOWED_VALUE"          # e.g. order_status "SHIPPED"
OUT_OF_RANGE = "OUT_OF_RANGE"                            # e.g. quantity 0, discount 1.01
INVALID_EMAIL = "INVALID_EMAIL"
DUPLICATE_KEY = "DUPLICATE_KEY"                          # identical later copy
CONFLICTING_DUPLICATE_KEY = "CONFLICTING_DUPLICATE_KEY"  # same key, different values
INVALID_FOREIGN_KEY = "INVALID_FOREIGN_KEY"              # parent missing or rejected
ORDER_BEFORE_SIGNUP = "ORDER_BEFORE_SIGNUP"
GROSS_AMOUNT_MISMATCH = "GROSS_AMOUNT_MISMATCH"
NET_AMOUNT_MISMATCH = "NET_AMOUNT_MISMATCH"

TRACE_COLUMNS = ["source_file", "source_row_number"]
REASON_COLUMNS = ["reason_code", "reason_detail"]

CENT = Decimal("0.01")
EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def validate_source_data(data: dict[str, pd.DataFrame]) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    """Validate all four tables, parents first.

    Returns:
        (valid, quarantined) - two dicts of DataFrames keyed by entity name.
    """
    valid, quarantined = {}, {}

    # 1. Customers and products have no parents.
    valid["customers"], quarantined["customers"] = validate_table(
        data["customers"], CUSTOMERS, table_rules=check_customer)
    valid["products"], quarantined["products"] = validate_table(
        data["products"], PRODUCTS)

    # 2. Orders may only reference accepted customers.
    signup_dates = {
        row.customer_id: date.fromisoformat(row.signup_date)
        for row in valid["customers"].itertuples()
    }
    valid["orders"], quarantined["orders"] = validate_table(
        data["orders"], ORDERS,
        table_rules=lambda row: check_order(row, signup_dates))

    # 3. Order items may only reference accepted orders and products.
    order_ids = set(valid["orders"]["order_id"])
    product_ids = set(valid["products"]["product_id"])
    valid["order_items"], quarantined["order_items"] = validate_table(
        data["order_items"], ORDER_ITEMS,
        table_rules=lambda row: check_order_item(row, order_ids, product_ids))

    return valid, quarantined


def validate_table(frame: pd.DataFrame, schema: EntitySchema, table_rules=None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Validate one table and split it into (valid, quarantined).

    Args:
        frame: Ingested table (text values plus trace columns).
        schema: The table's schema from schemas.py.
        table_rules: Optional function(parsed_row) -> list of (code, detail)
            for foreign keys and business rules. It only sees rows that
            passed every earlier check, so all its values are parsed.
    """
    data_columns = list(schema.column_names)
    missing = [c for c in data_columns + TRACE_COLUMNS + ["ingestion_error"] if c not in frame.columns]
    if missing:
        raise ValueError(f"{schema.name}: input is missing columns {missing}; run ingestion first")

    records = frame.to_dict("records")
    problems: list[list[tuple[str, str]]] = [[] for _ in records]
    parsed_rows: list[dict | None] = [None] * len(records)

    # Steps 1 and 2: malformed rows, then field-by-field checks.
    for i, record in enumerate(records):
        if record["ingestion_error"]:
            problems[i].append((MALFORMED_ROW, record["ingestion_error"]))
            continue
        parsed_rows[i], field_problems = check_fields(record, schema)
        problems[i].extend(field_problems)

    # Step 3: duplicate primary keys.
    for i, problem in find_duplicate_keys(records, schema).items():
        problems[i].append(problem)

    # Step 4: foreign keys and business rules, only for otherwise clean rows.
    if table_rules is not None:
        for i, parsed in enumerate(parsed_rows):
            if not problems[i]:
                problems[i].extend(table_rules(parsed))

    # Split into the two outputs, keeping the original text values.
    is_valid = [not p for p in problems]
    output_columns = data_columns + TRACE_COLUMNS
    valid = frame.loc[is_valid, output_columns].reset_index(drop=True)

    rejected = [i for i, ok in enumerate(is_valid) if not ok]
    quarantined = frame.loc[[not ok for ok in is_valid], output_columns].reset_index(drop=True)
    quarantined["reason_code"] = [_join_codes(problems[i]) for i in rejected]
    quarantined["reason_detail"] = ["; ".join(detail for _, detail in problems[i]) for i in rejected]
    return valid, quarantined


def _join_codes(problems: list[tuple[str, str]]) -> str:
    """'CODE_A;CODE_B' - each code once, in the order it was found."""
    return ";".join(dict.fromkeys(code for code, _ in problems))


# ---------------------------------------------------------------------------
# Step 2: field checks (driven entirely by schemas.py)
# ---------------------------------------------------------------------------


def check_fields(record: dict, schema: EntitySchema) -> tuple[dict, list[tuple[str, str]]]:
    """Check every column of one row.

    Returns:
        (parsed, problems) - ``parsed`` maps column name to a Python value
        (Decimal, int, date, datetime, bool, str or None for empty);
        ``problems`` is a list of (reason_code, detail).
    """
    parsed, problems = {}, []
    for column in schema.columns:
        value, problem = check_value(record[column.name], column)
        parsed[column.name] = value
        if problem:
            problems.append(problem)
    return parsed, problems


def check_value(text: str, column: ColumnSpec):
    """Check one field. Returns (parsed_value, problem_or_None)."""
    if text.strip() == "":
        if column.nullable:
            return None, None
        return None, (MISSING_REQUIRED_VALUE, f"{column.name} is required")

    try:
        value = parse_value(text, column)
    except ValueError as error:
        return None, (INVALID_DATA_TYPE, f"{column.name}: {error}")

    if column.allowed_values is not None and value not in column.allowed_values:
        return value, (INVALID_ALLOWED_VALUE, f"{column.name}: {text!r} is not one of {list(column.allowed_values)}")
    if column.numeric_range is not None and not column.numeric_range.contains(value):
        return value, (OUT_OF_RANGE, f"{column.name}: {text} is outside the allowed range")
    return value, None


def parse_value(text: str, column: ColumnSpec):
    """Convert CSV text to a Python value, following data dictionary §3.5.

    Raises ValueError with a readable message when the text is not valid
    for the column's logical type.
    """
    kind = column.logical_type

    if kind is LogicalType.VARCHAR:
        return text

    if kind is LogicalType.INTEGER:
        if not re.fullmatch(r"[+-]?\d+", text):
            raise ValueError(f"{text!r} is not a whole number")
        return int(text)

    if kind is LogicalType.DECIMAL:
        # Plain digits only: rejects "abc", "NaN", "inf", "1e3" and "1,000".
        match = re.fullmatch(r"[+-]?(\d+)(?:\.(\d+))?", text)
        if not match:
            raise ValueError(f"{text!r} is not a decimal number")
        whole, fraction = match.group(1), match.group(2) or ""
        if len(fraction) > column.scale:
            raise ValueError(f"{text!r} has more than {column.scale} decimal places")
        if len(whole.lstrip("0")) > column.precision - column.scale:
            raise ValueError(f"{text!r} is too large for {column.type_label}")
        return Decimal(text)

    if kind is LogicalType.BOOLEAN:
        if text not in ("true", "false"):
            raise ValueError(f"{text!r} is not 'true' or 'false'")
        return text == "true"

    if kind is LogicalType.DATE:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            raise ValueError(f"{text!r} is not a YYYY-MM-DD date")
        return date.fromisoformat(text)          # also rejects 2024-02-30

    if kind is LogicalType.TIMESTAMPTZ:
        try:
            moment = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            raise ValueError(f"{text!r} is not a UTC timestamp like 2024-03-15T09:41:07Z") from None
        return moment.replace(tzinfo=timezone.utc)

    raise ValueError(f"unsupported type {kind}")


# ---------------------------------------------------------------------------
# Step 3: duplicate keys (data dictionary §8)
# ---------------------------------------------------------------------------


def find_duplicate_keys(records: list[dict], schema: EntitySchema) -> dict[int, tuple[str, str]]:
    """Apply the duplicate-key policy.

    - Same key, identical values: keep the first occurrence (lowest
      source_row_number), quarantine the later copies as DUPLICATE_KEY.
    - Same key, different values: quarantine every copy as
      CONFLICTING_DUPLICATE_KEY.

    "First" is decided by source_row_number, never by list position,
    so the result does not depend on the order rows were loaded.

    Returns {record index: (reason_code, detail)} for rows to reject.
    """
    key = schema.primary_key
    rows_by_key: dict[str, list[int]] = {}
    for i, record in enumerate(records):
        if record[key] and not record["ingestion_error"]:
            rows_by_key.setdefault(record[key], []).append(i)

    problems = {}
    for key_value, indexes in rows_by_key.items():
        if len(indexes) == 1:
            continue
        indexes.sort(key=lambda i: records[i]["source_row_number"])
        payloads = {tuple(records[i][c] for c in schema.column_names) for i in indexes}
        lines = [records[i]["source_row_number"] for i in indexes]

        if len(payloads) == 1:                      # identical copies
            first = lines[0]
            for i in indexes[1:]:
                problems[i] = (DUPLICATE_KEY, f"{key} {key_value!r} duplicates line {first}")
        else:                                       # conflicting copies
            for i in indexes:
                problems[i] = (CONFLICTING_DUPLICATE_KEY, f"{key} {key_value!r} has conflicting values on lines {lines}")
    return problems


# ---------------------------------------------------------------------------
# Step 4: table-specific rules (data dictionary §6 and §7)
# ---------------------------------------------------------------------------


def check_customer(row: dict) -> list[tuple[str, str]]:
    """Customer rule: email must be valid when provided."""
    email = row["email"]
    if email is not None and not EMAIL_PATTERN.fullmatch(email):
        return [(INVALID_EMAIL, f"email: {email!r} is not a valid email address")]
    return []


def check_order(row: dict, signup_dates: dict[str, date]) -> list[tuple[str, str]]:
    """Order rules: customer must exist (and be accepted); no order before signup."""
    customer_id = row["customer_id"]
    if customer_id not in signup_dates:
        return [(INVALID_FOREIGN_KEY, f"customer_id {customer_id!r} not found in accepted customers")]
    if row["order_timestamp"].date() < signup_dates[customer_id]:
        return [(ORDER_BEFORE_SIGNUP, f"order on {row['order_timestamp'].date()} is before signup on {signup_dates[customer_id]}")]
    return []


def check_order_item(row: dict, order_ids: set[str], product_ids: set[str]) -> list[tuple[str, str]]:
    """Order-item rules: order and product must exist; amounts must reconcile.

    Product active status and list price are intentionally not checked.
    """
    problems = []
    if row["order_id"] not in order_ids:
        problems.append((INVALID_FOREIGN_KEY, f"order_id {row['order_id']!r} not found in accepted orders"))
    if row["product_id"] not in product_ids:
        problems.append((INVALID_FOREIGN_KEY, f"product_id {row['product_id']!r} not found in accepted products"))

    gross, _, net = calculate_amounts(row["quantity"], row["unit_price"], row["discount_pct"])
    if row["gross_amount"] != gross:
        problems.append((GROSS_AMOUNT_MISMATCH, f"gross_amount {row['gross_amount']} should be {gross}"))
    if row["net_amount"] != net:
        problems.append((NET_AMOUNT_MISMATCH, f"net_amount {row['net_amount']} should be {net}"))
    return problems


def calculate_amounts(quantity: int, unit_price: Decimal, discount_pct: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """The §7 money formulas, using Decimal and ROUND_HALF_UP.

    Returns (gross_amount, discount_amount, net_amount).
    """
    gross = (unit_price * quantity).quantize(CENT, rounding=ROUND_HALF_UP)
    discount = (gross * discount_pct).quantize(CENT, rounding=ROUND_HALF_UP)
    return gross, discount, gross - discount
