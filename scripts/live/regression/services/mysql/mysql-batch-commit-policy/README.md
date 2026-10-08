# mysql-batch-commit-policy

`DatabaseReader` → `DatabaseWriter` on MySQL with `BatchPolicy` and `CommitPolicy` set, over twelve
seeded rows inserted in four statements. The case asserts that the app reaches RUNNING and
that the target ends with exactly 12 rows: no row lost or duplicated across batch and commit
boundaries. It does not check order or atomicity.

```bash
cd scripts/live
export SLT_INFRA_OWNERSHIP=shared SLT_KEEP_SERVICES=1
python -m pytest -m live regression/services/mysql/mysql-batch-commit-policy
```
