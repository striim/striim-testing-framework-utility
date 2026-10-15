"""Bounded REST-only administration of the local BigQuery emulator.

Each operation runs in a disposable child. Killing that child enforces a total deadline
even for a peer that trickles bytes forever. Cancellation is best effort, never rollback.
The capped transport runs before the SDK decodes JSON, including for a single giant row.
"""
from __future__ import annotations

import base64
import datetime as dt
import ipaddress
import json
import re
import subprocess
import sys
import time
import uuid
from decimal import Decimal
from urllib.parse import urlsplit


class BigQueryError(Exception):
    def __init__(self, code, detail):
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}")


def local_http(value):
    p = urlsplit(value)
    if p.scheme != "http" or p.username or p.password or p.query or p.fragment:
        raise ValueError("BigQuery emulator endpoints must be plain local HTTP URLs without credentials")
    if not p.hostname or p.path not in ("", "/"):
        raise ValueError("BigQuery emulator endpoint must name a host, without an API path")
    if p.hostname not in ("localhost", "host.docker.internal"):
        address = ipaddress.ip_address(p.hostname)
        if address.version != 4 or not (address.is_private or address.is_loopback) or address.is_unspecified or address.is_link_local or address.is_multicast or address.is_reserved:
            raise ValueError("BigQuery emulator endpoint must resolve inside the local test stack")
    if p.port is None:
        raise ValueError("BigQuery emulator endpoint must include its test port")
    return value.rstrip("/")


def identifiers(project, dataset):
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", project):
        raise ValueError("invalid BigQuery test project identifier")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,1023}", dataset):
        raise ValueError("invalid BigQuery test dataset identifier")


def table_ref(raw, project, dataset):
    """Fully qualified, case-preserving identity; only the configured dataset is reachable."""
    identifiers(project, dataset)
    parts = raw.split(".")
    if len(parts) == 1:
        parts = [project, dataset, *parts]
    elif len(parts) == 2:
        parts = [project, *parts]
    if len(parts) != 3 or parts[:2] != [project, dataset]:
        raise ValueError("BigQuery table must belong to the configured project and dataset")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,1023}", parts[2]):
        raise ValueError("invalid BigQuery table identifier")
    return ".".join(parts)


SCALARS = {"INTEGER", "INT64", "NUMERIC", "DECIMAL", "TIMESTAMP", "DATETIME", "DATE",
           "BYTES", "BOOLEAN", "BOOL", "STRING"}


def decode_rows(schema, rows):
    fields = schema.get("fields", [])
    names = [f["name"] for f in fields]
    if len(set(names)) != len(names):
        raise BigQueryError("duplicate-column", "duplicate BigQuery result column")
    for f in fields:
        if f.get("type") not in SCALARS or f.get("mode") == "REPEATED" or f.get("fields"):
            raise BigQueryError("exact-type-unsupported", f"{f['name']}: {f.get('type')}/{f.get('mode')}")

    def cell(field, value):
        if value is None:
            return None
        kind = field["type"]
        if kind in ("INT64", "INTEGER", "NUMERIC", "DECIMAL", "TIMESTAMP") and not isinstance(value, str):
            raise BigQueryError("invalid-value", "numeric REST value must be an exact string")
        if kind in ("INT64", "INTEGER"):
            return int(value)
        if kind in ("NUMERIC", "DECIMAL"):
            return Decimal(value)
        if kind == "TIMESTAMP":
            if not re.fullmatch(r"-?[0-9]+", value):
                raise BigQueryError("invalid-value", "TIMESTAMP REST value must be integer microseconds")
            return dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(microseconds=int(value))
        if kind == "DATETIME":
            result = dt.datetime.fromisoformat(value)
            if result.tzinfo:
                raise BigQueryError("invalid-value", "DATETIME has a timezone")
            return result
        if kind == "DATE":
            return dt.date.fromisoformat(value)
        if kind == "BYTES":
            return base64.b64decode(value, validate=True)
        if kind in ("BOOL", "BOOLEAN"):
            if type(value) is bool:
                return value
            if value not in ("true", "false"):
                raise BigQueryError("invalid-value", "invalid BOOL")
            return value == "true"
        return value

    out = []
    for row in rows:
        values = row["f"]
        if len(values) != len(fields):
            raise BigQueryError("invalid-value", "result width differs from schema")
        out.append({f["name"]: cell(f, v.get("v")) for f, v in zip(fields, values)})
    return out


