"""Publish validated records into a DuckDB star schema (data dictionary §12).

Tables:
    dim_customer, dim_product, dim_order, dim_date   (dimensions)
    fact_sales                                       (one row per order item)

How publishing stays safe (full refresh):
    - This module only receives records AFTER validation has finished,
      so a batch-level ingestion failure never reaches it.
    - All five tables are dropped, recreated and reloaded inside ONE
      DuckDB transaction. If anything fails, ROLLBACK restores the
      previous warehouse exactly; nothing is half-published.
    - Quarantine files are replaced only after the warehouse commit
      succeeds, each through a temporary file and an atomic rename.

Why drop-and-recreate instead of DELETE + INSERT: DuckDB checks foreign
keys against rows deleted earlier in the same transaction, so deleting
and re-inserting the same keys fails. Recreating the tables avoids that.

Idempotency: surrogate keys are ROW_NUMBER() over the sorted business
key, and every run rebuilds the tables from scratch, so loading the
same snapshot twice gives identical tables with no duplicates.

Validated values arrive as text. The SQL below converts them with
CAST, and turns empty strings into NULL with NULLIF (§3.5).
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import duckdb
import pandas as pd

from dataguard.pipeline.validation import calculate_amounts
from dataguard.schemas import SCHEMAS

DEFAULT_DB_PATH = Path("database/dataguard.duckdb")
DEFAULT_QUARANTINE_DIR = Path("data/quarantine")

# Children before parents, so foreign keys allow the drops.
DROP_ORDER = ("fact_sales", "dim_date", "dim_order", "dim_product", "dim_customer")
TABLE_NAMES = ("dim_customer", "dim_product", "dim_order", "dim_date", "fact_sales")

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS dim_customer (
    customer_key      BIGINT PRIMARY KEY,
    customer_id       VARCHAR NOT NULL UNIQUE,
    customer_name     VARCHAR NOT NULL,
    email             VARCHAR,
    city              VARCHAR,
    customer_segment  VARCHAR NOT NULL,
    signup_date       DATE NOT NULL
);

CREATE TABLE IF NOT EXISTS dim_product (
    product_key       BIGINT PRIMARY KEY,
    product_id        VARCHAR NOT NULL UNIQUE,
    product_name      VARCHAR NOT NULL,
    category          VARCHAR NOT NULL,
    brand             VARCHAR,
    list_price        DECIMAL(12,2) NOT NULL,
    is_active         BOOLEAN NOT NULL
);

CREATE TABLE IF NOT EXISTS dim_order (
    order_key         BIGINT PRIMARY KEY,
    order_id          VARCHAR NOT NULL UNIQUE,
    order_timestamp   TIMESTAMPTZ NOT NULL,
    order_status      VARCHAR NOT NULL,
    payment_method    VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS dim_date (
    date_key          INTEGER PRIMARY KEY,       -- YYYYMMDD, e.g. 20241117
    full_date         DATE NOT NULL UNIQUE,
    day_number        INTEGER NOT NULL,          -- day of the month, 1-31
    day_name          VARCHAR NOT NULL,
    month_number      INTEGER NOT NULL,
    month_name        VARCHAR NOT NULL,
    quarter_number    INTEGER NOT NULL,
    year_number       INTEGER NOT NULL,
    is_weekend        BOOLEAN NOT NULL
);

CREATE TABLE IF NOT EXISTS fact_sales (
    sales_key         BIGINT PRIMARY KEY,
    order_item_id     VARCHAR NOT NULL UNIQUE,
    order_key         BIGINT NOT NULL REFERENCES dim_order (order_key),
    customer_key      BIGINT NOT NULL REFERENCES dim_customer (customer_key),
    product_key       BIGINT NOT NULL REFERENCES dim_product (product_key),
    date_key          INTEGER NOT NULL REFERENCES dim_date (date_key),
    quantity          INTEGER NOT NULL,
    unit_price        DECIMAL(12,2) NOT NULL,
    discount_pct      DECIMAL(5,4) NOT NULL,
    gross_amount      DECIMAL(14,2) NOT NULL,
    discount_amount   DECIMAL(14,2) NOT NULL,
    net_amount        DECIMAL(14,2) NOT NULL,
    source_file       VARCHAR NOT NULL,
    source_row_number BIGINT NOT NULL
);
"""

