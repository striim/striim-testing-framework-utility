#!/usr/bin/env bash
# Download the large external dependencies for the Striim Docker image ONCE, into
# images/striim/deps/. The Dockerfile bind-mounts this dir at build time instead
# of re-fetching multi-GB artifacts on every (emulated) rebuild.
#
#   Run ONCE before `docker compose build`:  ./download-dependencies.sh
#
# Idempotent: files already present (non-empty) are skipped, so re-running only
# fetches what is missing. Versions below must match compose.yaml build args.
set -euo pipefail

# Load defaults from the .env docker compose reads, but let an already-set environment
# variable WIN (so `STRIIM_VERSION=5.4.0.6C ./download-dependencies.sh`, and the live
# harness passing a release version, are honored). `set -a; . .env` would clobber the
# caller's env, so instead only export keys that aren't already set. Matches docker
# compose's own precedence (shell env over .env).
ENV_FILE="$(cd "$(dirname "$0")" && pwd)/.env"
if [ -f "$ENV_FILE" ]; then
  while IFS= read -r _line || [ -n "$_line" ]; do
    case "$_line" in ''|\#*) continue;; esac
    _key=${_line%%=*}; _val=${_line#*=}
    _key=$(printf '%s' "$_key" | tr -d '[:space:]')
    [ -z "$_key" ] && continue
    # only take the .env value when the variable is not already set in the environment
    if [ -z "${!_key+x}" ]; then export "$_key=$_val"; fi
  done < "$ENV_FILE"
fi

STRIIM_VERSION="${STRIIM_VERSION:-5.4.2}"
INSTANTCLIENT_VERSION="${INSTANTCLIENT_VERSION:-216000}"
INSTANTCLIENT_ZIPFILE="${INSTANTCLIENT_ZIPFILE:-instantclient-basic-linux.x64-21.6.0.0.0dbru.zip}"

DEPS_DIR="$(cd "$(dirname "$0")" && pwd)/images/striim/deps"
mkdir -p "$DEPS_DIR"

fetch() {
  local url="$1" name="$2"
  local dest="$DEPS_DIR/$name"
  if [ -s "$dest" ]; then
    echo "  present  $name"
    return 0
  fi
  echo "  fetching $name"
  curl -fL --retry 3 --progress-bar -o "$dest.part" "$url"
  mv "$dest.part" "$dest"
  echo "  done     $name ($(du -h "$dest" | cut -f1))"
}

echo "Striim ${STRIIM_VERSION} dependencies -> $DEPS_DIR"

# Striim packages (striim-node is ~3.3 GB)
S3="https://striim-downloads.s3.us-west-1.amazonaws.com/Releases/${STRIIM_VERSION}"

# Fail fast with a clear message if this release's .debs simply aren't published at
# S3 (e.g. STRIIM_HOME points at a release whose artifacts aren't uploaded yet), instead of
# curl aborting deep inside `fetch` with a bare "command failed" from the harness.
_probe_status="$(curl -fsL -o /dev/null -w '%{http_code}' \
  "$S3/striim-dbms-${STRIIM_VERSION}-Linux.deb" 2>/dev/null || true)"
case "$_probe_status" in
  200|301|302) ;;
  *)
    echo "ERROR: Striim release ${STRIIM_VERSION} is not available for download" >&2
    echo "  (its artifacts are not published at $S3/) — pick another release." >&2
    exit 1
    ;;
esac

fetch "$S3/striim-dbms-${STRIIM_VERSION}-Linux.deb"    "striim-dbms-${STRIIM_VERSION}-Linux.deb"
fetch "$S3/striim-node-${STRIIM_VERSION}-Linux.deb"    "striim-node-${STRIIM_VERSION}-Linux.deb"
fetch "$S3/striim-agent-${STRIIM_VERSION}-Linux.deb"   "striim-agent-${STRIIM_VERSION}-Linux.deb"
fetch "$S3/striim-samples-${STRIIM_VERSION}-Linux.deb" "striim-samples-${STRIIM_VERSION}-Linux.deb"

# JDBC drivers
fetch "https://download.microsoft.com/download/0/2/A/02AAE597-3865-456C-AE7F-613F99F850A8/sqljdbc_6.0.8112.200_enu.tar.gz" "sqljdbc_6.0.8112.200_enu.tar.gz"
fetch "https://cdn.mysql.com/archives/mysql-connector-java-8.0/mysql-connector-java-8.0.30.zip" "mysql-connector-java-8.0.30.zip"
# Vertica: the same release as services/vertica's server.
fetch "https://repo1.maven.org/maven2/com/vertica/jdbc/vertica-jdbc/25.3.0-0/vertica-jdbc-25.3.0-0.jar" "vertica-jdbc-25.3.0-0.jar"

# JMX exporter
fetch "https://repo1.maven.org/maven2/io/prometheus/jmx/jmx_prometheus_javaagent/0.16.1/jmx_prometheus_javaagent-0.16.1.jar" "jmx_prometheus_javaagent-0.16.1.jar"

# Oracle Instant Client
fetch "https://download.oracle.com/otn_software/linux/instantclient/${INSTANTCLIENT_VERSION}/${INSTANTCLIENT_ZIPFILE}" "${INSTANTCLIENT_ZIPFILE}"

echo "All dependencies present in $DEPS_DIR"
