# Striim image recipe

The Docker cluster the framework starts when `STRIIM_URL` is unset: `slt-striim` (primary, web UI
and REST on 9080, login `admin`/`striim`), `slt-node` (a second node) and `slt-agent`. Using it is
covered in [docs/RUN-YOUR-FIRST-TEST.md](../../../../docs/RUN-YOUR-FIRST-TEST.md), "Docker mode"; how
the framework finds or starts it, in [docs/internals/ENGINE.md](../../../../docs/internals/ENGINE.md).

How it is built:

- `download-dependencies.sh` fetches the Striim packages and JDBC drivers (about 6.3 GB) into
  `images/striim/deps/` (not in git), or the framework copies them from `SLT_STRIIM_DEPS_MANIFEST`.
- `images/striim/Dockerfile` builds an amd64 image from them. The release comes from `STRIIM_HOME`
  (default 5.4.2); `.env` here holds only the Oracle instant client build arguments.
- The license is never baked in: `COMPANY_NAME`, `CLUSTER_NAME`, `PRODUCT_KEY` and `LICENCE_KEY` come
  from the environment at start.
- Each node runs `jmx_prometheus_javaagent` for the `jmx` assertion tier. CPU and memory caps come
  from `SLT_STRIIM_*_CPUS` and `SLT_STRIIM_MEM_*`.

By hand, from this folder: `./download-dependencies.sh`, `docker compose build slt-striim`,
`docker compose up -d slt-striim slt-node slt-agent`.
