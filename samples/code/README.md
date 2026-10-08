# Code samples

Two live cases that build and load your own Java into Striim before the app runs:

| Sample | Loads | Case |
|---|---|---|
| [`udf/`](udf/README.md) | a UDF library (`udf:`) | `referenceudf-mark-processed` |
| [`op/`](op/README.md) | an Open Processor (`op:`) | `referenceop-copy-adds-userdata` |

Both copy Postgres rows from a source table to a target table and stamp `userdata.processed=true`
on every event. The stamp is written to a file, and the case checks it.

They need Maven, a JDK 17 and `STRIIM_HOME` set to a Striim install. Against your own Striim server,
`STRIIM_HOME` must be that server's own install, on this machine. From the clone root:

```bash
striim-test run samples/code        # or samples/code/udf, samples/code/op
```

[docs/TESTING-YOUR-JAVA.md](../../docs/TESTING-YOUR-JAVA.md) explains how a case points at its code,
building by hand, and starting a module of your own. `tests/test_code_samples.py` builds both jars
the way the framework does before a live run.

## License

This folder is part of the Striim testing framework and is covered by the framework's `LICENSE`:
Elastic License 2.0 (ELv2). Code you copy from it stays under ELv2: keep this notice and give
anyone you share the copy with a copy of that `LICENSE`.
