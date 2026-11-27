#!/bin/bash
# slt-vertica-eon entrypoint, one per node. EON_ROLE=primary creates the database on first boot
# (create_db, add_subcluster for the secondary, init.sql) or starts it on later boots, and owns
# the vcluster config. EON_ROLE=secondary runs its NMA and rejoins its node when it restarts
# alone. Certificates and the vcluster config are shared through the /shared volume.
#
# As in services/vertica, vcluster starts Vertica as a daemon, so PID 1 waits and stops the
# database (primary) or its node (secondary) on SIGTERM. The healthcheck waits for DONE_MARKER.
set -u
DONE_MARKER=/data/.slt-init-done
# /data is the container layer: clear a marker an earlier boot of this container left.
rm -f "$DONE_MARKER" /tmp/vcluster.log

export USER=dbadmin
DB=sltdb
PASSWORD=striim
ROLE=${EON_ROLE:?EON_ROLE must be primary or secondary}
PRIMARY=${EON_PRIMARY_HOST:?}
SECONDARY=${EON_SECONDARY_HOST:?}
SECONDARY_SC=sc_secondary
S3_ENDPOINT=${EON_S3_ENDPOINT:?}          # host:port, no scheme
S3_BUCKET=${EON_S3_BUCKET:?}
: "${EON_S3_ACCESS_KEY:?}" "${EON_S3_SECRET_KEY:?}"
S3_PARAMS="AWSEndpoint=$S3_ENDPOINT,AWSAuth=$EON_S3_ACCESS_KEY:$EON_S3_SECRET_KEY,AWSEnableHttps=0,AWSRegion=us-east-1"
SHARED=/shared
VCONF=$SHARED/vertica_cluster.yaml
CREATED=$SHARED/.db-created
# The primary writes a fresh token to BOOT at entry, and the same token to READY once this
# life's start_db or create is over; the secondary waits for READY to match BOOT.
BOOT=$SHARED/.primary-boot
READY=$SHARED/.primary-ready
CERTS=/opt/vertica/config/https_certs
VCLUSTER=/opt/vertica/bin/vcluster
VSQL=/opt/vertica/bin/vsql
ME=$(hostname -i | awk '{print $1}')
RECREATE="recreate the stack: python -m livetest.cli stop vertica-eon, then start it again"

log() { echo "[vertica-eon:$ROLE] $*" >&2; }
fail() { log "ERROR: $*"; [ -f /tmp/vcluster.log ] && tail -15 /tmp/vcluster.log >&2; exit 1; }
wait_for() { # seconds, command...
  local n=$1; shift
  for _ in $(seq 1 "$n"); do "$@" >/dev/null 2>&1 && return 0; sleep 1; done
  return 1
}

