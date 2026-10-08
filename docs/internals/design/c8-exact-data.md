# C8 — Exact data semantics `slt-canon/1` (design note)

The design of exact comparison. The model is `livetest.canon`; the manifest dispatch, readers and
ownership check are `livetest.exactdata`; the plugin's input-snapshot and evidence hooks call them.
The customer view of the same rules is `docs/TEST-YAML.md`, "`exact:`". Version numbers below
(1.8.0, 1.9.0) name contract revisions, which the evidence records.

## C8.1 Manifest spelling

```yaml
exact:                         # top level; required by any exact data/file spec and by a declared diff
  version: 1                   # only 1
  max_rows: 100000             # optional, 1..1000000; rows per side
  max_bytes: 67108864          # optional, 1..536870912; canonical bytes per side
assert:
  data:
    - target: ${PG_TARGET_SCHEMA}.${TID}tgt
      target_db: postgres-target
      match: expected/tgt.csv  # case-dir relative; must resolve inside the case dir
      keys: [id, amount]       # existing projection, recorded as normalization
      exact:                   # or `exact: true` = all defaults
        columns: {amount: "decimal:2", created: timestamptz, payload: json, img: "binary:hex", ok: boolean, n: integer, d: date}
        ignore: [loaded_at]    # removed from both sides; must be present on at least one side
        order: any             # any (multiset, default) | sequence
        order_by: [id]         # required iff order: sequence on a db route; forbidden otherwise
  file:
    - path: ${OWNED_DIR}/out.json
      match: expected/events.csv
      exact: {order: sequence} # sequence needs exactly one file on one node (C8.3)
  diff:
    - {source: ..., source_db: postgres-source, target: ..., target_db: postgres-target, exact: {columns: {...}}}
```

- **Why a top-level block.** `manifest.py` rejects unknown top-level keys, so a 1.8.0 framework
  refuses a 1.9.0 exact case instead of running it dedup-blind.
- **`diff.exact: true`** (a boolean) without the block keeps its 1.8.0 meaning under profile
  `legacy-text/1` (C8.4). A mapping-form diff `exact` requires the block.

**Load-time rejections** (a `ManifestError` before provisioning, and in `striim-test doctor --case`):
- top-level `version` ≠ 1, an unknown top-level `exact` key, `max_rows`/`max_bytes` out of range;
- a top-level block with no exact spec (vacuous);
- a spec-level `exact` on `data`/`file`, or a mapping-form diff `exact`, without the block;
- `exact` without `match` (data/file), or together with `rows`, `min_rows`, `ordered` or `project`;
- unknown `exact` keys or type spellings; a column both typed and ignored;
- `order_by` without `order: sequence`, `order_by` on a file route, or `sequence` on a db route
  without `order_by`;
- a data or diff route other than `postgres-source`/`postgres-target`, or a native-mode file read
  (follow-ups F4/F6);
- a `match` path that escapes the case dir after `resolve()`.

Refusal codes: `exact-block-missing`, `invalid-exact-block`, `invalid-exact-spec`,
`invalid-declaration`, `exact-route-unsupported`.

## C8.2 Canonical rows `slt-canon/1`

**Cell model.** A row maps a column name (the exact string: DB description name, CSV header or JSON
key) to a cell: `ABSENT` (the key is not in the map), `NULL`, or `VALUE(type, text)`.

**Row bytes.** The cells sorted by name codepoint, each `[name, null]` or `[name, type, text]`
(undeclared columns have type `text`), serialised as
`json.dumps(cells, ensure_ascii=False, separators=(",", ":")).encode("utf-8")`. JSON escaping keeps
the `\n` framing unambiguous.

**Declaration.** `{columns, ignore, keys, order, order_by, profile}` with defaults `{}`, `[]`,
`null`, `"any"`, `null`, `"slt-canon/1"`; `ignore`, `keys` and `order_by` are kept verbatim.
`declarationSha256` = `sha256:` + hex sha256 of
`json.dumps(declaration, sort_keys=True, ensure_ascii=False, separators=(",", ":"))` in UTF-8.

**Digest.**
`sha256( b"slt-canon/1\n" + b"order=" + any|sequence + b"\n" + b"decl=" + declarationSha256 + b"\n" + Σ(row_bytes + b"\n") )`,
written `sha256:<hex>`, where `declarationSha256` is its `sha256:<hex>` text.
- For `any`, rows are sorted bytewise; for `sequence`, they stay in observed order.
- Multiplicity is kept: duplicates change the digest; rows are never deduplicated.
- Two different normalizations never share a digest.

