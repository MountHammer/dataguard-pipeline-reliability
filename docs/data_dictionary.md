
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

All timestamps are normalized to UTC.

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

## 7. Financial Calculations

Gross amount:

gross_amount = quantity * unit_price

Discount amount:

discount_amount = gross_amount * discount_pct

Net amount:

net_amount = gross_amount - discount_amount

Money calculations are rounded to two decimal places using
decimal arithmetic.

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

Each input file must contain its expected columns.

- Missing required columns cause batch-level failure.
- Unexpected columns cause schema-drift failure in strict mode.
- Column order does not affect validation.
- Invalid individual records are quarantined when the schema is valid.

## 10. Empty Batch Policy

An empty dataset with a valid expected header is permitted.

It must produce empty valid and rejected outputs without unexpected errors.

A missing or unreadable source file is treated as a batch-level failure.

## 11. Traceability

The ingestion process adds the following metadata:

- source_file
- source_row_number
- run_id

The original source filename and row number are preserved
in processed and quarantined records.

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

- run_id
- started_at_utc
- ended_at_utc
- status
- input_row_count
- valid_row_count
- rejected_row_count
- failure_reason
- execution_time_seconds

Timestamps use ISO-8601 UTC notation.

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

These limitations are documented trade-offs for the initial
working vertical slice.
