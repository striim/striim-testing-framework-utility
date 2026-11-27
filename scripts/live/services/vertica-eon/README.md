# Vertica Eon service recipe

Using this service in a test (accounts, tokens, routes, settings): [docs/SERVICES.md](../../../../docs/SERVICES.md).

How it is built:

- Two containers from one image, built on `opentext/vertica-k8s:25.3.0-8-multiarch` like
  `services/vertica`: `slt-vertica-eon-1` (node 1, the primary subcluster) and `slt-vertica-eon-2`
  (node 2, subcluster `sc_secondary`). Database `sltdb`, superuser `dbadmin`, and the `qasource` and
  `qatarget` users and schemas of `services/vertica`.
- Communal storage is the `minio` service (`depends_on: [minio]`), reached at
  `host.docker.internal:${SLT_MINIO_HOST_PORT}`, bucket `vertica-eon`. Each new database gets a path of
  its own (`s3://vertica-eon/sltdb-<UTC time>`), so a MinIO that outlived an earlier database does not
  block `create_db`. Stopping `minio` under a running `vertica-eon` breaks the database: stop and
  start both.
- `entrypoint.sh` does the Kubernetes operator's work: test-only TLS certificates for both nodes
  (shared through a volume), the node management agents, then on first boot `vcluster create_db`
  (two shards, depot 1 GB), `add_subcluster` and `init.sql`; on later boots `start_db`. A node 2
  restarted alone rejoins with `start_node`. A first boot that stopped part-way cannot be resumed: the
  primary exits saying to recreate the stack (`python -m livetest.cli stop vertica-eon`, then start).
- The catalog stores node IPs, so the nodes have static IPs, `.11` and `.12` of a /24 set by
  `SLT_VERTICA_EON_SUBNET` (its first three octets, default `172.16.250`). Host names
  (`<prefix>-slt-vertica-eon-1`, `-2`) and the certificate names follow `SLT_STACK_PREFIX`. A second
  stack running at once needs its own `SLT_VERTICA_EON_SUBNET` and host ports.
- First boot takes about 60 s; later boots about 20 s. Each node has 2 GB and 1 CPU
  (`SLT_VERTICA_EON_MEM_LIMIT`, `SLT_VERTICA_EON_CPUS`).
- Vertica's JSON startup progress is not copied to the container log
  (`VERTICA_STARTUP_LOG_DUPLICATE=/dev/null`); it stays in each node's `startup.log`.
- `SLT_VERTICA_EON_BASE` replaces the base image for a build without network access, for example
  `slt-vertica:25.3.0-8`, which `services/vertica` builds from the same base.
- Data is in the container layers and in MinIO: removing the containers removes the database.