**Markers (goldens only).**
- `<null>` → `NULL` (data, file).
- `<absent>` → `ABSENT` (file only); on a db route it is refused, `absent-marker-db-route:<col>`.
- An empty cell → `VALUE(text, "")` in an undeclared column; `invalid-value:expected:<col>` in a
  typed column (never NULL).
- The marker strings stay reserved in goldens. The actual side is never rewritten.
- A golden is UTF-8 without a BOM (`golden-bom`, `golden-undecodable`), read by `csv` with
  `newline=""` (CRLF accepted); a ragged row, empty or duplicate header is refused.

**Undeclared column (type `text`).**
- Actual side accepts `str`, `int` (base-10), `bool` (`true`/`false`) and `None` (NULL).
- Every other Python type (Decimal, float, datetime, date, time, timedelta, bytes, memoryview, dict,
  list, UUID, …) is refused: `undeclared-conversion:<col>:<type name>`, naming the declaration to add.
- Expected side: the CSV text as is.

**Declared types.** Each applies to both sides; a parse failure is `invalid-value:<side>:<col>`.

| Type | Expected text | Actual value | Canonical text |
|---|---|---|---|
| `integer` | `^-?[0-9]+$` | `int` (not bool); an integral Decimal with exponent 0 | base-10 |
| `decimal:<s>` (0 ≤ s ≤ 38) | finite decimal literal | Decimal, int; float only through `Decimal(repr(f))` | `format(quantize(10^-s), "f")`, zero without sign. `lossy-decimal:<side>:<col>` if quantizing changes the value; never rounded |
| `boolean` | `true` or `false` exactly | `bool` | `true`/`false` |
| `timestamptz` | ISO 8601 (`T` or one space) with `Z` or `±hh:mm` | aware `datetime` | UTC `YYYY-MM-DDTHH:MM:SS.ffffffZ`. Naive on either side: `timestamp-without-zone:<side>:<col>` |
| `timestamp` | ISO 8601 without offset | naive `datetime` | `YYYY-MM-DDTHH:MM:SS.ffffff`. Aware: `timestamp-with-zone:<side>:<col>` |
| `date` | ISO date | `date` (not datetime) | `YYYY-MM-DD` |
| `binary:hex` / `binary:base64` | hex (either case) / base64 | bytes, bytearray, memoryview; `str` in the declared encoding (file events) | lowercase hex |
| `json` | JSON text | dict, list, or JSON text | keys sorted by codepoint, `,`/`:` separators, `ensure_ascii=False`; numbers keep their exact Decimal text (`1.0` ≠ `1.00`; a float inside a dict goes through `Decimal(repr(f))`); duplicate keys and `NaN`/`Infinity` refused |

**Projection and ignore.** `keys` projects first; `ignore` then removes the named columns from both
sides. An `ignore` column present on neither side is `ignored-column-not-present`. Both are copied
verbatim into evidence; nothing is inferred from output.

**Column set.** Rows compare as whole maps, so a missing or extra column is a row difference.

