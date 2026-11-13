# DataGuard Data Dictionary

## 1. Purpose

This document defines the source data schemas, entities, relationships,
data types, constraints, transformation rules and analytical target model
for the DataGuard pipeline reliability project.

The business domain is retail transactions.

All datasets are synthetic and reproducible.

## 2. Business Domain

The application processes retail transactional data from four source
entities:

1. Customers
2. Products
3. Orders
4. Order Items

The pipeline validates and transforms these datasets before publishing
analytical data into DuckDB.

## 3. Source Entities and Relationships

### 3.1 Customers

Grain: One row per customer.

Primary key: customer_id

| Column | Data Type | Nullable | Constraints |
|---|---|---|---|
| customer_id | VARCHAR | No | Unique, non-empty |
| customer_name | VARCHAR | No | Non-empty |
| email | VARCHAR | Yes | Valid email if provided |
| city | VARCHAR | Yes | Optional |
| customer_segment | VARCHAR | No | STANDARD, PREMIUM, VIP |
| signup_date | DATE | No | Valid calendar date |

### 3.2 Products

Grain: One row per product.

Primary key: product_id

| Column | Data Type | Nullable | Constraints |
|---|---|---|---|
| product_id | VARCHAR | No | Unique, non-empty |
| product_name | VARCHAR | No | Non-empty |
| category | VARCHAR | No | Non-empty |
| brand | VARCHAR | Yes | Optional |
| list_price | DECIMAL(12,2) | No | Greater than zero |
| is_active | BOOLEAN | No | TRUE or FALSE |

### 3.3 Orders

Grain: One row per order.

Primary key: order_id

Foreign key: customer_id references customers.customer_id.

| Column | Data Type | Nullable | Constraints |
|---|---|---|---|
| order_id | VARCHAR | No | Unique, non-empty |
| customer_id | VARCHAR | No | Valid customer reference |
| order_timestamp | TIMESTAMPTZ | No | Valid ISO-8601 timestamp |
| order_status | VARCHAR | No | COMPLETED, PENDING, CANCELLED |
| payment_method | VARCHAR | No | Supported payment method |

Supported payment methods:

- UPI
- CREDIT_CARD
- DEBIT_CARD
- NET_BANKING
- COD

All source timestamps are written in UTC using the `Z` suffix
(see §3.5).

### 3.4 Order Items

Grain: One row per individual product line in an order.

Primary key: order_item_id

Foreign keys:
- order_id references orders.order_id.
- product_id references products.product_id.

| Column | Data Type | Nullable | Constraints |
|---|---|---|---|
| order_item_id | VARCHAR | No | Unique, non-empty |
| order_id | VARCHAR | No | Valid order reference |
| product_id | VARCHAR | No | Valid product reference |
| quantity | INTEGER | No | Greater than zero |
| unit_price | DECIMAL(12,2) | No | Greater than zero |
| discount_pct | DECIMAL(5,4) | No | Between zero and one |
| gross_amount | DECIMAL(14,2) | No | Must reconcile |
| net_amount | DECIMAL(14,2) | No | Must reconcile |

In the initial synthetic generator, unit_price equals the
referenced product's list_price. Discounts are recorded separately
in discount_pct and are never folded into unit_price. This is a
generator invariant only, not a pipeline validation rule
(see §6 and §16.4).

### 3.5 CSV Serialization Conventions

All source files are UTF-8 CSV with one header row, a comma
delimiter and LF (`\n`) line endings. Text values never contain
line breaks, so every record occupies exactly one line.

| Data type | CSV representation | Example |
|---|---|---|
| NULL | Empty field | `a,,c` |
| VARCHAR | Text; quoted only when it contains a comma or quote | `Mumbai` |
| DATE | YYYY-MM-DD | `2024-03-15` |
| TIMESTAMPTZ | UTC ISO-8601 with `Z` suffix | `2024-03-15T09:41:07Z` |
| BOOLEAN | Lowercase `true` / `false` | `true` |
| INTEGER | Base-10 digits, no separators | `3` |
| DECIMAL(12,2), DECIMAL(14,2) | Exactly two decimal places, no currency symbol or thousands separator | `1499.00` |
| DECIMAL(5,4) | Exactly four decimal places | `0.1000` |

