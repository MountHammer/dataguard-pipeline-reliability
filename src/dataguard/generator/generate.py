"""Synthetic retail data generator for DataGuard.

Creates four related CSV files - customers, products, orders and
order_items - that follow docs/data_dictionary.md (§3 columns,
§3.5 CSV format, §16 generation rules).

How it works, in four steps:
    1. GeneratorConfig holds the row counts and seed, and rejects
       impossible combinations before anything is generated.
    2. One function per table builds a pandas DataFrame. Parents are
       built first, so children can only pick IDs that already exist.
    3. format_for_csv() turns Python values into the exact text forms
       of §3.5 (dates, `Z` timestamps, true/false, fixed decimals).
    4. write_dataset() writes the CSVs plus generation_metadata.json.

Reproducibility: each table gets its own NumPy random generator built
from (seed, table number), and customers get a Faker instance seeded
with the same seed. Nothing depends on the current date or time.
Same seed + same config + pinned library versions = identical files.

Run from the project root:
    python scripts/generate_data.py --preset sample --out data/sample
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import re
import sys
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import faker
import numpy as np
import pandas as pd

from dataguard.schemas import (
    CUSTOMER_SEGMENTS,
    CUSTOMERS,
    ORDER_ITEMS,
    ORDER_STATUSES,
    ORDERS,
    PAYMENT_METHODS,
    PRODUCTS,
    SCHEMAS,
    EntitySchema,
    LogicalType,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GeneratorConfig:
    """Settings for one generated dataset.

    The config checks itself when it is created (data dictionary §16.3)
    and raises ValueError for impossible combinations, for example more
    orders than order items (every order needs at least one item).
    """

    seed: int = 42
    n_customers: int = 20
    n_products: int = 15
    n_orders: int = 40
    n_order_items: int = 100
    max_items_per_order: int = 6
    inactive_product_ratio: float = 0.10
    start_date: date = date(2024, 1, 1)
    end_date: date = date(2025, 12, 31)

    @property
    def n_inactive_products(self) -> int:
        # Decimal(str(...)) avoids float surprises such as 0.29 * 100 = 28.999...
        return math.floor(Decimal(str(self.inactive_product_ratio)) * self.n_products)

    @property
    def n_active_products(self) -> int:
        return self.n_products - self.n_inactive_products

    def __post_init__(self) -> None:
        counts = {
            "n_customers": self.n_customers,
            "n_products": self.n_products,
            "n_orders": self.n_orders,
            "n_order_items": self.n_order_items,
        }
        for name, value in counts.items():
            if value < 0:
                raise ValueError(f"{name} must not be negative (got {value})")
        if self.max_items_per_order < 1:
            raise ValueError("max_items_per_order must be at least 1")
        if not 0 <= self.inactive_product_ratio <= 1:
            raise ValueError("inactive_product_ratio must be between 0 and 1")
        if self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        if self.n_orders > self.n_order_items:
            raise ValueError("n_orders cannot exceed n_order_items: every order needs at least one item")
        if self.n_order_items > self.n_orders * self.max_items_per_order:
            raise ValueError("n_order_items exceeds n_orders * max_items_per_order")
        if self.n_order_items > self.n_orders * self.n_active_products:
            raise ValueError("n_order_items exceeds n_orders * active products (products are distinct within an order)")
        if self.n_orders > 0 and self.n_customers == 0:
            raise ValueError("orders need at least one customer")
        if self.n_orders > 0 and self.n_active_products == 0:
            raise ValueError("orders need at least one active product")


# Named sizes from data dictionary §16.5.
PRESETS: dict[str, GeneratorConfig] = {
    "sample": GeneratorConfig(n_customers=20, n_products=15, n_orders=40, n_order_items=100),
    "perf-10k": GeneratorConfig(n_customers=1_000, n_products=200, n_orders=4_000, n_order_items=10_000),
    "perf-100k": GeneratorConfig(n_customers=5_000, n_products=500, n_orders=40_000, n_order_items=100_000),
    "perf-250k": GeneratorConfig(n_customers=10_000, n_products=800, n_orders=100_000, n_order_items=250_000),
}

# ---------------------------------------------------------------------------
# Fixed vocabularies and probabilities (kept here so they are easy to read)
# ---------------------------------------------------------------------------

SEGMENT_WEIGHTS = [0.70, 0.22, 0.08]                 # STANDARD, PREMIUM, VIP
STATUS_WEIGHTS = [0.75, 0.15, 0.10]                  # COMPLETED, PENDING, CANCELLED
PAYMENT_WEIGHTS = [0.35, 0.25, 0.15, 0.10, 0.15]     # UPI, CREDIT, DEBIT, NET_BANKING, COD

PRODUCT_NOUNS = {
    "Electronics": ["Headphones", "Charger", "Speaker", "Keyboard", "Mouse"],
    "Home": ["Lamp", "Cushion", "Kettle", "Water Bottle", "Clock"],
    "Grocery": ["Basmati Rice", "Green Tea", "Honey", "Almonds", "Coffee"],
    "Apparel": ["T-Shirt", "Kurta", "Jacket", "Sneakers", "Cap"],
    "Sports": ["Yoga Mat", "Cricket Bat", "Football", "Dumbbell", "Skipping Rope"],
    "Books": ["Notebook", "Novel", "Cookbook", "Atlas", "Diary"],
}
PRODUCT_ADJECTIVES = ["Classic", "Smart", "Eco", "Premium", "Compact", "Everyday", "Pro", "Lite"]
BRANDS = ["Aarav", "Nexa", "Kiran", "Vastra", "Orbit", "Tejas", "Sutra", "Zenith"]

DISCOUNT_OPTIONS = ["0.0000", "0.0500", "0.1000", "0.1500", "0.2000", "0.2500", "0.5000", "1.0000"]
DISCOUNT_WEIGHTS = [0.40, 0.15, 0.15, 0.10, 0.08, 0.07, 0.04, 0.01]

EMAIL_NULL_RATE = 0.10
CITY_NULL_RATE = 0.05
BRAND_NULL_RATE = 0.10

CENT = Decimal("0.01")

# ---------------------------------------------------------------------------
# Table generators (parents first)
# ---------------------------------------------------------------------------


def _rng(config: GeneratorConfig, table_number: int) -> np.random.Generator:
    """Random generator for one table, built from (seed, table number).

    Each table has its own generator, so changing the number of order
    items does not change the customers that are generated.
    """
    return np.random.default_rng([config.seed, table_number])


def _email_from_name(name: str, number: int) -> str:
    """Build a unique, valid email like 'aryan.maharaj.000001@example.com'.

    example.com is reserved for examples, so no real person is emailed.
    """
    local = re.sub(r"[^a-z0-9]+", ".", name.lower()).strip(".") or "customer"
    return f"{local}.{number:06d}@example.com"


def generate_customers(config: GeneratorConfig) -> pd.DataFrame:
    """Build the customers table (data dictionary §3.1)."""
    rng = _rng(config, 1)
    fake = faker.Faker("en_IN")
    fake.seed_instance(config.seed)
    n = config.n_customers

    numbers = range(1, n + 1)
    names = [fake.name() for _ in numbers]
    cities = [fake.city() for _ in numbers]
    emails = [_email_from_name(name, i) for name, i in zip(names, numbers)]

    # Blank out some optional values so the nullable columns are exercised.
    email_missing = rng.random(n) < EMAIL_NULL_RATE
    city_missing = rng.random(n) < CITY_NULL_RATE

    total_days = (config.end_date - config.start_date).days
    signup_offsets = rng.integers(0, total_days + 1, size=n)

    return pd.DataFrame({
        "customer_id": [f"CUST-{i:06d}" for i in numbers],
        "customer_name": names,
        "email": [None if missing else e for e, missing in zip(emails, email_missing)],
        "city": [None if missing else c for c, missing in zip(cities, city_missing)],
        "customer_segment": rng.choice(CUSTOMER_SEGMENTS, size=n, p=SEGMENT_WEIGHTS).tolist(),
        "signup_date": [config.start_date + timedelta(days=int(d)) for d in signup_offsets],
    }, columns=CUSTOMERS.column_names)


def generate_products(config: GeneratorConfig) -> pd.DataFrame:
    """Build the products table (data dictionary §3.2).

    Prices are drawn as whole paise and converted to Decimal, so they
    are exact. A fixed number of products is marked inactive.
    """
    rng = _rng(config, 2)
    n = config.n_products

    categories = rng.choice(list(PRODUCT_NOUNS), size=n).tolist()
    names = [
        f"{rng.choice(PRODUCT_ADJECTIVES)} {rng.choice(PRODUCT_NOUNS[category])}"
        for category in categories
    ]
    brands = rng.choice(BRANDS, size=n).tolist()
    brand_missing = rng.random(n) < BRAND_NULL_RATE
    price_paise = rng.integers(4_900, 4_999_901, size=n)        # Rs 49.00 to Rs 49,999.00

    is_active = [True] * n
    for index in rng.choice(n, size=config.n_inactive_products, replace=False):
        is_active[int(index)] = False

    return pd.DataFrame({
        "product_id": [f"PROD-{i:06d}" for i in range(1, n + 1)],
        "product_name": names,
        "category": categories,
        "brand": [None if missing else b for b, missing in zip(brands, brand_missing)],
        "list_price": [Decimal(int(p)).scaleb(-2) for p in price_paise],
        "is_active": is_active,
    }, columns=PRODUCTS.column_names)


def generate_orders(config: GeneratorConfig, customers: pd.DataFrame) -> pd.DataFrame:
    """Build the orders table (data dictionary §3.3).

    Each order picks an existing customer, and its timestamp is placed
    between that customer's signup date and the end of the date window,
    so no order can predate its customer's signup.
    """
    rng = _rng(config, 3)
    n = config.n_orders

    all_customer_ids = customers["customer_id"].tolist()
    all_signup_dates = customers["signup_date"].tolist()
    customer_rows = rng.integers(0, len(all_customer_ids), size=n) if n else []
    end_of_window = datetime.combine(config.end_date + timedelta(days=1), time(0), tzinfo=timezone.utc)

    customer_ids, timestamps = [], []
    for row in customer_rows:
        signup_start = datetime.combine(all_signup_dates[row], time(0), tzinfo=timezone.utc)
        window_seconds = int((end_of_window - signup_start).total_seconds())
        customer_ids.append(all_customer_ids[row])
        timestamps.append(signup_start + timedelta(seconds=int(rng.integers(0, window_seconds))))

    return pd.DataFrame({
        "order_id": [f"ORD-{i:08d}" for i in range(1, n + 1)],
        "customer_id": customer_ids,
        "order_timestamp": timestamps,
        "order_status": rng.choice(ORDER_STATUSES, size=n, p=STATUS_WEIGHTS).tolist(),
        "payment_method": rng.choice(PAYMENT_METHODS, size=n, p=PAYMENT_WEIGHTS).tolist(),
    }, columns=ORDERS.column_names)


def _items_per_order(config: GeneratorConfig, rng: np.random.Generator) -> np.ndarray:
    """Decide how many lines each order gets.

    Every order starts with one line. The remaining lines are handed
    out in rounds to randomly chosen orders that are still below the
    cap, so the total is exactly n_order_items.
    """
    counts = np.ones(config.n_orders, dtype=int)
    cap = min(config.max_items_per_order, config.n_active_products)
    remaining = config.n_order_items - config.n_orders
    while remaining > 0:
        open_orders = np.flatnonzero(counts < cap)
        chosen = rng.choice(open_orders, size=min(remaining, len(open_orders)), replace=False)
        counts[chosen] += 1
        remaining -= len(chosen)
    return counts


def generate_order_items(config: GeneratorConfig, orders: pd.DataFrame, products: pd.DataFrame) -> pd.DataFrame:
    """Build the order_items table (data dictionary §3.4 and §7).

    - Each line belongs to an existing order and an existing ACTIVE product.
    - Products are distinct within one order.
    - unit_price equals the product's list_price (generator invariant).
    - Money uses Decimal with ROUND_HALF_UP:
        gross    = quantity * unit_price
        discount = round_half_up(gross * discount_pct, 2)
        net      = gross - discount
    """
    rng = _rng(config, 4)
    active = products[products["is_active"].astype(bool)]
    active_ids = active["product_id"].tolist()
    price_by_product = dict(zip(active["product_id"], active["list_price"]))

    order_ids, product_ids = [], []
    for order_id, count in zip(orders["order_id"], _items_per_order(config, rng)):
        for index in rng.choice(len(active_ids), size=int(count), replace=False):
            order_ids.append(order_id)
            product_ids.append(active_ids[int(index)])

    n = len(order_ids)
    quantities = rng.integers(1, 6, size=n).tolist()            # 1 to 5 units
    discounts = [Decimal(d) for d in rng.choice(DISCOUNT_OPTIONS, size=n, p=DISCOUNT_WEIGHTS)]

    unit_prices, gross_amounts, net_amounts = [], [], []
    for product_id, quantity, discount_pct in zip(product_ids, quantities, discounts):
        unit_price = price_by_product[product_id]
        gross = unit_price * quantity
        discount_amount = (gross * discount_pct).quantize(CENT, rounding=ROUND_HALF_UP)
        unit_prices.append(unit_price)
        gross_amounts.append(gross)
        net_amounts.append(gross - discount_amount)

    return pd.DataFrame({
        "order_item_id": [f"ITEM-{i:09d}" for i in range(1, n + 1)],
        "order_id": order_ids,
        "product_id": product_ids,
        "quantity": quantities,
        "unit_price": unit_prices,
        "discount_pct": discounts,
        "gross_amount": gross_amounts,
        "net_amount": net_amounts,
    }, columns=ORDER_ITEMS.column_names)


def generate_dataset(config: GeneratorConfig) -> dict[str, pd.DataFrame]:
    """Generate all four tables, parents first. Keys are entity names."""
    customers = generate_customers(config)
    products = generate_products(config)
    orders = generate_orders(config, customers)
    order_items = generate_order_items(config, orders, products)
    return {"customers": customers, "products": products, "orders": orders, "order_items": order_items}


# ---------------------------------------------------------------------------
# CSV formatting and writing
# ---------------------------------------------------------------------------


def _format_value(value, logical_type: LogicalType, scale: int | None) -> str:
    """Turn one Python value into its CSV text form (data dictionary §3.5)."""
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return ""                                         # NULL -> empty field
    if logical_type is LogicalType.DATE:
        return value.isoformat()                          # 2024-03-15
    if logical_type is LogicalType.TIMESTAMPTZ:
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")      # 2024-03-15T09:41:07Z
    if logical_type is LogicalType.BOOLEAN:
        return "true" if value else "false"
    if logical_type is LogicalType.DECIMAL:
        return f"{Decimal(value):.{scale}f}"              # 1499.00 / 0.1000
    return str(value)                                     # VARCHAR, INTEGER


def format_for_csv(frame: pd.DataFrame, schema: EntitySchema) -> pd.DataFrame:
    """Return a copy of ``frame`` where every value is CSV text.

    The text form of each column is chosen from its logical type in
    schemas.py, so formatting rules live in one place.
    """
    formatted = {
        column.name: [_format_value(v, column.logical_type, column.scale) for v in frame[column.name]]
        for column in schema.columns
    }
    return pd.DataFrame(formatted, columns=schema.column_names, dtype=object)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_dataset(tables: dict[str, pd.DataFrame], config: GeneratorConfig,
                  out_dir: Path | str, overwrite: bool = False) -> dict[str, Path]:
    """Write the four CSVs and generation_metadata.json into ``out_dir``.

    Files are UTF-8 with LF line endings on every operating system.
    Existing CSVs are only replaced when ``overwrite`` is True.

    Returns a mapping of entity name to written CSV path.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    paths = {name: out_dir / schema.file_name for name, schema in SCHEMAS.items()}
    existing = [p.name for p in paths.values() if p.exists()]
    if existing and not overwrite:
        raise FileExistsError(f"{out_dir} already contains {', '.join(existing)}; use overwrite=True")

    for name, schema in SCHEMAS.items():
        text_frame = format_for_csv(tables[name], schema)
        text_frame.to_csv(paths[name], index=False, lineterminator="\n", encoding="utf-8")

    # No "generated at" time is stored: it would change on every run and
    # stop the metadata file itself from being reproducible.
    metadata = {
        "generator": "dataguard.generator.generate",
        "config": {key: (value.isoformat() if isinstance(value, date) else value)
                   for key, value in asdict(config).items()},
        "row_counts": {name: len(tables[name]) for name in SCHEMAS},
        "files": {paths[name].name: {"sha256": _sha256(paths[name])} for name in SCHEMAS},
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "pandas": pd.__version__,
            "faker": faker.VERSION,
        },
    }
    with open(out_dir / "generation_metadata.json", "w", encoding="utf-8", newline="\n") as handle:
        json.dump(metadata, handle, indent=2)
        handle.write("\n")

    return paths


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point. Returns 0 on success, 1 on error."""
    parser = argparse.ArgumentParser(description="Generate DataGuard synthetic retail CSV files.")
    parser.add_argument("--preset", choices=PRESETS, default="sample", help="named dataset size (default: sample)")
    parser.add_argument("--out", default="data/sample", help="output directory (default: data/sample)")
    parser.add_argument("--seed", type=int, help="random seed (default: 42)")
    parser.add_argument("--customers", type=int, dest="n_customers", help="override customer count")
    parser.add_argument("--products", type=int, dest="n_products", help="override product count")
    parser.add_argument("--orders", type=int, dest="n_orders", help="override order count")
    parser.add_argument("--order-items", type=int, dest="n_order_items", help="override order-item count")
    parser.add_argument("--overwrite", action="store_true", help="replace existing CSV files")
    args = parser.parse_args(argv)

    overrides = {key: value for key, value in vars(args).items()
                 if key in {"seed", "n_customers", "n_products", "n_orders", "n_order_items"} and value is not None}
    try:
        config = replace(PRESETS[args.preset], **overrides)   # replace() re-runs the config checks
        paths = write_dataset(generate_dataset(config), config, args.out, overwrite=args.overwrite)
    except (ValueError, FileExistsError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    for name, path in paths.items():
        print(f"{name:<12} {sum(1 for _ in open(path, encoding='utf-8')) - 1:>8,} rows  {path}")
    print(f"metadata     {Path(args.out) / 'generation_metadata.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
