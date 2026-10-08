# syntax=docker/dockerfile:1.4
# Live-tier Oracle image: gvenzl/oracle-free with ARCHIVELOG (the LogMiner
# prerequisite) + the Striim CDC/data setup baked in AT BUILD TIME (see bake.sql).
#
# Why build-time: enabling archivelog requires a MOUNT-mode restart. At RUNTIME that
# fights gvenzl's PID-1 entrypoint — it re-orchestrates startup when the DB shuts down
# and collides with the manual restart (ORA-65048/ORA-12805 -> container crash). During
# `docker build` there is no entrypoint monitor and we own the single foreground
# sqlplus, so the restart is safe; the archivelog-enabled control file + the
# c##striim/qasource/qatarget users persist in the image layer. Runtime just opens the
# already-configured DB — no restart, no crash.
FROM gvenzl/oracle-free:23.26.2-slim-faststart

COPY bake.sql /tmp/bake.sql
RUN set -eux; sqlplus -s -L / as sysdba @/tmp/bake.sql