# Each statement reads from the validated DataFrames registered as
# src_customers, src_products, src_orders and src_order_items.
LOAD_SQL = [
    """
    INSERT INTO dim_customer
    SELECT ROW_NUMBER() OVER (ORDER BY customer_id),
           customer_id, customer_name, NULLIF(email, ''), NULLIF(city, ''),
           customer_segment, CAST(signup_date AS DATE)
    FROM src_customers
    """,
    """
    INSERT INTO dim_product
    SELECT ROW_NUMBER() OVER (ORDER BY product_id),
           product_id, product_name, category, NULLIF(brand, ''),
           CAST(list_price AS DECIMAL(12,2)), CAST(is_active AS BOOLEAN)
    FROM src_products
    """,
    """
    INSERT INTO dim_order
    SELECT ROW_NUMBER() OVER (ORDER BY order_id),
           order_id, CAST(order_timestamp AS TIMESTAMPTZ), order_status, payment_method
    FROM src_orders
    """,
    # A continuous calendar from the first to the last order date (UTC).
    """
    INSERT INTO dim_date
    SELECT CAST(strftime(d, '%Y%m%d') AS INTEGER), d,
           dayofmonth(d), dayname(d), month(d), monthname(d),
           quarter(d), year(d), dayofweek(d) IN (0, 6)
    FROM (
        SELECT CAST(day AS DATE) AS d
        FROM (SELECT MIN(CAST(order_timestamp AS DATE)) AS first_day,
                     MAX(CAST(order_timestamp AS DATE)) AS last_day
              FROM dim_order) AS bounds,
             generate_series(bounds.first_day, bounds.last_day, INTERVAL 1 DAY) AS days(day)
        WHERE bounds.first_day IS NOT NULL
    )
    """,
    # LEFT JOINs: if a dimension row were ever missing, its key would be
    # NULL and the NOT NULL constraint would fail the whole load, instead
    # of an inner join silently dropping the fact row.
    """
    INSERT INTO fact_sales
    SELECT ROW_NUMBER() OVER (ORDER BY i.order_item_id),
           i.order_item_id,
           o.order_key,
           c.customer_key,
           p.product_key,
           CAST(strftime(CAST(o.order_timestamp AS DATE), '%Y%m%d') AS INTEGER),
           CAST(i.quantity AS INTEGER),
           CAST(i.unit_price AS DECIMAL(12,2)),
           CAST(i.discount_pct AS DECIMAL(5,4)),
           CAST(i.gross_amount AS DECIMAL(14,2)),
           CAST(i.discount_amount AS DECIMAL(14,2)),
           CAST(i.net_amount AS DECIMAL(14,2)),
           i.source_file,
           CAST(i.source_row_number AS BIGINT)
    FROM src_order_items AS i
    LEFT JOIN dim_order    AS o  ON o.order_id = i.order_id
    LEFT JOIN src_orders   AS so ON so.order_id = i.order_id
    LEFT JOIN dim_customer AS c  ON c.customer_id = so.customer_id
    LEFT JOIN dim_product  AS p  ON p.product_id = i.product_id
    """,
]


# ---------------------------------------------------------------------------
# Connection and setup
# ---------------------------------------------------------------------------


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> duckdb.DuckDBPyConnection:
    """Open (or create) the DuckDB file. Times are handled in UTC."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    con.execute("SET TimeZone = 'UTC'")      # UTC dates for date_key, on any machine
    return con


def initialize_warehouse(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Create the five tables if they do not exist yet. Existing data is kept."""
    with connect(db_path) as con:
        con.execute(CREATE_TABLES_SQL)


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