# Test-only CA and certificates, made once by the primary, valid for both nodes' names and IPs.
make_certs() {
  local subj=/O=StriimTest/OU=Vertica d=$SHARED/certs name cn sans
  mkdir -p "$d" && cd "$d" || return 1
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "$subj/CN=rootca" \
    -keyout rootca.key -out rootca.pem \
    -addext basicConstraints=critical,CA:TRUE -addext keyUsage=critical,keyCertSign,cRLSign \
    2>/dev/null || return 1
  sans="DNS:localhost,IP:127.0.0.1,DNS:$PRIMARY,DNS:$SECONDARY"
  for ip in ${EON_CERT_IPS:-}; do sans="$sans,IP:$ip"; done
  printf 'subjectAltName=%s\nextendedKeyUsage=serverAuth,clientAuth\n' "$sans" > ext.cnf
  for name in vertica_https dbadmin; do
    cn=$([ "$name" = dbadmin ] && echo dbadmin || echo "$PRIMARY")
    openssl req -newkey rsa:2048 -nodes -subj "$subj/CN=$cn" -keyout "$name.key" -out "$name.csr" \
      2>/dev/null || return 1
    openssl x509 -req -in "$name.csr" -CA rootca.pem -CAkey rootca.key -CAcreateserial \
      -days 3650 -extfile ext.cnf -out "$name.pem" 2>/dev/null || return 1
  done
  rm -f ./*.csr ext.cnf rootca.srl
  json() { awk '{printf "%s\\n", $0}' "$1"; }
  printf '{"name": "https", "mode": 2, "key": "%s", "certificate": "%s", "chain_certs": [], "ca_certificates": ["%s"]}\n' \
    "$(json vertica_https.key)" "$(json vertica_https.pem)" "$(json rootca.pem)" > httpstls.json
  touch .done
  cd / || return 1
}

nma_up() { curl -sfk "https://$1:5554/v1/health"; }
sql() { "$VSQL" -h "$1" -U dbadmin -w "$PASSWORD" -d "$DB" -Atc "$2"; }

# Secondary: wait (up to $1 seconds) until the primary's current life is ready. READY matching
# a BOOT that changed since our entry is a new life that finished. READY matching the BOOT seen
# at entry is either a live primary (secondary restarted alone; it answers SQL) or a stale pair
# a killed primary left (it does not answer until its new life has removed READY).
wait_primary_life() {
  local n=$1 boot0 b r
  boot0=$(cat "$BOOT" 2>/dev/null)
  for _ in $(seq 1 "$n"); do
    b=$(cat "$BOOT" 2>/dev/null); r=$(cat "$READY" 2>/dev/null)
    if [ -n "$b" ] && [ "$r" = "$b" ]; then
      [ "$b" != "$boot0" ] && return 0
      if sql "$PRIMARY" 'SELECT 1' >/dev/null 2>&1 \
          && [ "$(cat "$READY" 2>/dev/null)" = "$b" ] && [ "$(cat "$BOOT" 2>/dev/null)" = "$b" ]; then
        return 0
      fi
    fi
    sleep 1
  done
  return 1
}

if [ "$ROLE" = primary ]; then
  rm -f "$READY"
  TOKEN=$(cat /proc/sys/kernel/random/uuid 2>/dev/null || echo "$(date +%s%N)-$$-$RANDOM")
  { printf '%s\n' "$TOKEN" > "$BOOT.tmp" && mv -f "$BOOT.tmp" "$BOOT"; } || fail "could not write $BOOT"
  trap 'log "SIGTERM: stopping $DB"; rm -f "$READY"; "$VCLUSTER" stop_db --db-name "$DB" --config "$VCONF" --password "$PASSWORD" >/dev/null 2>&1; exit 0' TERM INT
  [ -f "$SHARED/certs/.done" ] || make_certs || fail "could not create certificates"
else
  trap 'log "SIGTERM: stopping node $ME"; [ -f "$VCONF" ] && "$VCLUSTER" stop_node --db-name "$DB" --config "$VCONF" --password "$PASSWORD" --hosts "$PRIMARY" --stop-hosts "$ME" >/dev/null 2>&1; exit 0' TERM INT
  wait_for 300 test -f "$SHARED/certs/.done" || fail "no certificates from the primary"
fi
cp "$SHARED"/certs/{rootca.pem,vertica_https.pem,vertica_https.key,dbadmin.pem,dbadmin.key,httpstls.json} "$CERTS"/ \
  || fail "could not install certificates"
chmod 600 "$CERTS"/*.key

/opt/vertica/bin/manage_node_agent.sh start >/dev/null || fail "NMA did not start"
wait_for 60 nma_up localhost || fail "NMA not healthy"

if [ "$ROLE" = primary ]; then
  wait_for 60 curl -sf "http://$S3_ENDPOINT/minio/health/live" \
    || fail "MinIO not reachable at $S3_ENDPOINT (is the minio service up?)"
  out=$(/usr/config/s3-mkbucket.py "$S3_ENDPOINT" "$S3_BUCKET" 2>&1) || fail "bucket: $out"
  log "bucket $S3_BUCKET: $out"
  wait_for 120 nma_up "$SECONDARY" || fail "secondary NMA not up at $SECONDARY:5554"

  if [ ! -d "/data/$DB" ]; then
    # A path of its own per database: a MinIO that outlived an earlier database keeps its files.
    communal="s3://$S3_BUCKET/$DB-$(date -u +%Y%m%d%H%M%S)"
    rm -f "$CREATED"      # a marker from an earlier life must not make a failed create look created
    t0=$(date +%s)
    "$VCLUSTER" create_db --db-name "$DB" --hosts "$PRIMARY" \
      --catalog-path /data --data-path /data --depot-path /data/depot --depot-size 1G \
      --communal-storage-location "$communal" --shard-count 2 \
      --config "$VCONF" --password "$PASSWORD" \
      --config-param "HttpServerConf=$CERTS/httpstls.json,$S3_PARAMS" \
      >/tmp/vcluster.log 2>&1 || fail "create_db failed; $RECREATE"
    log "create_db done in $(( $(date +%s) - t0 ))s"
    t0=$(date +%s)
    "$VCLUSTER" add_subcluster --db-name "$DB" --config "$VCONF" --password "$PASSWORD" \
      --subcluster "$SECONDARY_SC" --new-hosts "$SECONDARY" \
      --data-path /data --depot-path /data/depot --depot-size 1G \
      >/tmp/vcluster.log 2>&1 || fail "add_subcluster failed; $RECREATE"
    log "add_subcluster done in $(( $(date +%s) - t0 ))s"
    "$VSQL" -U dbadmin -w "$PASSWORD" -d "$DB" -v ON_ERROR_STOP=1 -f /usr/config/init.sql >/dev/null \
      || fail "init.sql failed; $RECREATE"
    touch "$CREATED"
    log "created $DB on $communal, provisioned qasource/qatarget"
  else
    # A first boot that stopped part-way leaves a catalog start_db cannot use.
    [ -f "$CREATED" ] || fail "/data/$DB exists but the first boot did not finish; $RECREATE"
    t0=$(date +%s)
    "$VCLUSTER" start_db --db-name "$DB" --config "$VCONF" --password "$PASSWORD" \
      --config-param "$S3_PARAMS" >/tmp/vcluster.log 2>&1 || fail "start_db failed"
    log "started $DB in $(( $(date +%s) - t0 ))s"
  fi
  { printf '%s\n' "$TOKEN" > "$READY.tmp" && mv -f "$READY.tmp" "$READY"; } || fail "could not write $READY"
else
  # On a full start the primary's start_db starts this node; wait for that first, or a
  # start_node here collides with it. Restarted alone (the primary up, this node DOWN), it
  # starts its own node.
  wait_for 600 test -f "$CREATED" || fail "database never created"
  wait_primary_life 600 || fail "primary did not finish starting within 600s ($READY does not match $BOOT)"
  wait_for 600 sql "$PRIMARY" 'SELECT 1' || fail "primary not answering"
  state=
  for _ in $(seq 1 120); do
    state=$(sql "$PRIMARY" "SELECT node_state FROM nodes WHERE node_address='$ME'" 2>/dev/null)
    [ "$state" = UP ] && break
    if [ "$state" = DOWN ]; then
      sleep 10
      state=$(sql "$PRIMARY" "SELECT node_state FROM nodes WHERE node_address='$ME'" 2>/dev/null)
      if [ "$state" = DOWN ]; then
        log "node $ME DOWN; start_node"
        "$VCLUSTER" start_node --db-name "$DB" --config "$VCONF" --password "$PASSWORD" \
          --hosts "$PRIMARY" --start-hosts "$ME" >/tmp/vcluster.log 2>&1 || fail "start_node failed"
      fi
    fi
    sleep 2
  done
  [ "$state" = UP ] || fail "node $ME did not come UP (state: ${state:-unknown})"
  log "node $ME UP"
fi
touch "$DONE_MARKER" || fail "could not write $DONE_MARKER"

# `wait` returns on a signal, so the trap runs promptly.
sleep infinity &
wait $!
