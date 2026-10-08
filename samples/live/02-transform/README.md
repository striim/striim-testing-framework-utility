# 02 Transform

Like `01-plain-replication`, with a transformation in the middle: a CQ doubles the `amount` of each
event before `DatabaseWriter` writes it. The CQ modifies the `WAEvent` in place (`MODIFY(data[2] = …)`),
so the event keeps the source table metadata the writer needs.

The golden `expected/rows.csv` is calculated by hand from `seed.sql`:

| id | amount in | amount out |
|---:|---:|---:|
| 11 | 10.25 | 20.50 |
| 22 | 0.50 | 1.00 |
| 33 | 1234.50 | 2469.00 |

The target column is `NUMERIC(12,2)` and `exact:` compares it as `decimal:2`. The `lifecycle:` block is
the same as in `01-plain-replication`.

```
striim-test run samples/live/02-transform
```
