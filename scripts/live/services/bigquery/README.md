# Local BigQuery emulator

Supply a qualified local BigQuery REST emulator image through `SLT_BIGQUERY_IMAGE`.
The framework starts that user-supplied image and a dedicated fake OAuth endpoint;
it does not build or distribute the emulator. The image must serve the dataset,
query/job, legacy `insertAll` and bounded direct CSV resumable-upload APIs on port
9050, accept `--project` and `--dataset`, and include a readiness healthcheck that
checks the configured dataset and `SELECT 1`. Use an isolated credential-free server.

The qualified consumer contract requires OCI labels
`org.opencontainers.image.version=0.8.1-framework.8` and
`org.opencontainers.image.revision=a531d3deb716eaba4972f9afa88e03e2c0f1a1af`.
Missing or incompatible labels refuse the image. The image supplier owns the source,
patches, license, build recipe and image verification. Run evidence records the
managed service image ID; retain that ID with the supplied image's qualification.

The pinned service supports legacy `insertAll` and bounded direct CSV batch loads.
Actual Striim 5.4.2 writer fixtures cover both paths on Linux amd64. L2 qualification explicitly excludes the installed Java
client exact-chunk-boundary failure described below. Actual writer qualification
is recorded separately from these API controls. Storage Write, cloud IAM, billing, performance and
recovery equivalence are outside this service's contract. BigQueryWriter batch loads
use direct resumable uploads, without GCS staging.

1. Install the live engine dependencies, including `google-cloud-bigquery`.
2. Set `SLT_BIGQUERY_IMAGE` to the qualified image tag or digest already present in Docker.
   Without it, BigQuery cases skip with an actionable prerequisite reason. An explicitly
   selected lane exits nonzero for missing prerequisites; a skipped case is never a pass.
3. Run the case with `striim-test run`. The framework starts the supplied image and
   the dedicated token endpoint. It does not build the emulator or pull an implicit image.

`SLT_STACK_PREFIX` isolates Compose projects, containers and networks. Set
`SLT_BIGQUERY_REST_HOST_PORT` (default 9050) and `SLT_BIGQUERY_TOKEN_HOST_PORT`
(9051) to remap host ports. `SLT_BIGQUERY_PROJECT` (test-project) and
`SLT_BIGQUERY_DATASET` (slt) configure both bootstrap and clients.
`SLT_BIGQUERY_CPUS` (1) and `SLT_BIGQUERY_MEM_LIMIT` (1g) bound the emulator.
The server's Go heap budget is 640 MiB, leaving headroom within the container cap.

Docker Striim uses its documented `PrivateServiceConnectEndpoint` property with a
reachable IPv4 HTTP address. The service uses a separate generated
`UploadedFiles/fake-bigquery-key.json`, uploaded to every eligible app node. Its
token URI stays local; GCS and Spanner keys are independent. No machine DNS, trust
store or product code changes are required for this route.

To use an existing emulator, set `SLT_BIGQUERY_HOST`, `SLT_BIGQUERY_PORT`,
`SLT_BIGQUERY_WRITER_ENDPOINT` and `SLT_BIGQUERY_TOKEN_URI`. Endpoints must be
plain HTTP on loopback, private IPv4 or a supported Docker host name; the token URI
must end in `/token`. A private address can also reach a corporate or VPN host:
the operator must select an isolated emulator endpoint. This is not a cloud credential
or ADC configuration. Native Striim requires an explicit working endpoint and token
setup. `SLT_BIGQUERY_IMAGE` and `SLT_BIGQUERY_TOKEN_IMAGE` override images; retain
their exact digests when collecting qualification evidence.

`bigquery-target` supports bounded basic row assertions and typed exact comparisons through
anonymous REST clients. Only qualified scalar schemas are accepted; unsupported types fail
even on an empty table. See `docs/TEST-YAML.md` for the page/row/byte/deadline contract.
Query identifiers and job deletion status are retained in assertion details. Reader jobs and
results are deleted after each operation within its deadline; cleanup is best effort. Server
cancellation is not rollback.

