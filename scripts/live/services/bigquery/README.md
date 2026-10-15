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
   `docker buildx build --load -t slt-bigquery:0.8.1-framework.0 .`.
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
extraction. Patch checksums and zero-fuzz application reject source drift. The empty
initial series does not claim repaired CSV load semantics. Architecture and lane
qualification remain recorded separately in `upstream.lock`.

The source recipe builds Linux amd64 and arm64 images. Native amd64 API/query
health, independent integer/text readback, port remapping, two-stack isolation and
local credential refresh have been exercised. Arm64 has been cross-built; native
ARM runtime behavior and native-Striim mode remain unqualified.