NULL and empty string are both written as an empty field.
An empty value in a non-nullable column is therefore treated as
a missing required value.

## 4. Entity Relationships

CUSTOMERS 1:N ORDERS

ORDERS 1:N ORDER_ITEMS

PRODUCTS 1:N ORDER_ITEMS

Each order belongs to exactly one customer.

Each order item belongs to exactly one order and one product.

The pipeline validates these relationships before publishing records.

## 5. Fact Table Grain

The analytical fact table has a grain of one row per order item.

This means each fact row represents one individual product line
within a customer order.

This grain preserves product-level transaction details and enables
analysis by product, customer, order and date.

## 6. Business Validation Rules

### Customer rules

- Customer ID must be present.
- Customer ID must be unique.
- Customer name cannot be empty.
- Customer segment must belong to the supported set.
- Email must be valid when provided.

### Product rules

- Product ID must be present and unique.
- Product name and category cannot be empty.
- List price must be greater than zero.
- Active status must be Boolean.

### Order rules

- Order ID must be present and unique.
- Customer reference must resolve.
- Order timestamp must be valid.
- Order status must belong to the supported set.
- Payment method must belong to the supported set.
- Order cannot predate the referenced customer's signup date.

### Order-item rules

- Order item ID must be present and unique.
- Order reference must resolve.
- Product reference must resolve.
- Quantity must be a positive integer.
- Unit price must be greater than zero.
- Discount percentage must be between zero and one.
- Gross and net amounts must reconcile with the business calculations.

### Generator invariants that are not validation rules

The synthetic generator always produces order items whose
unit_price equals the product's list_price and whose product is
active (see §16.4). These are properties of the generated data
only. The pipeline does not reject order items that differ:

- unit_price may differ from the product's current list_price.
- An order item may reference a product that is now inactive,
  because historical orders can legitimately include products
  that were later deactivated.

## 7. Financial Calculations

```text
gross_amount    = quantity * unit_price
discount_amount = round_half_up(gross_amount * discount_pct, 2)
net_amount      = gross_amount - discount_amount
```

All monetary calculations use decimal arithmetic
(Python decimal.Decimal) with ROUND_HALF_UP to two decimal
places. Binary floating-point is never used for money.

Because quantity is an integer and unit_price has two decimals,
gross_amount is exact. Only discount_amount is rounded, so
gross_amount = net_amount + discount_amount always holds exactly.

Example: quantity 1, unit_price 10.10, discount_pct 0.0500
gives gross 10.10; 10.10 × 0.05 = 0.505, so discount 0.51
(half-even rounding would give 0.50) and net 9.59.

Source gross_amount and net_amount values must match the expected
calculated values.

Completed sales revenue includes only transactions whose
order_status is COMPLETED.

## 8. Duplicate Handling Policy

Duplicates are identified using the relevant entity's primary key.

If multiple records have the same key and identical payloads:

- Retain the first deterministic occurrence.
- Quarantine subsequent occurrences.

If multiple records have the same key but conflicting payloads:

- Quarantine all records sharing that conflicting key.
- Assign CONFLICTING_DUPLICATE_KEY.

Duplicate resolution must not depend on database insertion order.

## 9. Schema Validation Policy

Schema validation runs per file before any record is checked.

Batch-level failures (no records from the file are processed):
- Missing or unreadable source file.
- Empty file without a header row.
- Missing required column.
- Unexpected column.
- Malformed file where parsing cannot safely continue or row
  boundaries cannot be established.

A batch-level failure in any one of the four required source
files fails the entire pipeline run. No analytical data is
published, and run-level failure metadata is preserved (§13).

Column order does not affect validation.

Record-level failures: when the schema is valid, any invalid
individual field value in a row that can be safely parsed causes
only that row to be quarantined, with one or more reason codes.
This includes missing required values, unparseable or wrong-typed
values, values outside allowed sets or ranges, duplicate keys,
invalid references and business-rule violations.

A row with an incorrect number of fields is a record-level
MALFORMED_ROW rejection when the parser can safely identify and
recover the row. Otherwise it is a batch-level failure.

## 10. Empty Batch Policy

An empty dataset with a valid expected header is permitted.

