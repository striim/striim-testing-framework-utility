# Raw file inventory

Run `striim-test run scripts/live/regression/framework/framework-raw-file` in Docker mode.
The expected two CRLF records follow from seed.sql; LF output or an extra file fails.

FileWriter uses the base rows.csv and inserts its initial rollover counter 00
before the extension, producing rows.00.csv. The exact inventory declares that
name, derived from [the naming contract](https://www.striim.com/docs/en/setting-output-names-and-rollover---upload-policies.html).
Byte hash, 17-byte length and two-record count remain derived from the seed and
CRLF formatter settings. The owned directory isolates the inventory per attempt.

The writer uses `RolloverPolicy: 'EventCount:10000'` with no interval. Two input
records cannot trigger rollover. This avoids the default timer creating an empty
second file while the app remains running. The exact inventory includes every
file: an unexpected rows.01.csv fails even when its byte length is zero. It is
neither ignored nor added to this sample's expected inventory. Other raw tests
can explicitly declare a known empty file with zero bytes, the SHA-256 of empty
bytes, and zero records.

The typed CQ removes event metadata, timestamps and unrelated null fields. The
expected 17 bytes and hash remain derived from the two seed values, selected
members, comma delimiter, UTF-8 and CRLF; they are unchanged. Reconnecting the
writer to the unprojected source stream is another broken-input check: variable
event metadata must make byte equality fail.
