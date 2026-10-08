#!/bin/bash

rm -rf /opt/striim/elasticsearch/data/* /opt/striim/logs/*

# Keystore extension moved from .jks to .p12 (PKCS#12) between 5.4.0.6C and 5.4.2:
# sksConfig/aksConfig still succeed, they just write the other name. Copying a fixed
# name therefore fails silently on the release that does not use it, the node and agent
# come up with no shared keystore, and neither can join the cluster -- so resolve the
# name at runtime instead of hardcoding it. <base> is sks or aks.
#
# /shared outlives a cluster (compose down without -v), so it can still hold the keystores
# of an earlier start, of this release or another. A node or agent that takes one of those
# never joins. The primary therefore stamps KEYSTORE_EPOCH and removes the old keystores
# before it generates new ones, and consumers accept only a keystore newer than the stamp.
# Consumers wait for the primary's 9080 first, which comes up only after the stamp.
#   keystore_path <dir> <base> [epoch] -> echoes the keystore there, newer than epoch if given
#   wait_keystore  <dir> <base>        -> blocks until one newer than /shared's stamp appears
KEYSTORE_EPOCH=/shared/.keystore-epoch
keystore_path() {
    for ext in jks p12; do
        [ -f "$1/$2.$ext" ] || continue
        [ -z "${3:-}" ] || [ "$1/$2.$ext" -nt "$3" ] || continue
        echo "$1/$2.$ext"; return 0
    done
    return 1
}
# /shared is the slt-striim-shared volume, and it can outlive `down -v`: Docker keeps a volume
# another container still uses. A previous cluster's keystore left there is taken by
# keystore_path (it prefers .jks), and a 5.4.2 node then fails "Keystore could not be opened:
# integrity check failed" with the new password. So the primary clears what it publishes first.
clear_shared() {
    rm -f "$1"/sks.jks "$1"/sks.p12 "$1"/sksKey.pwd "$1"/aks.jks "$1"/aks.p12 "$1"/aksKey.pwd \
          "$1"/startUp.properties "$1"/server.sh
}
# Publish <src> as /shared/<name>: the password first, then the keystore by rename, so a
# consumer that sees the keystore also sees a complete file and its password.
share_keystore() {
    cp "$2" "/shared/$(basename "$2")"
    cp "$1" "/shared/.$(basename "$1").tmp" && mv "/shared/.$(basename "$1").tmp" "/shared/$(basename "$1")"
}
wait_keystore() {
    while ! keystore_path "$1" "$2" "$KEYSTORE_EPOCH" > /dev/null; do
        echo "Waiting for shared $2 keystore"
        pause 5
    done
}

# Print a rendered startUp.properties for debugging WITHOUT the license: the primary and node
# used to `cat` it after PRODUCT_KEY/LICENCE_KEY were substituted in, which put both values in
# clear in `docker logs`. Never cat that file directly.
show_props() {
    sed -E 's/^[[:space:]]*(ProductKey|LicenceKey)[[:space:]]*=.*/\1=<redacted>/' "$1"
}