**Order.** For `sequence` with `order_by`, the key tuples of the returned rows must be unique (the
query's `ORDER BY` is then total); a tie or a row without an `order_by` column is `order-not-total`.

**Samples.**
- `missing` (expected minus actual) and `extra` (actual minus expected): Counter differences over the
  row bytes, each ≤ 20 distinct rows with their counts.
- For `sequence`, `firstMismatch: {index, expected, actual}`.
- ≤ 64 KiB in total, computed from the full multisets, redacted in the envelope (C4 1.9.0);
  `sampleTruncated` says whether the cap cut them.
- **The digest always covers every row.** A side over `max_rows` or `max_bytes` (canonical row
  bytes plus one per row) fails with `canonical-limit-exceeded`; nothing is truncated before comparison.

**Logical digests** (4.3 cross-run comparison). `logical.expectedSha256` and `logical.actualSha256`
are computed like the canonical digests after replacing, in every `text`-typed cell, every exact
occurrence of a run-binding token value (TID, TID_UPPER, TID_ORACLE, NS, APP, APP_BARE, PG_SLOT,
OWNED_DIR, RUN_ID, WORKER, ATTEMPT, SENTINEL_*) with `${NAME}`, longest value first, in one pass.
Comparison itself always uses the rendered rows.

**Aggregates.** `data.canonicalSha256` (actual) and `data.expectedSha256` are `sha256:` over the lines
`<index>:<sha>\n` in comparison index order.

## C8.3 Reads, ownership, timing, goldens

- **Postgres** (`postgres-source`, `postgres-target`): a fresh bounded `lifecycle.PgProbe` per read,
  `query_described` returning `cursor.description` names;
  `SELECT * FROM "<schema>"."<table>" [ORDER BY <quoted order_by>] LIMIT %s` with `max_rows + 1`;
  the spec deadline and the C7.2 [r1] cancel protocol; psycopg2's own typecasters. `coerce_cell` /
  `select_rows` never feed `slt-canon/1`.

  **As implemented** (the deviation was accepted in review). `query_described` and
  the literal `SELECT *` read above were replaced, so the byte budget is enforced before rows are transferred.
  `PgProbe.read_exact` runs `lifecycle.exact_read` in the probe thread. Every statement runs in one
  `REPEATABLE READ, READ ONLY` transaction, so all of them see one snapshot:
  1. **Measurement; no rows are sent.**
     `SELECT count(*), coalesce(sum(octet_length(ROW(_slt_r.*)::text)), 0), coalesce(max(octet_length(ROW(_slt_r.*)::text)), 0) FROM (SELECT * FROM "<schema>"."<table>" LIMIT max_rows+1) _slt_r`.
     A count over `max_rows`, or a total of row text plus one byte per row over `2 × max_bytes`, is refused
     as `canonical-limit-exceeded` before any row is read.
  2. **Named server-side cursor.**
     `SELECT octet_length(ROW(_slt_r.*)::text), _slt_r.* FROM "<schema>"."<table>" _slt_r [ORDER BY _slt_r."<c>", ...] LIMIT %s`
     with `max_rows + 1`. Rows come back in `fetchmany` batches sized so one batch of the widest row stays
     within the budget. The client keeps a running total of the measured row sizes as a second bound, then
     drops the size column from the names and rows it returns.
  3. **Canonical check.** `canon.compare` still enforces `max_bytes` exactly on the canonical bytes.

  The `max_rows + 1` sentinel, the ownership gate, `statement_timeout` (set on each statement and each
  `FETCH`) and the cancel protocol are unchanged. json/jsonb cells decode through decoders registered on
  that connection only, which return `canon.JsonText`; the global typecasters are not changed. The
  `2 × max_bytes` factor bounds Postgres row text. It is not a measured cap on network bytes or Python RSS;
  real-Postgres validation is still outstanding.
- **File** (docker mode): listed through `ownership.docker_ls`; each file read with
  `docker exec <node> head -c <max_bytes+1> -- <file>` under a timeout; events parsed as the `file`
  assertion does, with `parse_float=Decimal`; projected by `file._project`. `sequence` requires the
  rendered path to match exactly one file on one node, else `order-source-ambiguous`. Native mode is
  refused.
- **Ownership.** In a lifecycle case every exact read target (the diff source included) must be a
  confirmed owned table of this attempt, and a file path must be inside a confirmed owned directory;
  otherwise `exact-target-not-owned` before any read. A legacy case records `owned: false` and never
  qualifies.
- **Timing.** Lifecycle case: one read per side after completion and stability, no polling. Legacy
  case: bounded reads polled until equal or timeout; never qualifies.
- **Goldens are immutable run inputs.** Their bytes are snapshotted once before provisioning and the
  comparison parses the snapshot. At finalization each golden is re-hashed; a change is
  `integrity.goldens.<path>.unchanged: false` and the case never qualifies. No framework path writes
  a golden or derives one from observed output.

## C8.4 Legacy profiles

- `legacy-text/1`: `diff.exact: true` without the top-level block. Same pass/fail and messages as
  1.8.0 (the stringified multiset of `normalized_row`), computed by `canon.legacy_text_multiset`.
  Recorded as a comparison; never qualifies.
- Distinct-set `data.match` / `file.match` and `data.ordered` without `exact`: unchanged, dedup-blind,
  never exact evidence.

## As implemented
- Exact specs are partitioned from the raw manifest lists before the legacy spec parsers (the synced
  `parse_diff_specs` refuses a mapping-form `exact`).
- With the top-level block, `diff.exact: true` is block-gated (`slt-canon/1`); without it, the diff keeps
  `legacy-text/1` and is recorded as a non-qualifying comparison.
- v1 record details and assertion failure messages are redacted (C4 1.9.0) before they reach the sidecar or
  junit.
