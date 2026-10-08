# Vertica service recipe

Using this service in a test (accounts, tokens, routes, using your own instance): [docs/SERVICES.md](../../../../docs/SERVICES.md).

How it is built:

- `images/vertica/` builds on `opentext/vertica-k8s:25.3.0-8-multiarch`, the public Vertica image,
  which is meant for the Kubernetes operator. `entrypoint.sh` does the operator's work for one node:
  test-only TLS certificates for the node management agent, then the agent, then on first boot
  `vcluster create_db` for `sltdb` (superuser `dbadmin`) and `init.sql` (the `qasource` and
  `qatarget` users and schemas); on later boots `vcluster start_db`.
- Unlicensed, it runs as Community Edition (1 TB, three nodes). 26.1 and later refuse to run as
  Community Edition, so the image is pinned to 25.3.
- The base image owns its files as UID 998 / GID 996 without an OS user; the Dockerfile adds
  `dbadmin` with those ids, because Vertica names the database superuser after the OS user. The
  one-node catalog uses loopback, so a changed container IP needs no `re_ip`. Compose sets both
  open-file limits to 32768, which `create_db` requires.
- The healthcheck waits for a marker file written once the database is up, then logs in as
  `qasource`. First boot takes about 20 s (30 s under emulation).
- Database storage is in the container layer: removing the container removes the data.
- Licences: the JDBC driver is under the Vertica Client License Agreement; `vertica-python` is
  Apache-2.0.
