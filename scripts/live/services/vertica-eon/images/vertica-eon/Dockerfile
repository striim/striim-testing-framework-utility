# slt-vertica-eon: Vertica 25.3 CE in Eon mode, communal storage on S3-compatible MinIO. Same base
# and user as services/vertica; entrypoint.sh plays the Kubernetes operator for two nodes.
# BASE is overridable for an offline build from a local image built FROM the same base
# (SLT_VERTICA_EON_BASE=slt-vertica:25.3.0-8).
ARG BASE=opentext/vertica-k8s:25.3.0-8-multiarch
FROM ${BASE}
USER root
RUN (getent group verticadba || groupadd -g 996 verticadba) \
 && (id dbadmin || useradd -u 998 -g 996 -m -d /home/dbadmin -s /bin/bash dbadmin) \
 && mkdir -p /data /shared && chown dbadmin:verticadba /data /shared
COPY init.sql /usr/config/init.sql
COPY entrypoint.sh s3-mkbucket.py /usr/config/
RUN chmod +x /usr/config/entrypoint.sh /usr/config/s3-mkbucket.py
# The base copies Vertica's JSON startup progress to PID 1's stdout; keep it in the node's own log.
ENV VERTICA_STARTUP_LOG_DUPLICATE=/dev/null
USER dbadmin
ENTRYPOINT ["/usr/config/entrypoint.sh"]
