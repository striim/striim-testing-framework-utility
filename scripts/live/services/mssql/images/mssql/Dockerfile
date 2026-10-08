# slt-mssql: SQL Server 2022 with the qasource/qatarget provisioning baked into the
# entrypoint (see entrypoint.sh + init.sql), so `docker compose up` alone leaves the
# data users present. No separate init service (a one-shot under `up --wait` is flaky).
FROM mcr.microsoft.com/mssql/server:2022-latest
COPY init.sql /usr/config/init.sql
COPY entrypoint.sh /usr/config/entrypoint.sh
USER root
RUN chmod +x /usr/config/entrypoint.sh
USER mssql
ENTRYPOINT ["/usr/config/entrypoint.sh"]
CMD ["/opt/mssql/bin/sqlservr"]
