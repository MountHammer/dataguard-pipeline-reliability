
# DataGuard - Testing Strategy and Test Plan

## 1. Purpose

This document defines the testing strategy for the DataGuard
Data Engineering pipeline.

The purpose is to verify that the pipeline processes valid data
correctly, detects invalid records, handles failures safely
and produces consistent analytical outputs.

## 2. Testing Objectives

1. Verify individual pipeline functions.
2. Verify source schema contracts.
3. Verify business validation rules.
4. Verify duplicate handling.
5. Verify referential integrity.
6. Verify successful end-to-end processing.
7. Verify failure handling and recovery.
8. Verify idempotency.
9. Verify source-to-target reconciliation.
10. Measure performance at different data volumes.

## 3. Testing Framework

Primary framework: Pytest

Coverage tool: pytest-cov

Test execution:

    python -m pytest -v

Coverage execution:

    python -m pytest --cov=src --cov-report=term-missing

Integration tests will use temporary directories and isolated
DuckDB databases.

## 4. Unit Testing

Unit tests validate individual functions independently.

| Test ID | Scenario | Input | Expected Result |
|---|---|---|---|
| UT001 | Valid customer | Complete customer | Accepted |
| UT002 | Missing customer ID | NULL ID | Rejected |
| UT003 | Empty customer name | Empty string | Rejected |
| UT004 | Valid product price | 100.00 | Accepted |
| UT005 | Zero product price | 0 | Rejected |
| UT006 | Negative product price | -100 | Rejected |
| UT007 | Valid quantity | 1 | Accepted |
| UT008 | Zero quantity | 0 | Rejected |
| UT009 | Negative quantity | -5 | Rejected |
| UT010 | Valid discount | 0.10 | Accepted |
| UT011 | Zero discount | 0 | Accepted |
| UT012 | Maximum discount | 1 | Accepted |
| UT013 | Negative discount | -0.01 | Rejected |
| UT014 | Excessive discount | 1.01 | Rejected |
| UT015 | Malformed numeric input | abc | Rejected |
| UT016 | Missing required value | NULL | Rejected |
| UT017 | NaN or infinity | Non-finite value | Rejected |

## 5. Schema Contract Testing

| Test ID | Scenario | Expected Result |
|---|---|---|
| SC001 | All expected columns present | Pass |
| SC002 | Missing required column | Batch failure |
| SC003 | Unexpected source column | Schema drift failure |
| SC004 | Reordered columns | Pass |
| SC005 | Empty file with valid header | Pass |
| SC006 | Empty file without header | Batch failure |
| SC007 | Unreadable file | Batch failure |

## 6. Duplicate Testing

| Test ID | Scenario | Expected Result |
|---|---|---|
| DU001 | Unique business IDs | Accepted |
| DU002 | Identical duplicate key | Keep first, quarantine subsequent copy |
| DU003 | Conflicting duplicate key | Quarantine all conflicting copies |
| DU004 | Duplicate customer ID | Apply customer duplicate policy |
| DU005 | Duplicate product ID | Apply product duplicate policy |
| DU006 | Duplicate order ID | Apply order duplicate policy |
| DU007 | Duplicate order item ID | Apply order-item duplicate policy |

Duplicate handling must be deterministic.

## 7. Referential Integrity Testing

| Test ID | Scenario | Expected Result |
|---|---|---|
| FK001 | Valid customer reference | Accepted |
| FK002 | Invalid customer reference | Rejected |
| FK003 | Valid product reference | Accepted |
| FK004 | Invalid product reference | Rejected |
| FK005 | Valid order reference | Accepted |
| FK006 | Invalid order reference | Rejected |
| FK007 | Order before customer signup | Rejected |
| FK008 | Warehouse orphan foreign key | Reconciliation failure |

## 8. Financial Business Rule Testing