Fixture DDL accepts plain `CREATE TABLE` statements with a `${TID}` table prefix in the
configured project/dataset. A pre-existing table is a collision, never adopted or replaced.
Cleanup deletes confirmed exact-name ledger entries and verifies absence. Endpoint/project/
dataset changes refuse recovery deletion. Retained ledgers can be replayed; the shared
bootstrap dataset and unknown tables outside a confirmed per-test dataset are never dropped.
For CDC, the separate per-test dataset ownership contract below contains generated staging.

Finite initial-load lifecycle targets can use BigQuery counts with a positive Postgres source
baseline. APPENDONLY insert/delete sentinel observation remains unsupported.

## Qualified legacy writer path

The tested product is Striim 5.4.2 with BigQuery REST SDK 2.59.0. Qualification
uses framework revision `8369251` and image `0.8.1-framework.5` on Linux amd64,
image SHA256 `5923fbf717e5e10c8879ea6e6ce238dffa61c57d5328fb96b8f47362e89b1d66`.
The sample and regression fixtures use the same hand-derived inputs and expected
values as those runs.

| Contract | Observed coverage |
|---|---|
| Write mode | APPENDONLY, `StreamingUpload=true`, `StreamingConfiguration='UseLegacyStreamingAPI=true'`; actual REST insertAll requests |
| Scalar fidelity | NULL and empty STRING, pipe, newline, double quote, signed INT64 limits and values beyond 2^53, decimal values compared at four places |
| Completion | Positive finite Postgres source baseline, target count completion, stability, then typed exact values and multiplicity |
| Comparison controls | Changed expected text fails exact comparison; a dedicated API-seeded canary fails on one extra identical row |
| Server error | A documented insertAll per-row error produces a target termination with the injected reason; separate from conversion errors |
| Queue startup | Capacity one fails during startup with its property diagnostic; capacity two independently starts and writes both expected rows |
| Endpoint | Documented PSC property with reachable IPv4 HTTP; direct and connection-profile initialization/reconstruction exercised |
| Architecture | Native Linux amd64 tested; native ARM execution remains unqualified |

Startup-only expected-HALT controls record passed assertions and
`run.qualifies=false` because they do not prove a completed data lifecycle.
The positive finite-load cases carry that data qualification. The tested queue
values do not establish other queue sizes, scoped property forwarding, saturation
or backpressure.

This legacy coverage does not establish streaming insert-ID deduplication, crash replay,
CDC completion, Storage Write, writer TLS deployment, cloud performance or cloud
service equivalence. Additional writer scalar types require their own fixtures;
the native all-types reader control is separate from writer type qualification.

## Bounded batch API and client exclusion

The supplied image must preserve the qualified CSV/upload behavior. Separate native API controls cover zero/one/two headerless
records, explicit positional schemas, comma/pipe/tab delimiters, skipLeadingRows,
custom NULL markers, empty STRING/BYTES, UTF-8, double quote and quoted newlines.
Malformed numeric/jagged rows and unsupported quote/encoding settings fail while
preserving existing target rows, including failed WRITE_TRUNCATE loads.

