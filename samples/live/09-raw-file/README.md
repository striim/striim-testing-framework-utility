# 09 Raw file bytes

DatabaseReader writes the two seed rows through DSVFormatter to one file, with
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
