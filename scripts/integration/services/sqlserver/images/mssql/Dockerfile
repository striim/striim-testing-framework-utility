# int-mssql: SQL Server 2022 with the qasource/qatarget provisioning baked into the
# entrypoint (see entrypoint.sh + init.sql), so `docker compose up` alone leaves the data
# users present. No separate init service -- a one-shot under `up --wait` is flaky.
#
# Mirrors scripts/live/services/mssql/images/mssql, with one deliberate difference: NO CDC.
# The integration tier never reads change data (same reasoning as services/postgres, which
# uses base postgres:16 rather than the live tier's wal2json build), so init.sql skips
# sp_cdc_enable_db and compose.yaml leaves SQL Server Agent off. That removes the Agent's
# capture/cleanup jobs from a container this tier starts and stops constantly.
FROM mcr.microsoft.com/mssql/server:2022-latest
COPY init.sql /usr/config/init.sql
COPY entrypoint.sh /usr/config/entrypoint.sh
USER root
RUN chmod +x /usr/config/entrypoint.sh
USER mssql
ENTRYPOINT ["/usr/config/entrypoint.sh"]
CMD ["/opt/mssql/bin/sqlservr"]
