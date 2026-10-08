# 03 File output

The app writes to a file instead of a table. Two seeded Postgres rows are read by `DatabaseReader` and
written by `FileWriter` as JSON to `${OWNED_DIR}/rows.json`. The framework creates `${OWNED_DIR}`
fresh for each run and removes it afterwards; nothing outside it is written or deleted.

- `lifecycle:` waits until the file has the two rows (`file-lines`, stable for one second).
- `exact:` reads the file back and compares its rows with `expected/rows.csv`. The rows may arrive in
  any order (this path does not promise one), but a missing, extra, duplicated or changed row fails.

This sample runs in Docker mode only (`STRIIM_URL` unset). The framework reads the output file from the
Striim server's filesystem, which it can do only inside the Docker cluster. Against your own Striim
server (native mode) the `exact:` file check is refused by design, so this sample fails there.

```
striim-test run samples/live/03-file-output
```
