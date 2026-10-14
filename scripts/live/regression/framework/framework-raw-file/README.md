# Raw file inventory

Run `striim-test run scripts/live/regression/framework/framework-raw-file` in Docker mode.
The expected two CRLF records follow from seed.sql; LF output or an extra file fails.

FileWriter uses the base rows.csv and inserts its initial rollover counter 00
before the extension, producing rows.00.csv. The exact inventory declares that
name, derived from [the naming contract](https://www.striim.com/docs/en/setting-output-names-and-rollover---upload-policies.html).
Byte hash, 17-byte length and two-record count remain derived from the seed and
CRLF formatter settings. The owned directory isolates the inventory per attempt.
