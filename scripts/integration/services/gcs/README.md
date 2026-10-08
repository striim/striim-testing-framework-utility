# Integration-tier GCS emulator recipe

The integration tier's `fake-gcs-server`, on port 14443, with one fixed bucket and per-test isolation
by `${TID}`-prefixed object paths. Settings are under `live_env` in `service.yaml`
([docs/SERVICES.md](../../../../docs/SERVICES.md), "Integration tier").

It runs one container, without the live tier's token server: an integration case sets
`StorageEndpoint: '${GCS_ENDPOINT}'`, and with an endpoint set the client uses no credentials, so a
token server would never be called. `INT_GCS_HOST` overrides the host token, but the container
still starts.
