# gcs-diff

A round-trip through the **Cloud Storage emulator** (fake-gcs-server): `GCSReader`
(DSV) reads a seeded object from the source bucket into a `WAEvent` stream, a CQ
projects just the two parsed data columns (the reader also attaches GCS metadata —
offset/path/object/`VALID_RECORD` — which we drop for a clean 1:1), and `GCSWriter`
(DSV) writes them to the target bucket. The diff tier reads **both buckets** via
`GcsAdmin` (`source_db: gcs` / `target_db: gcs`, source/target = the bucket names) and
asserts the target's records match the source. DSV is comma-delimited (CSV);
`GcsAdmin` parses objects positionally.

The framework starts the emulator and the fake token server and uploads a throwaway key. See
`docs/SERVICES.md`, "GCS", for the endpoint, token server and cooling-time settings.