| Test ID | Scenario | Expected Result |
|---|---|---|
| BR001 | Correct gross amount | Accepted |
| BR002 | Incorrect gross amount | Rejected |
| BR003 | Correct net amount | Accepted |
| BR004 | Incorrect net amount | Rejected |
| BR005 | Zero discount | Correct calculation |
| BR006 | Full discount | Net amount equals zero |
| BR007 | Fractional monetary amount | Correct two-decimal rounding |
| BR008 | Pending order revenue | Excluded |
| BR009 | Cancelled order revenue | Excluded |
| BR010 | Completed order revenue | Included |

## 9. Data Quality Testing

Verify that:

- Every accepted record satisfies its entity schema.
- Every rejected record has at least one reason code.
- Source filenames are preserved.
- Source row numbers are preserved.
- Run identifiers are recorded.
- No invalid foreign keys reach the warehouse.
- Invalid rows remain traceable.
- Valid rows are not rejected unnecessarily.

## 10. Integration Testing

| Test ID | Scenario | Expected Result |
|---|---|---|
| IT001 | Complete successful pipeline | SUCCESS |
| IT002 | Empty input batch | Successful empty outputs |
| IT003 | Mixed valid and invalid records | Valid published, invalid quarantined |
| IT004 | Large synthetic input | Successful processing |
| IT005 | Warehouse reconciliation | All checks pass |
| IT006 | Out-of-order transactions | Correct event-time processing |
| IT007 | Late-arriving transaction | Included in next complete snapshot |

Integration tests must use isolated temporary directories
and databases.

## 11. Idempotency Testing

| Test ID | Scenario | Expected Result |
|---|---|---|
| ID001 | Run identical input once | Initial warehouse snapshot |
| ID002 | Run identical input again | Identical analytical snapshot |
| ID003 | Verify business key uniqueness | No unintended duplicates |
| ID004 | Compare revenue totals | Identical totals |
| ID005 | Compare surrogate key mappings | Identical mappings |

The execution run IDs may differ, but run metadata must not
cause otherwise identical analytical snapshots to differ.

## 12. Failure and Recovery Testing

| Test ID | Scenario | Expected Result |
|---|---|---|
| FR001 | Invalid source file | Batch failure |
| FR002 | Failure during validation | Failure recorded |
| FR003 | Failure during publishing | Transaction rolled back |
| FR004 | Retry after failure | Successful recovery |
| FR005 | Repeated failed execution | Separate run records |
| FR006 | Previous committed warehouse exists | Remains unchanged after failed publish |

Each execution receives its own run ID.

## 13. Regression Testing

Regression tests ensure that previously corrected problems
do not return.

Important regression scenarios:

- Duplicate records must not reappear in the fact table.
- Invalid references must remain rejected.
- Revenue calculations must remain consistent.
- Repeated runs must not create unintended duplicates.
- Failed transactions must not partially publish data.
- Schema validation must continue enforcing the data contract.

## 14. Performance Testing

Benchmark the pipeline using:

- 10,000 order items
- 100,000 order items
- 250,000 order items

Measure:

- Total execution time
- Ingestion time
- Validation time
- Transformation time
- Publishing time
- Processed records per second
- Peak memory usage where practical

Results must be measured, not estimated.

Identify one practical optimization based on measurements.

## 15. Test Execution Evidence

Capture:

- Pytest terminal output.
- Test pass and failure counts.
- Coverage report.
- Sample valid records.
- Sample quarantined records.
- Reconciliation report.
- Idempotency comparison.
- Failure and retry evidence.
- Performance measurements.

## 16. Acceptance Criteria

The project is considered ready for submission when:

1. All required tests pass.
2. Primary keys are unique and non-null.
3. Foreign key relationships are valid.
4. Invalid records are traceable.
5. Business transformations are deterministic.
6. Repeated execution is safe.
7. Failed publishing does not commit partial changes.
8. Source and target aggregates reconcile.
9. Performance results are documented.
10. Setup and execution work in a clean environment.