# Everything this script and its children print reaches `docker logs`, and the server logs
# the license itself at boot ("ProductKey: …", "License Key: …"), so all of it goes through
# this filter: the literal PRODUCT_KEY/LICENCE_KEY values from the environment (a quoted
# pattern, so no value needs escaping) and anything after a key name in any spelling or case.
# Plain bash, one line at a time: mawk held its input until its read buffer filled, so a
# running container's docker logs lost everything after the first few kilobytes.
redact_stream() {
    local line re='(product[ _]?key|licen[cs]e[ _]?key)[[:space:]]*[:=][[:space:]]*'
    shopt -s nocasematch
    while IFS= read -r line || [ -n "$line" ]; do
        [ ${#PRODUCT_KEY} -ge 4 ] && line=${line//"$PRODUCT_KEY"/"<redacted>"}
        [ ${#LICENCE_KEY} -ge 4 ] && line=${line//"$LICENCE_KEY"/"<redacted>"}
        if [[ $line =~ $re ]]; then
            line=${line%%"${BASH_REMATCH[0]}"*}${BASH_REMATCH[0]}'<redacted>'
        fi
        printf '%s\n' "$line"
    done
}

# Every path at once: the nohup'd starts' stderr, sksConfig/aksConfig, the log tails and the
# agent's failure-path cats.
exec > >(redact_stream) 2>&1
REDACT_PID=$!

# `docker stop` sends SIGTERM to PID 1, which is this script. bash as PID 1 has no default
# action for it and runs no trap while a foreground command runs, so the old last command (a
# foreground `tail -f`) left SIGTERM dropped and every node was SIGKILLed after compose's stop
# timeout (exit 137). Now each Striim start leads its own process group (`setsid`,
# recorded in STRIIM_GROUPS), the final tail runs in the background under `wait`, and TERM/INT
# stop the groups newest first: the JVMs get SIGTERM and run their shutdown hooks, and Derby
# gets its own shutdown command. Then the tail is stopped and the redaction filter drains
# before the script exits 143. Nothing bypasses redact_stream, boot or shutdown.
STRIIM_GROUPS=()
DBMS_GROUP=
TAIL_PID=
wait_group() {    # until no process of group $1 is left
    while kill -0 -- "-$1" 2>/dev/null; do sleep 0.2; done
}
stop_striim() {
    trap '' TERM INT
    echo "Stopping Striim (signal received)"
    [ -n "$FG_GROUP" ] && kill -TERM -- "-$FG_GROUP" 2>/dev/null
    local i pg
    for (( i=${#STRIIM_GROUPS[@]}-1; i>=0; i-- )); do
        pg=${STRIIM_GROUPS[i]}
        if [ "$pg" = "$DBMS_GROUP" ]; then
            /opt/striim/sbin/striim-dbms stop > /dev/null 2>&1 || kill -TERM -- "-$pg" 2>/dev/null
        else
            kill -TERM -- "-$pg" 2>/dev/null
        fi
        wait_group "$pg"
    done
    echo "Striim stopped"
    # tail -f reads on its own schedule: give it the JVM's last lines before stopping it.
    [ -n "$TAIL_PID" ] && sleep 1 && kill "$TAIL_PID" 2>/dev/null && wait "$TAIL_PID" 2>/dev/null
    exec >&- 2>&-
    wait "$REDACT_PID" 2>/dev/null
    exit 143
}
trap stop_striim TERM INT
# The trap also has to run during start-up: bash defers it until a FOREGROUND command ends,
# and the primary's boot has a 15 s sleep and two keystore JVM runs, longer than compose's
# 10 s stop timeout. So every boot wait and keystore tool runs through fg_group: in its own
# process group, in the background, under `wait`, which a trapped signal interrupts at once.
# stop_striim then ends that group too, so nothing keeps the redaction pipe open.
FG_GROUP=
fg_group() {
    setsid "$@" &
    FG_GROUP=$!
    wait "$FG_GROUP"
}
pause() { fg_group sleep "$1"; }
# follow <log>: the container's last command. Backgrounded so the trap can run.
follow() {
    tail -f "$1" &
    TAIL_PID=$!
    wait "$TAIL_PID"
}

if [ "${ROLE}" == "primary" ]; then

    touch "$KEYSTORE_EPOCH"
    clear_shared /shared

    # Temporary workaround until DNS is supported
    LOCAL_IP=`ifconfig | sed -En 's/127.0.0.1//;s/.*inet (addr:)?(([0-9]*\.){3}[0-9]*).*/\2/p'`
    STRIIMAI_IP=`getent hosts ${STRIIMAI_HOSTNAME} | awk '{print $1}'`

    cp /opt/striim/conf/startUp.properties.orig /opt/striim/conf/startUp.properties
    sed -i "s|WAClusterName=|WAClusterName=${CLUSTER_NAME}|g" /opt/striim/conf/startUp.properties
    sed -i "s|CompanyName=|CompanyName=${COMPANY_NAME}|g" /opt/striim/conf/startUp.properties
    sed -i "s|# ProductKey=|ProductKey=${PRODUCT_KEY}|g" /opt/striim/conf/startUp.properties
    sed -i "s|# LicenceKey=|LicenceKey=${LICENCE_KEY}|g" /opt/striim/conf/startUp.properties
    sed -i "s|# MEM_MAX=4096m|MEM_MAX=${MEM_MAX}|g" /opt/striim/conf/startUp.properties
    sed -i "s|# MetaDataRepositoryLocation=|MetaDataRepositoryLocation=${PRIMARY_HOSTNAME}:1527|g" /opt/striim/conf/startUp.properties
    sed -i "s|# WAZookeeperAddress=localhost:2181|WAZookeeperAddress=${ZOOKEEPER_HOSTNAME}:2181|g" /opt/striim/conf/startUp.properties
    sed -i "s|# WABrokerAddress=localhost:9092|WABrokerAddress=${KAFKA_HOSTNAME}:9092|g" /opt/striim/conf/startUp.properties
    sed -i "s|# EnableJmx=false|EnableJmx=${ENABLE_JMX}|g" /opt/striim/conf/startUp.properties
    sed -i "s|StriimAIServiceAddress = localhost:9000|StriimAIServiceAddress = ${STRIIMAI_IP}:9000|g" /opt/striim/conf/startUp.properties

    echo "NATIVE_LIBS=/app/instantclient" >> /opt/striim/conf/startUp.properties

    # Hazelcast's own cloud auto-detection picks the GCP discovery strategy whenever it's
    # reachable from a GCP metadata server (true of any container on a GCP VM, including this
    # one) — but that strategy discovers *VM instances* via the Compute Engine API, which has
    # no notion of sibling Docker containers on the SAME VM, and this VM's service account has
    # no Compute Engine access anyway ("Google Cloud API access is forbidden! Starting
    # standalone." in striim.server.log). Every node then forms its own single-member cluster
    # and never finds its peers — a silent split-brain (confirmed via LIST DEPLOYMENTGROUPS:
    # the 'default' group never has 2 members). Force plain TCP-IP discovery with an explicit
    # member list instead — the standard fix for Hazelcast clustering inside Docker Compose —
    # bypassing cloud auto-detection entirely regardless of what host it runs on.
    echo "striim.cluster.enable-tcpipClustering=TRUE" >> /opt/striim/conf/startUp.properties
    echo "striim.node.servernode.address=${PRIMARY_HOSTNAME},striim-node" >> /opt/striim/conf/startUp.properties
    show_props /opt/striim/conf/startUp.properties

    # Generate the SERVER keystore at runtime, then share it so cluster nodes reuse it.
    echo "Configuring server keystore (sksConfig)"
    fg_group /opt/striim/bin/sksConfig.sh -a striim -s striim -k striim -t Derby

    cp /opt/striim/bin/server.sh.orig /opt/striim/bin/server.sh
    sed -i "s|#!/bin/bash|#!/bin/bash\n\nJAVA_AGENT=\"-javaagent:/opt/striim/jmx/jmx_prometheus_javaagent-0.16.1.jar=7071:/opt/striim/jmx/jmx_prometheus_config.yaml\"|g" /opt/striim/bin/server.sh
    sed -i "s|#!/bin/bash|#!/bin/bash\n\nJVM_DEBUG_OPTS=\"-Xdebug -Xrunjdwp:transport=dt_socket,server=y,suspend=n,address=0.0.0.0:8787\"|g" /opt/striim/bin/server.sh
    sed -i 's|$JVM_DEBUG_OPTS \\|$JVM_DEBUG_OPTS $JAVA_AGENT \\|g' /opt/striim/bin/server.sh

    # ⚠ Force the JDK's own JAXP provider. This image ships Oracle's xmlparserv2-21.1.0.0.jar,
    # which registers itself as the DocumentBuilderFactory through META-INF/services and then
    # needs oracle.i18n.util.LocaleMapper -- a class in Oracle's orai18n-mapping.jar, which
    # ships with the full Oracle client and is in NO jar here (the bundled orai18n-21.6.0.0.jar
    # carries only the character converters). Any component that parses XML through JAXP
    # therefore dies with NoClassDefFoundError. The Teradata JDBC driver does exactly that at
    # connect time, parsing its TDGSS config, so JdbcSink could not open a Teradata
    # connection at all until this was set (measured 2026-09-22). Forcing the JDK parser fixes
    # it without adding the missing jar, which Oracle does not publish to Maven Central.
    sed -i "s|#!/bin/bash|#!/bin/bash\n\nJAXP_OPTS=\"-Djavax.xml.parsers.DocumentBuilderFactory=com.sun.org.apache.xerces.internal.jaxp.DocumentBuilderFactoryImpl\"|g" /opt/striim/bin/server.sh
    # Anchored on $JAVA_AGENT, not $JVM_DEBUG_OPTS: the sed above has already rewritten that
    # text, so the original anchor no longer exists in the file.
    sed -i 's|$JVM_DEBUG_OPTS $JAVA_AGENT \\|$JVM_DEBUG_OPTS $JAVA_AGENT $JAXP_OPTS \\|g' /opt/striim/bin/server.sh

    share_keystore "$(keystore_path /opt/striim/conf sks)" /opt/striim/conf/sksKey.pwd
    cp /opt/striim/conf/startUp.properties /shared/
    cp /opt/striim/bin/server.sh /shared/

    echo ""
    echo "#"
    echo "# PRIMARY"
    echo "#"
    echo ""

    echo "Starting primary dbms"
    setsid nohup /opt/striim/sbin/striim-dbms start > /opt/striim/logs/striim-dbms-start.log &
    DBMS_GROUP=$!; STRIIM_GROUPS+=("$!")
    while ! nc -z ${PRIMARY_HOSTNAME} 1527 < /dev/null; do
        echo "Waiting for primary dbms to start"
        pause 1
    done

    echo "Starting primary node"
    setsid nohup /opt/striim/sbin/striim-node start > /opt/striim/logs/striim-node-start.log &
    STRIIM_GROUPS+=("$!")

    # The AGENT keystore (aksConfig) must be generated where a live node answers on
    # localhost:9081 (aksConfig has no server-address option). Do it here on the
    # primary once the node is up, then share it so agents can reuse it.
    echo "Waiting for primary node to be up before generating agent keystore"
    while ! nc -z ${PRIMARY_HOSTNAME} 9080 < /dev/null; do pause 5; done
    pause 15
    echo "Configuring agent keystore (aksConfig) and sharing it"
    fg_group /opt/striim/agent/bin/aksConfig.sh -p striim -k striim
    share_keystore "$(keystore_path /opt/striim/agent/conf aks)" /opt/striim/agent/conf/aksKey.pwd 2>/dev/null \
        || echo "WARN: agent keystore not produced"

    follow /opt/striim/logs/striim-node.log

elif [ "${ROLE}" == "node" ]; then

    while ! nc -z ${PRIMARY_HOSTNAME} 9080 < /dev/null; do
        echo "Waiting for primary node to start"
        pause 10
    done
    wait_keystore /shared sks

    cp "$(keystore_path /shared sks "$KEYSTORE_EPOCH")" /opt/striim/conf/
    cp /shared/sksKey.pwd /opt/striim/conf/
    cp /shared/startUp.properties /opt/striim/conf/
    cp /shared/server.sh /opt/striim/bin/

    show_props /opt/striim/conf/startUp.properties

    echo ""
    echo "#"
    echo "# NODE"
    echo "#"
    echo ""

    echo "Starting node"
    setsid nohup /opt/striim/sbin/striim-node start > /opt/striim/logs/striim-node-start.log &
    STRIIM_GROUPS+=("$!")
    pause 5
    follow /opt/striim/logs/striim-node.log

elif [ "${ROLE}" == "agent" ]; then

    while ! nc -z ${PRIMARY_HOSTNAME} 9080 < /dev/null; do
        echo "Waiting for primary node to start"
        pause 10
    done
    wait_keystore /shared aks

    # Reuse the agent keystore generated + shared by the primary.
    cp "$(keystore_path /shared aks "$KEYSTORE_EPOCH")" /opt/striim/agent/conf/
    cp /shared/aksKey.pwd /opt/striim/agent/conf/

    cp /opt/striim/agent/conf/agent.conf.orig /opt/striim/agent/conf/agent.conf
    sed -i "s|striim.cluster.clusterName=|striim.cluster.clusterName=${CLUSTER_NAME}|g" /opt/striim/agent/conf/agent.conf
    sed -i "s|striim.node.servernode.address=|striim.node.servernode.address=${PRIMARY_HOSTNAME}|g" /opt/striim/agent/conf/agent.conf
    sed -i "s|MEM_MAX=1024m|MEM_MAX=${MEM_MAX}|g" /opt/striim/agent/conf/agent.conf
    cat /opt/striim/agent/conf/agent.conf

    cp /opt/striim/agent/bin/agent.sh.orig /opt/striim/agent/bin/agent.sh
    sed -i "s|#!/bin/bash|#!/bin/bash\n\nJAVA_AGENT=\"-javaagent:/opt/striim/jmx/jmx_prometheus_javaagent-0.16.1.jar=7071:/opt/striim/jmx/jmx_prometheus_config.yaml\"|g" /opt/striim/agent/bin/agent.sh
    sed -i 's|$JAVA_SYSTEM_PROPERTIES \\|$JAVA_SYSTEM_PROPERTIES $JAVA_AGENT \\|g' /opt/striim/agent/bin/agent.sh

    echo ""
    echo "#"
    echo "# AGENT"
    echo "#"
    echo ""

    echo "Starting agent"
    setsid nohup /opt/striim/agent/sbin/striim-agent start > /opt/striim/agent/logs/striim-agent-start.log 2>&1 &
    AGENT_PID=$!; STRIIM_GROUPS+=("$!")

    # WAIT for the agent's log, do not assume it after a fixed sleep.
    #
    # `tail` is this container's last command, so PID 1's exit status IS tail's. The previous
    # form was `sleep 5; tail -f striim.agent.log` -- and striim.agent.log is created by the
    # JVM itself, measured at ~3.5s after launch on an idle host. Under load that crosses 5s,
    # tail exits 1 with "cannot open ... No such file", and the container dies reporting a
    # failure that is really just a slow start. It then succeeds on a plain `docker start`,
    # which makes it read as flakiness rather than as a fixed-timeout bug -- and it cost real
    # time being misdiagnosed as JVM heap exhaustion.
    #
    # sbin/striim-agent runs bin/agent.sh in the FOREGROUND (no & inside it), so $! stays alive
    # for as long as the JVM does and `kill -0` is a true liveness check.
    AGENT_LOG=/opt/striim/agent/logs/striim.agent.log
    AGENT_SYS_LOG=/opt/striim/agent/logs/striim-agent-system.log
    WAIT_LIMIT=${AGENT_LOG_WAIT_SECONDS:-120}
    waited=0
    while [ ! -f "${AGENT_LOG}" ]; do
        if ! kill -0 "${AGENT_PID}" 2>/dev/null; then
            # The JVM really did fail. Show ITS output rather than tail's complaint about a
            # missing file: agent.sh sends stdout+stderr to striim-agent-system.log, which is
            # where a failure like "Could not reserve enough space for object heap" appears.
            echo "AGENT FAILED TO START: the process exited before creating ${AGENT_LOG}."
            echo "--- striim-agent-system.log ---"
            cat "${AGENT_SYS_LOG}" 2>/dev/null
            echo "--- striim-agent-start.log ---"
            cat /opt/striim/agent/logs/striim-agent-start.log 2>/dev/null
            exit 1
        fi
        if [ "${waited}" -ge "${WAIT_LIMIT}" ]; then
            echo "AGENT FAILED TO START: still running after ${WAIT_LIMIT}s but ${AGENT_LOG}"
            echo "was never created. Raise AGENT_LOG_WAIT_SECONDS if this host is just slow."
            echo "--- striim-agent-system.log ---"
            cat "${AGENT_SYS_LOG}" 2>/dev/null
            exit 1
        fi
        pause 1
        waited=$((waited + 1))
    done
    echo "Agent log appeared after ${waited}s; following it"
    follow "${AGENT_LOG}"

fi
