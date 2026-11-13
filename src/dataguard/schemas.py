"""Centralized source-data schemas for DataGuard.

This module is the single place in the code that describes the four
source CSV files: their columns, column order, logical data types,
nullability, primary keys, foreign keys, allowed values and numeric
ranges.

Source of truth: docs/data_dictionary.md, sections 3.1 to 3.4.

Other modules (the generator now, the validator and warehouse later)
import these definitions instead of re-typing column names, so the
code cannot quietly drift away from the documented data contract.

This module only *describes* data. It does not read, write, generate
or validate any data.

Example:
    >>> from dataguard.schemas import ORDERS
    >>> ORDERS.column_names
    ('order_id', 'customer_id', 'order_timestamp', 'order_status', 'payment_method')
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Mapping


# ---------------------------------------------------------------------------
# Logical data types
# ---------------------------------------------------------------------------


class LogicalType(str, Enum):
    """The kind of value a column holds, as named in the data dictionary.

    "Logical" means the meaning of the value, not how it is stored.
    In the CSV files every value is text; data dictionary §3.5 defines
    how each logical type is written as text (for example, a DATE is
    written as ``2024-03-15``).
    """

    VARCHAR = "VARCHAR"          # free text
    DATE = "DATE"                # calendar date, written YYYY-MM-DD
    TIMESTAMPTZ = "TIMESTAMPTZ"  # date and time in UTC, written with a Z suffix
    BOOLEAN = "BOOLEAN"          # true / false
    INTEGER = "INTEGER"          # whole number
    DECIMAL = "DECIMAL"          # exact fixed-point number (used for money)


# ---------------------------------------------------------------------------
# Allowed values (closed sets from the data dictionary)
# ---------------------------------------------------------------------------
# Tuples keep a fixed, documented order, which keeps any output that
# lists them deterministic.

CUSTOMER_SEGMENTS: tuple[str, ...] = ("STANDARD", "PREMIUM", "VIP")
ORDER_STATUSES: tuple[str, ...] = ("COMPLETED", "PENDING", "CANCELLED")
PAYMENT_METHODS: tuple[str, ...] = (
    "UPI",
    "CREDIT_CARD",
    "DEBIT_CARD",
    "NET_BANKING",
    "COD",
)


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NumericRange:
    """Lower and/or upper limits for a numeric column.

    ``None`` means "no limit on this side". The ``*_inclusive`` flags say
    whether the limit itself is allowed:

    - ``NumericRange(minimum=Decimal("0"), min_inclusive=False)`` means
      "greater than zero" (0 itself is not allowed).
    - ``NumericRange(Decimal("0"), Decimal("1"))`` means "between 0 and 1,
      including both 0 and 1".

    Limits are Decimals so money and percentages are compared exactly,
    never as binary floating-point numbers.
    """

    minimum: Decimal | None = None
    maximum: Decimal | None = None
    min_inclusive: bool = True
    max_inclusive: bool = True

    def contains(self, value: Decimal | int) -> bool:
        """Return True if ``value`` lies inside this range."""
        number = Decimal(value)
        if self.minimum is not None:
            if number < self.minimum or (not self.min_inclusive and number == self.minimum):
                return False
        if self.maximum is not None:
            if number > self.maximum or (not self.max_inclusive and number == self.maximum):
                return False
        return True


@dataclass(frozen=True)
class ColumnSpec:
    """Everything the data dictionary says about one column.

    Attributes:
        name: Column name exactly as it appears in the CSV header.
        logical_type: The kind of value the column holds.
        nullable: True if the column may be empty. For a non-nullable
            text column, an empty string also counts as missing
            (data dictionary §3.5).
        precision: Total number of digits, for DECIMAL columns only.
        scale: Digits after the decimal point, for DECIMAL columns only.
        allowed_values: Closed set of permitted values, or None if any
            value of the right type is allowed.
        numeric_range: Permitted numeric range, or None if unrestricted.
    """

    name: str
    logical_type: LogicalType
    nullable: bool
    precision: int | None = None
    scale: int | None = None
    allowed_values: tuple[str, ...] | None = None
    numeric_range: NumericRange | None = None

    @property
    def type_label(self) -> str:
        """The type as written in the data dictionary, e.g. 'DECIMAL(12,2)'."""
        if self.logical_type is LogicalType.DECIMAL:
            return f"DECIMAL({self.precision},{self.scale})"
        return self.logical_type.value


@dataclass(frozen=True)
class ForeignKey:
    """A link from a column in one entity to the primary key of another.

    Example: ``orders.customer_id -> customers.customer_id`` means every
    customer_id in orders must exist in customers.
    """

    column: str
    references_entity: str
    references_column: str

    def __str__(self) -> str:
        return f"{self.column} -> {self.references_entity}.{self.references_column}"


@dataclass(frozen=True)
class EntitySchema:
    """The complete contract for one source CSV file.

    Attributes:
        name: Entity name, e.g. "customers".
        file_name: Expected source file name, e.g. "customers.csv".
        columns: Column specs in the exact expected header order.
        primary_key: Column that uniquely identifies each row.
        foreign_keys: Links from this entity to its parent entities.
    """

    name: str
    file_name: str
    columns: tuple[ColumnSpec, ...]
    primary_key: str
    foreign_keys: tuple[ForeignKey, ...] = ()

    @property
    def column_names(self) -> tuple[str, ...]:
        """Column names in the exact expected header order."""
        return tuple(column.name for column in self.columns)

    @property
    def required_columns(self) -> tuple[str, ...]:
        """Names of columns that must always have a value."""
        return tuple(column.name for column in self.columns if not column.nullable)

    @property
    def nullable_columns(self) -> tuple[str, ...]:
        """Names of columns that may be empty."""
        return tuple(column.name for column in self.columns if column.nullable)

    def column(self, name: str) -> ColumnSpec:
        """Return the spec for one column by name.

        Raises:
            KeyError: if the entity has no column with that name.
        """
        for column in self.columns:
            if column.name == name:
                return column
        raise KeyError(f"{self.name} has no column named {name!r}")


# ---------------------------------------------------------------------------
# Small helpers to keep the entity definitions short and readable
# ---------------------------------------------------------------------------


def _text(name: str, *, nullable: bool = False, allowed: tuple[str, ...] | None = None) -> ColumnSpec:
    return ColumnSpec(name, LogicalType.VARCHAR, nullable, allowed_values=allowed)


def _decimal(name: str, precision: int, scale: int, numeric_range: NumericRange | None = None) -> ColumnSpec:
    return ColumnSpec(
        name, LogicalType.DECIMAL, False,
        precision=precision, scale=scale, numeric_range=numeric_range,
    )


# Reusable numeric ranges.
GREATER_THAN_ZERO = NumericRange(minimum=Decimal("0"), min_inclusive=False)
ZERO_TO_ONE_INCLUSIVE = NumericRange(minimum=Decimal("0"), maximum=Decimal("1"))


# ---------------------------------------------------------------------------
# Entity schemas (data dictionary §3.1 - §3.4)
# ---------------------------------------------------------------------------

# §3.1 Customers: one row per customer.
CUSTOMERS = EntitySchema(
    name="customers",
    file_name="customers.csv",
    columns=(
        _text("customer_id"),
        _text("customer_name"),
        _text("email", nullable=True),  # format check ("valid email") belongs to the validator
        _text("city", nullable=True),
        _text("customer_segment", allowed=CUSTOMER_SEGMENTS),
        ColumnSpec("signup_date", LogicalType.DATE, nullable=False),
    ),
    primary_key="customer_id",
)

# §3.2 Products: one row per product.
PRODUCTS = EntitySchema(
    name="products",
    file_name="products.csv",
    columns=(
        _text("product_id"),
        _text("product_name"),
        _text("category"),
        _text("brand", nullable=True),
        _decimal("list_price", 12, 2, GREATER_THAN_ZERO),
        ColumnSpec("is_active", LogicalType.BOOLEAN, nullable=False),
    ),
    primary_key="product_id",
)

# §3.3 Orders: one row per order.
ORDERS = EntitySchema(
    name="orders",
    file_name="orders.csv",
    columns=(
        _text("order_id"),
        _text("customer_id"),
        ColumnSpec("order_timestamp", LogicalType.TIMESTAMPTZ, nullable=False),
        _text("order_status", allowed=ORDER_STATUSES),
        _text("payment_method", allowed=PAYMENT_METHODS),
    ),
    primary_key="order_id",
    foreign_keys=(ForeignKey("customer_id", "customers", "customer_id"),),
)

# §3.4 Order items: one row per product line in an order.
ORDER_ITEMS = EntitySchema(
    name="order_items",
    file_name="order_items.csv",
    columns=(
        _text("order_item_id"),
        _text("order_id"),
        _text("product_id"),
        ColumnSpec("quantity", LogicalType.INTEGER, nullable=False, numeric_range=GREATER_THAN_ZERO),
        _decimal("unit_price", 12, 2, GREATER_THAN_ZERO),
        _decimal("discount_pct", 5, 4, ZERO_TO_ONE_INCLUSIVE),
        # gross_amount and net_amount have no simple range in the data
        # dictionary; they must reconcile with the §7 calculations, which
        # is a business rule checked by the validator, not a schema rule.
        _decimal("gross_amount", 14, 2),
        _decimal("net_amount", 14, 2),
    ),
    primary_key="order_item_id",
    foreign_keys=(
        ForeignKey("order_id", "orders", "order_id"),
        ForeignKey("product_id", "products", "product_id"),
    ),
)


# ---------------------------------------------------------------------------
# Registry: look up any schema by entity name
# ---------------------------------------------------------------------------

#: All source schemas, keyed by entity name, in dependency order
#: (parents before children). Read-only so other modules cannot change it.
SCHEMAS: Mapping[str, EntitySchema] = MappingProxyType({
    schema.name: schema for schema in (CUSTOMERS, PRODUCTS, ORDERS, ORDER_ITEMS)
})

#: Entity names in dependency order: generate and validate in this order.
ENTITY_ORDER: tuple[str, ...] = tuple(SCHEMAS)

#: The exact source file names the pipeline expects.
SOURCE_FILE_NAMES: tuple[str, ...] = tuple(schema.file_name for schema in SCHEMAS.values())


def get_schema(entity: str) -> EntitySchema:
    """Return the schema for an entity name such as "orders".

    Raises:
        KeyError: if the entity name is unknown.
    """
    try:
        return SCHEMAS[entity]
    except KeyError:
        known = ", ".join(SCHEMAS)
        raise KeyError(f"Unknown entity {entity!r}. Known entities: {known}") from None


# ---------------------------------------------------------------------------
# Self-check: catch typing mistakes in the definitions above at import time
# ---------------------------------------------------------------------------


def _check_definitions() -> None:
    """Fail fast if the definitions above are internally inconsistent.

    This checks the schema *definitions*, not any data.
    """
    for position, schema in enumerate(SCHEMAS.values()):
        names = schema.column_names
        if len(set(names)) != len(names):
            raise ValueError(f"{schema.name}: duplicate column names")
        if schema.column(schema.primary_key).nullable:
            raise ValueError(f"{schema.name}: primary key must not be nullable")
        for column in schema.columns:
            is_decimal = column.logical_type is LogicalType.DECIMAL
            if is_decimal != (column.precision is not None and column.scale is not None):
                raise ValueError(f"{schema.name}.{column.name}: precision/scale only for DECIMAL")
        for fk in schema.foreign_keys:
            if schema.column(fk.column).nullable:
                raise ValueError(f"{schema.name}: foreign key {fk} must not be nullable")
            parent = SCHEMAS.get(fk.references_entity)
            if parent is None or parent.primary_key != fk.references_column:
                raise ValueError(f"{schema.name}: {fk} must reference a primary key")
            # Parents must appear earlier in dependency order.
            if ENTITY_ORDER.index(parent.name) >= position:
                raise ValueError(f"{schema.name}: parent {parent.name} must come first")


_check_definitions()


__all__ = [
    "CUSTOMERS",
    "CUSTOMER_SEGMENTS",
    "ColumnSpec",
    "ENTITY_ORDER",
    "EntitySchema",
    "ForeignKey",
    "GREATER_THAN_ZERO",
    "LogicalType",
    "NumericRange",
    "ORDERS",
    "ORDER_ITEMS",
    "ORDER_STATUSES",
    "PAYMENT_METHODS",
    "PRODUCTS",
    "SCHEMAS",
    "SOURCE_FILE_NAMES",
    "ZERO_TO_ONE_INCLUSIVE",
    "get_schema",
]
