# 19 Expected halt

A case that passes because the app halts. DatabaseReader copies three seeded Postgres rows to a
DatabaseWriter whose `Tables:` mapping names `${TID}no_such_target`, a table no `ddl:` file
creates, so the app halts when the first row reaches the writer. Run:

```
striim-test run samples/live/19-expect-halt
```

`expect_halt: true` makes the halt the assertion, so the case needs no `assert:` block.
`expect_halt_contains: no_such_target` narrows it: the halt reason must name that table. The
table name carries `${TID}`, so under `--parallel` another case's halt cannot satisfy it.

In Docker mode the reason of a halt that happens while the app runs is read from the cluster's
server log; if that log cannot be read, the case fails and says so. Against your own Striim server
the log is not read: the case passes on the halt alone, and its record says the reason was not
checked.

Deliberately broken input: add a `ddl_target.sql` that creates `${TID}no_such_target` in the
target schema and list it under `ddl:` with `db: postgres-target`. The app then runs, never
halts, and the case fails when `timeout` runs out.

The gallery case [framework-expect-halt](../../../scripts/live/regression/framework/framework-expect-halt/test.yaml)
is the same test at smoke depth.
