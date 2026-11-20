# Code samples

Two live cases that build and load your own Java into Striim before the app runs:

| Sample | Loads | Case |
|---|---|---|
| [`udf/`](udf/README.md) | a UDF library (`udf:`) | `referenceudf-mark-processed` |
| [`op/`](op/README.md) | an Open Processor (`op:`) | `referenceop-copy-adds-userdata` |

Both copy Postgres rows from a source table to a target table and stamp `userdata.processed=true`
on every event. The stamp is written to a file, and the case checks it.

They need Maven, a JDK 17 and `STRIIM_HOME` set to a Striim install. From the clone root:

```bash
striim-test run samples/code        # or samples/code/udf, samples/code/op
```

**Both run in Docker mode only.** Each compares the target table exactly and proves the load
finished with a `lifecycle:` block, and a test with a `lifecycle:` block writes its files under
`${OWNED_DIR}`, a directory the framework creates on the Striim nodes for the run. The framework
can create that directory only on its own Docker cluster, so against your own Striim server these
cases fail with `owned-dir-unsupported-native`. With your own server, start from a sample that runs
there: `samples/live/01-plain-replication`, `02-transform` or `04-lifecycle-check`, and add the
`op:` or `udf:` block from [docs/TESTING-YOUR-JAVA.md](../../docs/TESTING-YOUR-JAVA.md). `STRIIM_HOME`
must then be that server's own install, on this machine.

[docs/TESTING-YOUR-JAVA.md](../../docs/TESTING-YOUR-JAVA.md) explains how a case points at its code,
building by hand, and starting a module of your own. `tests/test_code_samples.py` builds both jars
the way the framework does before a live run.

## License

This folder is part of the Striim testing framework and is covered by the framework's `LICENSE`:
Elastic License 2.0 (ELv2). Code you copy from it stays under ELv2: keep this notice and give
anyone you share the copy with a copy of that `LICENSE`.
