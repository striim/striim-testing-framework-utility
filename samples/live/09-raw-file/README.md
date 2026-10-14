# 09 Raw file bytes

DatabaseReader writes the two seed rows through DSVFormatter to one file, with
comma delimiters, CRLF row delimiters, no header and no quotes. See
[DSV Formatter](https://www.striim.com/docs/en/dsv-formatter.html).

```
striim-test run samples/live/09-raw-file
```

The expected bytes are `1,alpha\r\n2,beta\r\n` (17 bytes and two records),
derived by hand from seed.sql and the formatter settings. The SHA-256 is in
test.yaml. The directory must contain only rows.csv, on one node.
Deliberately broken input: set RowDelimiter to `\n`. Counts agree but the byte
length and hash must fail. An extra rolled file must also fail.
