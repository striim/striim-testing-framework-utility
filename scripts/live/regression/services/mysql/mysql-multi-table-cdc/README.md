# mysql-multi-table-cdc

One `DatabaseReader` reads three related MySQL tables (customers, their orders, the orders' items)
and one `DatabaseWriter` writes all three. The case asserts that each target table ends with the
seeded number of rows: 3 customers, 5 orders, 8 items. It does not compare values or check foreign
keys.

```bash
cd scripts/live
export SLT_INFRA_OWNERSHIP=shared SLT_KEEP_SERVICES=1
python -m pytest -m live regression/services/mysql/mysql-multi-table-cdc
```