class BigQueryAdmin:
    def __init__(self, base):
        self.project, self.dataset = base["project"], base["dataset"]
        identifiers(self.project, self.dataset)
        self.endpoint = local_http(base.get("endpoint") or f"http://{base['host']}:{base['port']}")
        self.last_operation = None

    def ref(self, raw):
        return table_ref(raw, self.project, self.dataset)

    def _operation(self, op, remaining=10.0, **kwargs):
        if remaining <= 0:
            raise BigQueryError("exact-read-timeout", "no read budget remains")
        started = time.monotonic()
        job = "slt_" + uuid.uuid4().hex
        request = dict(op=op, endpoint=self.endpoint, project=self.project, dataset=self.dataset,
                       job=job, deadline=started + remaining - min(0.5, remaining / 5), **kwargs)
        operation = dict(jobId=job, endpoint=self.endpoint, cancellation="not-needed", jobDelete="not-needed")
        self.last_operation = operation
        argv = [sys.executable, "-m", "livetest.bigqueryadmin"]
        reserve = min(0.5, remaining / 5)
        timed_out = False
        try:
            try:
                result = subprocess.run(argv, input=json.dumps(request), text=True, capture_output=True,
                                        timeout=max(0.01, remaining - reserve))
            except subprocess.TimeoutExpired:
                # The worker is killed and reaped before cleanup. Cancellation is never rollback.
                timed_out = True
                raise BigQueryError("exact-read-timeout", f"job {job}; reader killed; cancellation is not rollback") from None
            if result.returncode:
                raise BigQueryError("exact-read-error", f"job {job}: worker exited {result.returncode}: {result.stderr[:300]}")
            doc = json.loads(result.stdout)
            if "error" in doc:
                raise BigQueryError(doc["error"], f"job {job}: {doc.get('detail', '')[:500]}")
            return doc
        finally:
            if op in ("query", "setup"):
                # The emulator retains complete query results until this exact job is deleted.
                # Keep cleanup within the original deadline on success, error and killed reads.
                budget = remaining - (time.monotonic() - started)
                operation["jobDelete"] = "timed-out"
                if timed_out:
                    operation["cancellation"] = "timed-out"
                if budget > 0:
                    try:
                        cleaned = subprocess.run(argv, input=json.dumps(dict(request, op="cleanup", cancel=timed_out,
                                                                             budget=budget)), text=True,
                                                 capture_output=True, timeout=budget)
                        if cleaned.returncode == 0:
                            report = json.loads(cleaned.stdout)
                            operation.update({k: report[k] for k in ("cancellation", "jobDelete") if k in report})
                        else:
                            operation["jobDelete"] = "failed"
                    except (subprocess.TimeoutExpired, ValueError, OSError):
                        pass  # best effort; the status remains visible in read diagnostics
            operation["elapsed_s"] = time.monotonic() - started

    def ensure_setup(self, remaining=20):
        doc = self._operation("setup", remaining=remaining)
        rows = decode_rows(doc["schema"], doc["rows"])
        if len(rows) != 1 or list(rows[0].values()) != [1]:
            raise BigQueryError("exact-read-error", "query healthcheck returned unexpected rows")

    def table_present(self, table, remaining=10):
        return self._operation("table", remaining, table=self.ref(table))["present"]

    def table_schema(self, table, remaining=10):
        doc = self._operation("table", remaining, table=self.ref(table))
        if not doc["present"]:
            raise BigQueryError("exact-read-error", "table is absent")
        return doc["schema"]

    def drop_table(self, table, remaining=10):
        # Called only with a confirmed exact-name ledger entry. Never drops the dataset.
        self._operation("drop", remaining, table=self.ref(table))

    def run_sql(self, sql, remaining=30):
        from livetest.sqlutil import split_sql_statements
        deadline = time.monotonic() + remaining
        for statement in split_sql_statements(sql):
            self._operation("query", deadline - time.monotonic(), sql=statement,
                            max_rows=1000, max_bytes=1_000_000)

    def count_rows(self, table, remaining=10):
        doc = self._operation("query", remaining, sql=f"SELECT COUNT(*) AS n FROM `{self.ref(table)}`",
                              max_rows=1, max_bytes=10000)
        return int(decode_rows(doc["schema"], doc["rows"])[0]["n"])

    def select_rows(self, table, order_by=None, max_rows=10000, max_bytes=8_000_000, remaining=10):
        if (isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows <= 0
                or isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0):
            raise ValueError("BigQuery read caps must be positive integers")
        name = self.ref(table)
        for col in order_by or ():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", col):
                raise ValueError("BigQuery order_by must contain plain column names")
        order = " ORDER BY " + ",".join(f"`{c}`" for c in order_by) if order_by else ""
        doc = self._operation("query", remaining, table=name,
                              sql=f"SELECT * FROM `{name}`{order} LIMIT {int(max_rows) + 1}",
                              max_rows=max_rows, max_bytes=max_bytes)
        return decode_rows(doc["schema"], doc["rows"])


