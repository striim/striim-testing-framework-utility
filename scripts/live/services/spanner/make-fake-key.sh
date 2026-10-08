#!/usr/bin/env bash
# Generate a THROWAWAY service-account JSON and upload it to the Docker cluster's
# UploadedFiles for the spanner-*-diff tests.
#
# Why: SpannerWriter/SpannerPGDialectWriter must PARSE a service-account key even
# when writing to the emulator (SPANNER_EMULATOR_HOST redirects the connection and
# skips auth). The key below is a locally-generated RSA key that corresponds to no
# real Google account — it grants nothing; it only needs to be structurally valid.
# It is NOT committed (see .gitignore); regenerate it with this script.
#
# NOTE: the live framework now self-provisions Spanner tests (SLT_SPANNER=1) — it
# applies the emulator redirect at cluster_up and uploads a throwaway key at test time
# (see services/spanner/README.md). This script is only for MANUAL / reused-cluster use
# (e.g. debugging against a cluster the framework didn't provision), where you must also
# start it in emulator mode yourself:
#   docker compose -f ../striim/compose.yaml -f ../striim/compose.spanner-emulator.yaml \
#     up -d slt-striim slt-node slt-agent
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/fake-gcp-key.json"
PEM_FILE="$(mktemp)"
trap 'rm -f "$PEM_FILE"' EXIT
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 2>/dev/null > "$PEM_FILE"
python3 - "$PEM_FILE" "$OUT" <<'PY'
import json, sys
pem = open(sys.argv[1]).read()
json.dump({
  "type": "service_account",
  "project_id": "test-project",
  "private_key_id": "0000000000000000000000000000000000000000",
  "private_key": pem,
  "client_email": "fake-sa@test-project.iam.gserviceaccount.com",
  "client_id": "000000000000000000000",
  "auth_uri": "https://accounts.google.com/o/oauth2/auth",
  "token_uri": "https://oauth2.googleapis.com/token",
  "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
  "client_x509_cert_url": "https://www.googleapis.com/robot/v1/metadata/x509/fake-sa%40test-project.iam.gserviceaccount.com",
  "universe_domain": "googleapis.com",
}, open(sys.argv[2], "w"), indent=2)
PY
# SLT_STACK_PREFIX (parallel stacks): target the prefixed cluster's containers.
for c in "${SLT_STACK_PREFIX:+${SLT_STACK_PREFIX}-}slt-striim" "${SLT_STACK_PREFIX:+${SLT_STACK_PREFIX}-}slt-node"; do
  docker cp "$OUT" "$c:/opt/striim/UploadedFiles/fake-gcp-key.json" && echo "uploaded -> $c"
done
echo "throwaway key at $OUT and in the cluster's UploadedFiles"
