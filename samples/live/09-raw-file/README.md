# 09 Raw file bytes

DatabaseReader reads the two seed rows in id order. A CQ projects only id and name
to a typed stream; DSVFormatter selects those two members in UTF-8, with
comma delimiters, CRLF row delimiters, no header and no quotes. See
[DSV Formatter](https://www.striim.com/docs/en/dsv-formatter.html).

```
striim-test run samples/live/09-raw-file
```

The expected bytes are `1,alpha\r\n2,beta\r\n` (17 bytes and two records),
derived by hand from seed.sql and the formatter settings. The SHA-256 is in
test.yaml. FileWriter's base FileName is rows.csv. Its documented rollover counter starts
at 00 and is inserted before the extension, so the directory must contain only
rows.00.csv, on one node (with the per-test TID prefix in the gallery).
See [file naming and rollover](https://www.striim.com/docs/en/setting-output-names-and-rollover---upload-policies.html).
The inventory is derived from that naming rule; the byte hash and record count
remain derived from seed.sql and the formatter settings.
Deliberately broken input: set RowDelimiter to `\n`. Counts agree but the byte
length and hash must fail. An extra rolled file must also fail.

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
