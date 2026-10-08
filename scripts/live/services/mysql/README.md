# MySQL service recipe

Using this service in a test (accounts, tokens, routes, using your own instance): [docs/SERVICES.md](../../../../docs/SERVICES.md).

How it is built:

- Stock `mysql:8.0`, started with `--binlog-format=ROW`, `--server-id=1` and a one-day binlog
  expiry, which `MySQLReader` CDC needs.
- `init-mysql.sql` runs at first start. The `qasource` and `qatarget` users and schemas are created
  at test time by `MySQLAdmin.ensure_setup` (`livetest/mysqlclient.py`), not by the init script, so
  an existing instance is set up the same way. `qasource` gets `REPLICATION SLAVE` and
  `REPLICATION CLIENT` and uses `mysql_native_password`.
- Serial runs reset both schemas before each test; parallel runs drop only the test's own
  `${TID}`-prefixed tables.