It must produce empty valid and rejected outputs without unexpected errors.

A missing or unreadable source file is treated as a batch-level failure.

An empty file without a header row is also a batch-level failure.

## 11. Traceability

The ingestion process adds the following metadata:

- source_file
- source_row_number
- run_id

The original source filename and row number are preserved
in processed and quarantined records.

source_row_number is the physical line number in the source CSV
file. The header is line 1 and the first data record is line 2.
Numbers are assigned on read, before any filtering, so they match
what a person sees when opening the file in a text editor.

Run identifiers and execution metrics are also recorded
in pipeline execution history.

## 12. Target Analytical Model

The DuckDB warehouse will use a dimensional model.

### Dimension tables

dim_customer:
- customer_key BIGINT PRIMARY KEY
- customer_id VARCHAR UNIQUE
- customer_name
- email
- city
- customer_segment
- signup_date

dim_product:
- product_key BIGINT PRIMARY KEY
- product_id VARCHAR UNIQUE
- product_name
- category
- brand
- list_price
- is_active

dim_order:
- order_key BIGINT PRIMARY KEY
- order_id VARCHAR UNIQUE
- order_timestamp
- order_status
- payment_method

dim_date:
- date_key INTEGER PRIMARY KEY
- full_date DATE UNIQUE
- day_number
- day_name
- month_number
- month_name
- quarter_number
- year_number
- is_weekend

### Fact table

fact_sales:

- sales_key BIGINT PRIMARY KEY
- order_item_id VARCHAR UNIQUE
- order_key BIGINT FOREIGN KEY
- customer_key BIGINT FOREIGN KEY
- product_key BIGINT FOREIGN KEY
- date_key INTEGER FOREIGN KEY
- quantity INTEGER
- unit_price DECIMAL(12,2)
- discount_pct DECIMAL(5,4)
- gross_amount DECIMAL(14,2)
- discount_amount DECIMAL(14,2)
- net_amount DECIMAL(14,2)
- source_file VARCHAR
- source_row_number BIGINT

All fact foreign keys must resolve to their corresponding dimension rows.

Surrogate keys will be assigned deterministically for the
current full-refresh implementation.

## 13. Run Metadata

Each pipeline execution has a unique run_id.

The run history records:

| Field | Meaning |
|---|---|
| run_id | Unique identifier for each execution (UUID4) |
| started_at_utc | Start time, ISO-8601 UTC with `Z` |
| ended_at_utc | End time, ISO-8601 UTC with `Z` |
| status | SUCCESS or FAILED |
| input_row_count | Data records read across all four source files |
| valid_row_count | Records accepted |
| rejected_row_count | Records quarantined |
| output_row_count | Rows published to fact_sales (0 when FAILED) |
| failure_reason | Empty on success; short reason when FAILED |
| execution_time_seconds | Wall-clock duration |

Rules:
- Every execution, including failed ones, appends exactly one
  record to `reports/pipeline_runs.jsonl` (one JSON object per line).
- The history is append-only; the pipeline never edits or deletes
  earlier records.
- For a SUCCESS run, input_row_count = valid_row_count +
  rejected_row_count.
- For a FAILED run, counts reflect progress up to the failure.
- Run metadata is never stored in analytical tables, so repeated
  runs on identical input produce identical analytical snapshots.

## 14. Late-Arriving Data and Historical Behaviour

order_timestamp represents business event time, not ingestion time.

Out-of-order records are not interpreted as invalid merely because
they arrive in a different sequence.

Late-arriving transactions are incorporated when the next complete
source snapshot is processed.

Order-item transaction records are preserved as individual facts.

Customer and product attributes represent the latest available
source snapshot. Slowly Changing Dimension Type 2 history is
outside the current implementation scope.

## 15. Assumptions and Limitations

- All data is synthetic.
- Currency is INR.
- Input data is supplied as CSV.
- The implementation uses full-refresh publishing.
- Incremental CDC processing is not implemented.
- Sequential surrogate keys are deterministic for identical
  source snapshots but may change when the snapshot changes.
- Customer and product attribute history is not maintained.
- The source system is simulated through reproducible data generators.
- The generator sets unit_price equal to the product's list_price
  (initial simplification; generator invariant only).
