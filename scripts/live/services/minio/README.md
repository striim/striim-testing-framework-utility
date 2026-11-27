# MinIO service recipe

Using this service in a test (tokens, settings): [docs/SERVICES.md](../../../../docs/SERVICES.md).

- An S3-compatible store for tests only, with fixed test credentials (`minioadmin` /
  `minioadmin-test`). `vertica-eon` keeps its communal storage here.
- The image is Chainguard's free `cgr.dev/chainguard/minio`, pinned by digest: `minio/minio` is no
  longer published. Chainguard's free tier serves only `latest`, so a digest that has aged out fails
  at pull time; re-pin `compose.yaml` (and `tests/test_service_catalog.py`) to the current `latest`
  digest.
- The image has `bash` but no `curl`, so the healthcheck sends `GET /minio/health/live` through
  bash's `/dev/tcp`.
- Data is in a compose volume, removed with the stack.
