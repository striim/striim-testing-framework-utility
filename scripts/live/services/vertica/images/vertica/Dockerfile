# slt-vertica: Vertica 25.3 Community Edition with the qasource/qatarget provisioning baked
# into the entrypoint (see entrypoint.sh + init.sql), so `docker compose up` alone leaves the
# data users present. No separate init service (a one-shot under `up --wait` is flaky).
#
# The base is OpenText's Kubernetes image, the only public Vertica image left. It ships no
# OS user for its files (owned by 998:996) and no entrypoint: the Kubernetes operator supplies
# both. This file adds the user; entrypoint.sh does the operator's part. 25.3 is the newest
# release that still runs unlicensed as CE -- 26.1+ refuse (scripts/live/services/vertica/README.md).
FROM opentext/vertica-k8s:25.3.0-8-multiarch
USER root
RUN groupadd -g 996 verticadba \
 && useradd -u 998 -g 996 -m -d /home/dbadmin -s /bin/bash dbadmin \
 && mkdir -p /data && chown dbadmin:verticadba /data
COPY init.sql /usr/config/init.sql
COPY entrypoint.sh /usr/config/entrypoint.sh
RUN chmod +x /usr/config/entrypoint.sh
USER dbadmin
ENTRYPOINT ["/usr/config/entrypoint.sh"]
