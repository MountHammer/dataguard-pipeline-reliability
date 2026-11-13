# DataGuard pipeline run report

| Field | Value |
|---|---|
| Run ID | `6ed14548-b0d3-43c3-b13f-7adf4fd5cb22` |
| Status | **SUCCESS** |
| Started (UTC) | 2026-11-13T13:03:34Z |
| Ended (UTC) | 2026-11-13T13:03:35Z |
| Duration | 0.215 s |
| Source directory | `data\sample` |
| Database | `database\dataguard.duckdb` |
| Warehouse published | yes |

## Record counts

| Entity | Input | Accepted | Rejected |
|---|---:|---:|---:|
| customers | 20 | 20 | 0 |
| products | 15 | 15 | 0 |
| orders | 40 | 40 | 0 |
| order_items | 100 | 100 | 0 |
| **Total** | **175** | **175** | **0** |

Acceptance rate: **100.0%** · Rejection rate: **0.0%**

## Warehouse tables

| Table | Rows |
|---|---:|
| dim_customer | 20 |
| dim_product | 15 |
| dim_order | 40 |
| dim_date | 604 |
| fact_sales | 100 |

Completed net revenue (INR): **4414455.35**
