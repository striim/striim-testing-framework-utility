# SQL Server service recipe

Using this service in a test (accounts, tokens, routes, using your own instance): [docs/SERVICES.md](../../../../docs/SERVICES.md).

How it is built:

- `images/mssql/` layers `entrypoint.sh` and `init.sql` on `mcr.microsoft.com/mssql/server:2022-latest`
  (amd64 only; emulated on Apple Silicon). `MSSQL_PID=Developer` gives CDC and the SQL Server Agent,
  which runs the CDC capture jobs.
- The server starts with a complex bootstrap `sa` password, because SQL Server refuses a weak one at
  first start. Once it accepts connections, the entrypoint's background step relaxes `sa` to
  `striim`, creates the `qauser` database, enables CDC, and creates the `qasource` and `qatarget`
  logins and users. `MssqlAdmin.ensure_setup` (`livetest/mssqladmin.py`) checks the same things at
  test time.
- The healthcheck waits for a marker file the entrypoint writes only after that setup has committed,
  so `--wait` never returns while `sa`'s password is changing.
- The container has no restart policy, so a `service_outage` test can stop the server itself
  (`graceful_stop` in `service.yaml`).
