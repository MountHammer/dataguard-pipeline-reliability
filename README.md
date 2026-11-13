
# DataGuard – Data Pipeline Reliability & Testing Framework

## 1. Project Overview

DataGuard is a test-driven Data Engineering project designed to build,
validate and verify a reliable data processing pipeline.

The project addresses common data pipeline problems such as:

- Missing or malformed data
- Duplicate records
- Invalid data types
- Referential integrity violations
- Unexpected schema changes
- Incorrect business calculations
- Partial pipeline failures
- Duplicate data caused by repeated execution

The objective is to ensure that only valid and trustworthy records
are published to the analytical storage layer.

## 2. Business Problem

Organizations receive transactional data from multiple sources.
This data may contain incomplete records, incorrect values,
duplicate identifiers and inconsistent relationships.

Without proper validation and automated testing, these problems
can propagate into analytical databases and produce unreliable
business reports.

DataGuard aims to detect, isolate and report these issues
before they affect downstream consumers.

## 3. Project Objectives

1. Define source and target data schemas.
2. Generate reproducible synthetic datasets.
3. Implement data ingestion and validation.
4. Detect malformed records, nulls and duplicates.
5. Validate business rules and referential integrity.
6. Quarantine invalid records with traceability.
7. Transform and publish valid records.
8. Implement safe reruns and failure recovery.
9. Build automated unit, integration and regression tests.
10. Measure pipeline performance and reconcile outputs.

## 4. Technology Stack

| Technology | Purpose |
|---|---|
| Python | Pipeline implementation |
| Pandas | Data ingestion and transformation |
| NumPy | Numerical operations and validation |
| DuckDB | Analytical storage |
| Pytest | Automated testing |
| Faker | Synthetic data generation |
| Git | Version control |
| GitHub | Source code hosting |

## 5. Proposed Architecture

Synthetic Data Generation
        |
        v
CSV Source Data
        |
        v
Data Ingestion
        |
        v
Schema Validation
        |
        v
Business Rule Validation
        |
        +---------------------+
        |                     |
        v                     v
Valid Records           Rejected Records
        |                     |
        v                     v
Transformation          Quarantine
        |
        v
DuckDB Warehouse
        |
        v
Reconciliation and Reports

Automated tests will verify the individual components
and the complete pipeline.

## 6. Source Data Entities

| Entity | Primary Key | Description |
|---|---|---|
| Customers | customer_id | Customer information |
| Products | product_id | Product information |
| Orders | order_id | Transaction headers |
| Order Items | order_item_id | Individual product lines |

The target analytical fact grain will be one row per order item.

## 7. Testing Strategy

The project will include:

- Unit testing
- Integration testing
- Data-quality testing
- Boundary-value testing
- Malformed-input testing
- Duplicate-handling tests
- Schema contract tests
- Referential integrity tests
- Regression testing
- Idempotency testing
- Failure and recovery testing

## 8. Reliability and Observability

The pipeline will record:

- Unique run identifier
- UTC start and end timestamps
- Source and output row counts
- Rejected record counts
- Execution duration
- Pipeline status
- Failure reason

Rejected records will retain their source information
and explicit validation reason codes.

## 9. Reproducibility and Performance

The project will use a fixed random seed for reproducible
synthetic datasets.

It will support a small review dataset and configurable
larger datasets, targeting 100,000 or more order items.

Byte-identical output for a given seed is guaranteed only in the
pinned environment defined in requirements.txt (Python 3.12,
pandas 3.1.1, NumPy 2.5.4, Faker 40.54.0). Other environments
may produce valid but different datasets.

Performance measurements and source-to-target
reconciliation results will be documented.

## 10. Current Project Status

Initial repository and project structure created.

Implementation, automated tests, sample datasets,
performance measurements and execution evidence
will be added incrementally.

## 11. Project Scope

This is a local, reproducible Data Engineering solution.
External services are not required.

Known limitations and future improvements will be
documented as implementation progresses.

---

**Project:** DataGuard  
**Focus:** Data Engineering, Data Quality and Automated Testing  
**Repository:** dataguard-pipeline-reliability