- Valid generated order items reference active products only
  (generator invariant only).
- Byte-identical generated data is guaranteed only in the pinned
  environment defined in requirements.txt. It is not claimed for
  other environments.
- Each execution writes run history to a local JSONL file.

These limitations are documented trade-offs for the initial
working vertical slice.

## 16. Synthetic Data Generation

### 16.1 Output files

customers.csv, products.csv, orders.csv and order_items.csv,
using the columns in §3 and the conventions in §3.5.

### 16.2 Identifiers

Identifiers are sequential and zero-padded to a fixed width that
does not depend on dataset size:

| Entity | Format | Example |
|---|---|---|
| Customer | CUST-NNNNNN | CUST-000001 |
| Product | PROD-NNNNNN | PROD-000001 |
| Order | ORD-NNNNNNNN | ORD-00000001 |
| Order item | ITEM-NNNNNNNNN | ITEM-000000001 |

### 16.3 Configuration constraints

Configuration is validated before generation. It is rejected when:

- Any row count is negative.
- n_orders > n_order_items (every order needs at least one item).
- n_order_items > n_orders × max_items_per_order.
- n_order_items > n_orders × n_active_products (products are
  distinct within an order).
- Orders exist but there are no customers.
- Orders exist but there are no active products.

All-zero counts are allowed and produce header-only files.

The number of inactive products is fixed by configuration
(floor(n_products × inactive_product_ratio)), so the number of
active products is known before generation.

### 16.4 Generation rules for valid data

- Parents are generated before children; foreign keys are chosen
  only from generated parents.
- Every order has between 1 and max_items_per_order items.
- Products are distinct within an order.
- Order items reference active products only. Inactive products
  remain in products.csv.
- unit_price equals the product's list_price.
- order_timestamp falls on or after the customer's signup_date (UTC).
- Dates come from a fixed configured window, never the current date.
- Rows are written sorted by primary key unless deterministic
  shuffling is enabled (later option, for out-of-order arrival
  tests). Shuffling changes row order only, never values.

The active-product and unit_price rules are generator invariants,
not pipeline validation rules (see §6).

### 16.5 Presets

| Preset | customers | products | orders | order_items |
|---|---|---|---|---|
| sample | 20 | 15 | 40 | 100 |
| perf-10k | 1,000 | 200 | 4,000 | 10,000 |
| perf-100k | 5,000 | 500 | 40,000 | 100,000 |
| perf-250k | 10,000 | 800 | 100,000 | 250,000 |

### 16.6 Reproducibility

A single integer seed drives independent random streams per
entity (NumPy SeedSequence) and a dedicated Faker instance with
the fixed locale en_IN.

For the same seed and configuration, the generator produces
byte-identical files in the pinned software environment
(requirements.txt: Python 3.12, pandas 3.1.1, NumPy 2.5.4,
Faker 40.54.0). Output under other versions is expected to be
valid but is not guaranteed to be identical.

## 17. Expected Anomaly Manifest

Anomaly injection is a later step applied to a copy of valid data.

Expected labels are never stored in source files. They are written
to `<output_dir>/_expected/expected_labels.csv`. The ingestion
pipeline reads only the four named source files and never reads
the `_expected/` directory. Manifest directories are excluded from
version control (.gitignore).

| Column | Meaning |
|---|---|
| anomaly_id | Unique label identifier |
| source_file | Affected source file name |
| source_row_number | Physical line number (§11) |
| entity | customers, products, orders or order_items |
| record_key | Primary key value; empty if the key was removed |
| anomaly_type | Injected anomaly |
| column_name | Affected column; empty for whole-row anomalies |
| original_value | Value before injection |
| injected_value | Value after injection |
| expected_outcome | REJECTED, ACCEPTED_FIRST_OCCURRENCE or REJECTED_CASCADE |
| expected_reason_code | Reason code the validator should assign |

Rules:

- At most one anomaly per source row.
- Valid rows rejected because a parent was rejected are listed
  as REJECTED_CASCADE.
- Batch-level scenarios are generated as separate scenario
  directories, each with `_expected/expected_batch_outcome.json`.
- The full list of reason codes will be defined before anomaly
  injection is implemented.