Qualified CSV scalar columns are STRING, INTEGER/INT64, NUMERIC, FLOAT/FLOAT64,
BOOLEAN/BOOL, base64-encoded BYTES, DATE, DATETIME, TIME and TIMESTAMP. Boolean
spellings follow the case-insensitive pairs 1/0, true/false, t/f, yes/no and y/n.
DATE uses YYYY-MM-DD; DATETIME uses YYYY-MM-DD HH:MM:SS with optional one-to-six
fraction digits; TIME uses HH:MM:SS with the same fractional precision. TIMESTAMP
accepts that DATETIME form in UTC, also with T, trailing Z or trailing ` UTC`.
Other valid cloud timestamp forms (offsets, slash dates and epoch numbers) remain
unqualified here. Malformed scalars fail before target mutation. The default empty
null marker produces NULL for every type; with a custom marker, empty STRING/BYTES
remain empty. These rules follow the [BigQuery CSV contract](https://cloud.google.com/bigquery/docs/loading-data-cloud-storage-csv#data_types)
within the stated temporal subset. Carriage returns inside quoted fields and blank
records are unqualified: Go's CSV reader normalizes CRLF and skips blank lines.
Additional types, nested fields and repeated fields remain unqualified.

Terminal success/failure and millisecond creation/start/end times persist;
upload responses and jobs.get agree, including zero counters after metadata reload.
`inputFileBytes` counts received file bytes. `outputRows` counts inserted records.
`outputBytes` sums UTF-8 bytes of accepted decoded non-null field representations,
without CSV syntax or separators. This is local accounting, without cloud billing
or storage-size equivalence. Failed loads report zero output rows/bytes. Bad-record
counts identify a known CSV record rejection; unrelated configuration/backend
failures do not fabricate a bad record.

Resumable buffers are limited to 16 MiB per upload, 32 MiB in aggregate, 32 active
sessions and 256 retained session entries. A later initiation sweeps entries idle
for ten minutes. Partial/status requests, byte-identical replay, out-of-order
rejection and final replay have native HTTP controls. Finalization applies one
load within the live session; replay tracking is bounded and in memory. An empty
`bytes */N` request completes only when N matches the buffered bytes; larger totals
remain incomplete and smaller totals are rejected. `bytes */*` never finalizes.
Restart/recovery and larger uploads are outside this candidate's contract.

Location follows the request origin, including remapped ports. Forwarded origin
is ignored unless the operator explicitly sets
`BIGQUERY_EMULATOR_TRUST_FORWARDED_ORIGIN=true` behind a trusted proxy. Compose
leaves this off. The unmodified Java REST client 2.59.0 follows both direct HTTP
and trusted-proxy TLS Locations; the TLS control uses a process-specific truststore
and normal hostname verification. It does not qualify a Striim TLS deployment.

The forced 256-KiB Java control loads a 300-KiB file correctly, with populated
LoadStatistics. An exact 512-KiB file fails: the SDK sends only unknown-total data,
then its zero-length close returns locally without a final HTTP request. The job
remains incomplete. Both direct-buffer and Files.copy controls reproduce this.
A separate diagnostic final request loads the row once and tolerates replay; that
manual step does not qualify the unmodified client. The pinned public
[Java RPC implementation](https://github.com/googleapis/java-bigquery/blob/v2.59.0/google-cloud-bigquery/src/main/java/com/google/cloud/bigquery/spi/v2/HttpBigQueryRpc.java#L1693)
contains that zero-length return. Changing the emulator cannot supply a missing
client request. The bounded L2 lane explicitly excludes this installed-client
exact-chunk-boundary case. Unknown-total chunks remain incomplete; no padding,
chunk-size or expected-row workaround is used. No product code or SDK jar is changed.
Actual writer and image architecture qualification are recorded separately.

The image supplier's client regression retains the correct oracle: one exact row.
It must pass with a corrected client before this exclusion is removed. A skip is
not a passing boundary test. The emulator may retain PENDING job metadata; no
completed load job or target rows result from the omitted final request.

The preceding bounded native API qualification used image `0.8.1-framework.7`, Linux amd64
config SHA256 `721406bd82f405efefcd5e945831e771b614d4a2b3aa928a4c24d597bb87dd26`.
The arm64 cross-build config is
`a7e788395af8d7bb2f24ba7b973d9a589f16f0ffedbfaa985af279bfdffea421`;
native ARM execution remains unqualified. The full feasible upstream Go suite,
HTTP/TLS client controls, load job persistence and chunk replay controls pass.
The exact-boundary client regression is an explicit skip, outside this bounded
qualification. These API controls are separate from the actual writer fixtures.

The CSV loader also recognizes gzip file bytes independently of HTTP headers,
as required by the default writer upload. Both received and expanded CSV are
limited to 16 MiB. Truncated files, checksum errors and expansion-limit failures
are rejected before target mutation. Input byte statistics retain the received
compressed size; loaded rows and decoded field-byte accounting remain unchanged.


Actual batch writer fixtures cover one/two headerless rows, five-row rollover
with eventCount two, NULL/empty/text, signed INT64 boundaries, four-place decimals,
quoted newlines and a malformed numeric load that terminates the target. Uploads
are gzip CSV file bytes, without an HTTP Content-Encoding header. The original
input/golden literals are preserved. See the batch sample and service gallery.

For non-ASCII text, run doctor and run with
`SLT_STRIIM_JAVA_SYSTEM_PROPERTIES=-Dfile.encoding=UTF-8`. The installed formatter
inherits the JVM default charset before compressing the local file; its BigQuery
Encoding property alone does not select that charset. A default ASCII JVM changed
non-ASCII characters before upload. The same Unicode literal and golden pass with
this existing per-stack JVM setting; the emulator does not reconstruct lost text.

A separate unmodified Java 2.59.0 Files.copy control uses its default 15-MiB chunk
size: 15 MiB unknown-total followed by 12,345 final bytes loads one exact row.
The forced 256-KiB control and default-size control are native API coverage;
the actual writer fixtures use normal default settings and small compressed files.
The exact-multiple defect concerns bytes fed to the upload channel, which can be
compressed bytes rather than the uncompressed CSV length.


Bounded CDC MERGE is exercised with PostgreSQLReader, batch mode and
OptimizedMerge=false. The hand-derived transition `(1,old),(2,keep)` to
`(1,new),(3,fresh)` includes a real update, delete and insert in captured staging
CSV and writer-generated compaction/MERGE SQL. Both readiness and completion
sentinels must be observed present and then absent through that same writer.
The framework allocates a fresh per-test dataset before DDL/TQL, refuses an
existing dataset, and ledger-binds its exact endpoint/project/dataset. This owns
generated stage objects as part of that explicit namespace. Cleanup stops the app,
drops exact fixture tables, removes the confirmed dataset with its contents and
verifies absence. The shared bootstrap dataset is retained; kept resources retain
the same scope for replay. Final MERGE state does not prove operation order,
replay multiplicity or crash/checkpoint safety. APPENDONLY CDC stays unqualified.


## Enforced writer scope and cloud differences

Before provisioning, `requires: [bigquery]` cases validate each literal
BigQueryWriter property block. Batch APPENDONLY is supported. MERGE requires a CDC
lifecycle whose sentinel observes `bigquery-target`, so generated staging tables
stay inside the ledger-owned per-test dataset. Other MERGE lifecycle shapes fail
before deployment. Streaming requires explicit `UseLegacyStreamingAPI=true` and
APPENDONLY mode. Storage Write, streaming MERGE, connection profiles and GCS staging
are refused. Properties outside the understood writer property list also fail
closed; adding a property requires reviewing its route first. These guards do not
claim that PSC routes an unqualified client through the REST emulator.
`ALTER TARGET` and `alter_recompile` actions are also refused in BigQuery cases:
post-creation writer property changes are outside the qualified scope.

The emulator has no streaming buffer. Real BigQuery restricts DML over recent
legacy REST streaming inserts; a local DML success does not qualify that cloud
behavior. Quotas, rate limits and DML concurrency/queue limits are not modeled.
See [BigQuery DML limitations](https://cloud.google.com/bigquery/docs/data-manipulation-language#limitations).
No real-cloud confirmation is claimed by this lane.

DDL and seed scripts split with [GoogleSQL lexical rules](https://cloud.google.com/bigquery/docs/reference/standard-sql/lexical):
quoted identifiers, single/double/triple strings, raw/bytes prefixes and backslash
escapes preserve embedded semicolons. Procedural scripts with internal statement
blocks are outside this plain-DDL/seed contract. Lexical preservation does not
qualify every emulator literal conversion: a native probe of `'''q';r'''`
stored `q'; r`. Triple-quoted literal value fidelity remains unqualified.

Generated fake-key uploads fail immediately if a container copy fails, for BigQuery,
GCS and Spanner alike. The upload remains retryable; a failed copy cannot mark it done.
