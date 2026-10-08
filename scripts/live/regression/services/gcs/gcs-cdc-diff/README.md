# gcs-cdc-diff

`gcs-diff` with the object uploaded after the app is RUNNING (`seed … when: post_start`), so
`GCSReader` has to find a new object while it polls, as a CDC reader captures changes made after
start. Same app otherwise: a CQ keeps the two data columns, `GCSWriter` (DSV) writes the target
bucket, and the diff tier compares both buckets through `GcsAdmin`. See `docs/SERVICES.md`, "GCS",
for the emulator settings.
