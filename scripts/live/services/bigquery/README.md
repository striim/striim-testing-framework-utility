# Local BigQuery emulator

Build a local BigQuery REST emulator with a dedicated fake OAuth endpoint. This is a
community emulator, built from the exact public source and dependency images in
`upstream.lock`. The framework image has its own version; it is not the upstream release image.

The current source baseline supports legacy `insertAll`. CSV batch compatibility is
not qualified by this baseline. Storage Write, cloud IAM, billing, performance and
recovery equivalence are outside this service's contract. BigQueryWriter batch loads
use direct resumable uploads, without GCS staging.

1. Install the live engine dependencies, including `google-cloud-bigquery`.
2. Build the image from this directory with
   `docker buildx build --load -t slt-bigquery:0.8.1-framework.5 .`.
   To isolate build cache, select your own docker-container builder with `--builder`.
   Set `SLT_BIGQUERY_IMAGE` to this tag to consume the prebuilt image without
   rebuilding it. Without that override, Compose builds the source recipe with a
   stack-prefixed image tag.
3. Start with `docker compose up -d --wait`. Readiness checks the dataset API and
   the result of `SELECT 1`, as well as the dedicated token endpoint.

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
local plain HTTP; the token URI must end in `/token`. This is not a cloud credential
or ADC configuration. Native Striim requires an explicit working endpoint and token
setup. `SLT_BIGQUERY_IMAGE` and `SLT_BIGQUERY_TOKEN_IMAGE` override images; retain
their exact digests when collecting qualification evidence.

Preserve `UPSTREAM-LICENSE` when redistributing. The pinned archive is checked before
extraction. Patch checksums and strict context checks reject source drift. The anonymous-result
lifetime patch reclaims the result table and its dataset/catalog
when its query job is deleted. Explicit destinations, replaced results and caller-modified
anonymous datasets are preserved; cleanup authority covers results created in the current
server instance. The pinned googlesqlite 0.3.1 backport from upstream PR 80 creates path catalogs without
reloading the root builtin-function set. The upstream change is open and unmerged;
its exact commit and patch digest are recorded in `upstream.lock`. A separate
go-googlesql 0.3.0 patch gives the value returned by `GetParserOptions` an owning
wrapper, so its C++ allocation is freed when the Go wrapper is collected. Its
existing parent lifetime is preserved. Cleared named parameters release their Go
type references while other analyzer dependencies stay pinned. Recreated catalogs
reuse one immutable builtin definition set, including its aliases, namespaces and
table-valued functions, through the existing non-owning catalog APIs.
The analyzer configuration is unchanged. The insertAll decoder preserves JSON
numeric literals without intermediate floating-point rounding, including signed
INT64 limits. CSV load semantics are not qualified by this series. Architecture
and lane qualification remain recorded separately in run evidence.

The source recipe builds Linux amd64 and arm64 images. Native amd64 API/query
health, independent integer/text readback, port remapping, two-stack isolation and
local credential refresh have been exercised. Arm64 has been cross-built; native
ARM runtime behavior and native-Striim mode remain unqualified.

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
bootstrap dataset and unknown tables are never dropped. Writer-created staging tables have
no deletion authority until their explicit ownership contract is implemented.

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

This coverage does not establish streaming insert-ID deduplication, crash replay,
CDC completion, Storage Write, writer TLS deployment, cloud performance or cloud
service equivalence. Additional writer scalar types require their own fixtures;
the native all-types reader control is separate from writer type qualification.
