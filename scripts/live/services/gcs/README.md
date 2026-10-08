# GCS emulator service recipe

Using this service in a test (accounts, tokens, routes, using your own instance): [docs/SERVICES.md](../../../../docs/SERVICES.md).

How it is built:

- `slt-gcs`: stock `fsouza/fake-gcs-server:1.54.0`, the storage API on port 4443.
- `slt-token`: a small fake OAuth token server (`faketoken.py`) on port 4444. Striim's GCS adapters
  use Google's Java client, which ignores `STORAGE_EMULATOR_HOST` and always fetches a token first;
  the framework's throwaway service-account key points its `token_uri` here.
- The adapters reject a host name as an endpoint, so the framework resolves the IP Striim reaches the
  emulator at and passes it in `${GCS_ENDPOINT}` and the emulator's `-public-host`.
- `GcsAdmin` (`livetest`) creates and clears each test's buckets.