def add_discount_amount(order_items: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of validated order items with a discount_amount column.

    Uses calculate_amounts() from validation.py so the §7 formula exists
    in exactly one place.
    """
    items = order_items.copy()
    items["discount_amount"] = [
        str(calculate_amounts(int(q), Decimal(price), Decimal(pct))[1])
        for q, price, pct in zip(items["quantity"], items["unit_price"], items["discount_pct"])
    ]
    return items


def publish_warehouse(valid: dict[str, pd.DataFrame], db_path: Path | str = DEFAULT_DB_PATH) -> dict[str, int]:
    """Replace the warehouse contents with ``valid`` in one transaction.

    Args:
        valid: The valid DataFrames returned by validate_source_data().
        db_path: DuckDB file to publish into.

    Returns:
        Row counts per table after the commit.

    Raises:
        Any DuckDB error. The transaction is rolled back first, so the
        previous warehouse contents are left untouched.
    """
    order_items = add_discount_amount(valid["order_items"])

    with connect(db_path) as con:
        con.register("src_customers", valid["customers"])
        con.register("src_products", valid["products"])
        con.register("src_orders", valid["orders"])
        con.register("src_order_items", order_items)

        con.execute("BEGIN TRANSACTION")
        try:
            for table in DROP_ORDER:
                con.execute(f"DROP TABLE IF EXISTS {table}")
            con.execute(CREATE_TABLES_SQL)
            for statement in LOAD_SQL:
                con.execute(statement)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

    return table_row_counts(db_path)


def write_quarantine(quarantined: dict[str, pd.DataFrame],
                     quarantine_dir: Path | str = DEFAULT_QUARANTINE_DIR) -> dict[str, Path]:
    """Write one CSV of rejected rows per entity, e.g. orders_quarantine.csv.

    Each file keeps the original values plus source_file,
    source_row_number, reason_code and reason_detail. A file with only a
    header means nothing was rejected. Each file is written to a
    temporary name first and then renamed, so a crash never leaves a
    half-written file.
    """
    quarantine_dir = Path(quarantine_dir)
    quarantine_dir.mkdir(parents=True, exist_ok=True)

    paths = {}
    for name in SCHEMAS:
        path = quarantine_dir / f"{name}_quarantine.csv"
        temporary = path.with_name(path.name + ".tmp")
        quarantined[name].to_csv(temporary, index=False, lineterminator="\n", encoding="utf-8")
        os.replace(temporary, path)
        paths[name] = path
    return paths


def publish_snapshot(valid: dict[str, pd.DataFrame], quarantined: dict[str, pd.DataFrame],
                     db_path: Path | str = DEFAULT_DB_PATH,
                     quarantine_dir: Path | str = DEFAULT_QUARANTINE_DIR) -> dict[str, int]:
    """Publish the warehouse, then the quarantine files.

    The quarantine files are only replaced after the warehouse commit
    succeeds, so a failed load leaves both outputs as they were.
    """
    counts = publish_warehouse(valid, db_path)
    write_quarantine(quarantined, quarantine_dir)
    return counts


# ---------------------------------------------------------------------------
# Simple read helpers (used by tests and reports)
# ---------------------------------------------------------------------------


def table_row_counts(db_path: Path | str = DEFAULT_DB_PATH) -> dict[str, int]:
    """Number of rows in each warehouse table."""
    with connect(db_path) as con:
        return {table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in TABLE_NAMES}


def completed_revenue(db_path: Path | str = DEFAULT_DB_PATH) -> dict:
    """Revenue from COMPLETED orders only (data dictionary §7).

    Pending and cancelled orders stay in fact_sales (they are real
    transactions) but are excluded from revenue.
    """
    sql = """
        SELECT COUNT(*)                          AS order_items,
               COALESCE(SUM(f.quantity), 0)      AS quantity,
               COALESCE(SUM(f.gross_amount), 0)  AS gross_revenue,
               COALESCE(SUM(f.net_amount), 0)    AS net_revenue
        FROM fact_sales AS f
        JOIN dim_order  AS o ON o.order_key = f.order_key
        WHERE o.order_status = 'COMPLETED'
    """
    with connect(db_path) as con:
        row = con.execute(sql).fetchone()
    return dict(zip(["order_items", "quantity", "gross_revenue", "net_revenue"], row))
