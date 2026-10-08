#!/bin/sh
# Generate standard legacy timezone links (e.g. US/Pacific, US/Eastern) from tzdata.zi
# so clients connecting with legacy timezone names do not fail the PostgreSQL connection handshake.
if [ -f /usr/share/zoneinfo/tzdata.zi ] && command -v zic >/dev/null 2>&1; then
    zic /usr/share/zoneinfo/tzdata.zi
fi
