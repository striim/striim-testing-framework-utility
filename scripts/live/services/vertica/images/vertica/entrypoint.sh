#!/bin/bash
# Custom slt-vertica entrypoint. The base image (opentext/vertica-k8s) expects the Kubernetes
# operator to create TLS certificates, start the node management agent (NMA), and create or
# start the database; this does the same for one node under plain compose, then provisions
# the qasource/qatarget accounts (init.sql) -- so a bare `docker compose up` leaves the users
# present.
#
# Unlike slt-mssql, there is no server binary to `exec`: `vcluster create_db`/`start_db` start
# Vertica as a daemon and return. So init runs in the foreground, and PID 1 then waits,
# stopping the database cleanly on SIGTERM (`docker stop`). The healthcheck (compose.yaml)
# checks DONE_MARKER, written only after init.sql has succeeded.
set -u
DONE_MARKER=/data/.slt-init-done
# /data has no VOLUME/bind-mount (compose.yaml declares none), so it is the container's
# writable layer: it survives `docker stop`/`start` (only `down` removes it). Clear a marker
# from a PRIOR boot so the healthcheck can only see one this boot wrote.
rm -f "$DONE_MARKER"

export USER=dbadmin
DB=sltdb
PASSWORD=striim
# Loopback, not the container IP: the IP can change on restart, and start_db would then need
# re_ip. Clients reach the node through the published port.
HOST=127.0.0.1
CERTS=/opt/vertica/config/https_certs
VCLUSTER=/opt/vertica/bin/vcluster
VSQL=/opt/vertica/bin/vsql

log() { echo "[vertica-init] $*" >&2; }

# Test-only certificates for the NMA and vcluster, made once per container. The NMA reads
# rootca.pem and vertica_https.{pem,key}; vcluster authenticates as <user>.{pem,key};
# httpstls.json is the database's HTTPS config. 25.3 ships no generator script.
make_certs() {
  local subj=/O=StriimTest/OU=Vertica name cn
  cd "$CERTS" || return 1
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "$subj/CN=rootca" \
    -keyout rootca.key -out rootca.pem \
    -addext basicConstraints=critical,CA:TRUE -addext keyUsage=critical,keyCertSign,cRLSign \
    2>/dev/null || return 1
  printf 'subjectAltName=DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth,clientAuth\n' > ext.cnf
  for name in vertica_https dbadmin; do
    cn=$([ "$name" = dbadmin ] && echo dbadmin || echo localhost)
    openssl req -newkey rsa:2048 -nodes -subj "$subj/CN=$cn" -keyout "$name.key" -out "$name.csr" \
      2>/dev/null || return 1
    openssl x509 -req -in "$name.csr" -CA rootca.pem -CAkey rootca.key -CAcreateserial \
      -days 3650 -extfile ext.cnf -out "$name.pem" 2>/dev/null || return 1
  done
  rm -f ./*.csr ext.cnf
  json() { awk '{printf "%s\\n", $0}' "$1"; }
  printf '{"name": "https", "mode": 2, "key": "%s", "certificate": "%s", "chain_certs": [], "ca_certificates": ["%s"]}\n' \
    "$(json vertica_https.key)" "$(json vertica_https.pem)" "$(json rootca.pem)" > httpstls.json
  cd / || return 1
}

stop_db() {
  log "SIGTERM: stopping $DB"
  "$VCLUSTER" stop_db --db-name "$DB" --hosts "$HOST" --password "$PASSWORD" >/dev/null 2>&1
  exit 0
}
trap stop_db TERM INT

if [ ! -f "$CERTS/rootca.pem" ]; then
  make_certs || { log "ERROR: could not create certificates"; exit 1; }
fi

/opt/vertica/bin/manage_node_agent.sh start >/dev/null || { log "ERROR: NMA did not start"; exit 1; }
for _ in $(seq 1 60); do
  curl -sfk https://localhost:5554/v1/health >/dev/null && break
  sleep 1
done

# First boot creates the database and runs init.sql once (Vertica has no CREATE USER IF NOT
# EXISTS; see init.sql). A later boot starts the existing database, whose catalog already
# holds the users.
if [ ! -d "/data/$DB" ]; then
  "$VCLUSTER" create_db --db-name "$DB" --hosts "$HOST" --catalog-path /data --data-path /data \
    --password "$PASSWORD" --config-param "HttpServerConf=$CERTS/httpstls.json" \
    >/tmp/vcluster.log 2>&1 || { log "ERROR: create_db failed"; tail -5 /tmp/vcluster.log >&2; exit 1; }
  "$VSQL" -U dbadmin -w "$PASSWORD" -d "$DB" -v ON_ERROR_STOP=1 -f /usr/config/init.sql >/dev/null \
    || { log "ERROR: init.sql failed"; exit 1; }
  log "created $DB, provisioned qasource/qatarget"
else
  "$VCLUSTER" start_db --db-name "$DB" --hosts "$HOST" --catalog-path /data --password "$PASSWORD" \
    >/tmp/vcluster.log 2>&1 || { log "ERROR: start_db failed"; tail -5 /tmp/vcluster.log >&2; exit 1; }
  log "started $DB"
fi
touch "$DONE_MARKER" || { log "ERROR: could not write $DONE_MARKER"; exit 1; }

# `wait` returns on a signal, so the trap runs promptly.
sleep infinity &
wait $!