def _worker(req):
    if req["op"] == "cleanup":
        # No SDK import, proxy discovery or redirects in the reserved cleanup budget.
        import http.client
        endpoint = urlsplit(local_http(req["endpoint"]))
        identifiers(req["project"], req["dataset"])
        deadline = time.monotonic() + req["budget"]
        report = {"cancellation": "not-needed", "jobDelete": "timed-out"}
        root = f"/bigquery/v2/projects/{req['project']}/jobs/{req['job']}"
        actions = ([("POST", "/cancel", "cancellation")] if req.get("cancel") else [])
        actions.append(("DELETE", "/delete", "jobDelete"))
        for method, suffix, field in actions:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            conn = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=left)
            try:
                conn.request(method, root + suffix, body=b"{}" if method == "POST" else None,
                             headers={"Content-Type": "application/json"})
                response = conn.getresponse()
                response.read(4097)
                report[field] = "response" if response.status in (200, 204, 404) else "failed"
            except OSError:
                report[field] = "failed"
            finally:
                conn.close()
        return report
    import requests
    from google.api_core.client_options import ClientOptions
    from google.api_core.exceptions import NotFound
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import bigquery

    cap = int(req.get("max_bytes", 1_000_000)) + 65536

    class CappedSession(requests.Session):
        def __init__(self):
            super().__init__()
            self.trust_env = False
            self.spent = 0

        def request(self, method, url, **kwargs):
            if not url.startswith(req["endpoint"] + "/"):
                raise BigQueryError("exact-read-error", "refused non-emulator request")
            kwargs.update(stream=True, allow_redirects=False)
            kwargs.setdefault("headers", {})["Accept-Encoding"] = "identity"
            response = super().request(method, url, **kwargs)
            try:
                if response.is_redirect or response.headers.get("Content-Encoding", "identity") != "identity":
                    raise BigQueryError("exact-read-error", "redirect/compressed response is unsupported")
                data = response.raw.read(cap - self.spent + 1, decode_content=False)
                self.spent += len(data)
                if self.spent > cap:
                    raise BigQueryError("canonical-limit-exceeded", "cumulative REST byte cap exceeded")
                response._content = data
                response._content_consumed = True
                return response
            finally:
                response.close()

    endpoint = local_http(req["endpoint"])
    identifiers(req["project"], req["dataset"])
    client = bigquery.Client(project=req["project"], credentials=AnonymousCredentials(),
                             client_options=ClientOptions(api_endpoint=endpoint), _http=CappedSession())
    root = f"/projects/{req['project']}"
    dataset = root + f"/datasets/{req['dataset']}"

    def api(method, path, **kwargs):
        left = req.get("deadline", time.monotonic() + 10) - time.monotonic()
        if left <= 0:
            raise BigQueryError("exact-read-timeout", "REST operation budget exhausted")
        return client._connection.api_request(method=method, path=path, timeout=(min(2, left), left), **kwargs)

    if req["op"] == "setup":
        try:
            api("GET", dataset)
        except NotFound:
            api("POST", root + "/datasets", data={"datasetReference": {
                "projectId": req["project"], "datasetId": req["dataset"]}})
        req = dict(req, op="query", sql="SELECT 1", max_rows=1)
    if req["op"] in ("table", "drop"):
        name = table_ref(req["table"], req["project"], req["dataset"])
        path = dataset + "/tables/" + name.split(".")[2]
        try:
            result = api("DELETE" if req["op"] == "drop" else "GET", path)
            return {"present": req["op"] != "drop", "schema": (result or {}).get("schema")}
        except NotFound:
            return {"present": False}
    if req.get("table"):
        table = api("GET", dataset + "/tables/" + req["table"].split(".")[2])
        decode_rows(table["schema"], [])  # reject unsupported schema even for an empty result
    api("POST", root + "/jobs", data={"jobReference": {"projectId": req["project"], "jobId": req["job"]},
        "configuration": {"query": {"query": req["sql"], "useLegacySql": False}}})
    rows, schema, token, row_bytes = [], None, None, 0
    for _ in range(128):
        params = {"maxResults": min(1000, req["max_rows"] + 1 - len(rows)), "timeoutMs": 100, "formatOptions.useInt64Timestamp": "true"}
        if token:
            params["pageToken"] = token
        page = api("GET", root + f"/queries/{req['job']}", query_params=params)
        if page.get("errors"):
            raise BigQueryError("exact-read-error", str(page["errors"])[:500])
        if not page.get("jobComplete", False):
            time.sleep(0.02)
            continue
        schema = page.get("schema", schema or {"fields": []})
        batch = page.get("rows", [])
        row_bytes += len(json.dumps(batch, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if row_bytes > 2 * int(req.get("max_bytes", 1_000_000)):
            raise BigQueryError("canonical-limit-exceeded", "REST row-byte cap exceeded")
        rows.extend(batch)
        if len(rows) > req["max_rows"]:
            raise BigQueryError("canonical-limit-exceeded", "REST row cap exceeded")
        token = page.get("pageToken")
        if not token:
            return {"schema": schema, "rows": rows}
        if not isinstance(token, str) or len(token) > 4096:
            raise BigQueryError("exact-read-error", "invalid page token")
    raise BigQueryError("canonical-limit-exceeded", "REST page cap exceeded")


if __name__ == "__main__":
    try:
        print(json.dumps(_worker(json.load(sys.stdin))))
    except Exception as exc:
        print(json.dumps({"error": getattr(exc, "code", "exact-read-error") if isinstance(exc, BigQueryError)
                          else "exact-read-error", "detail": getattr(exc, "detail", str(exc))[:500]}))
