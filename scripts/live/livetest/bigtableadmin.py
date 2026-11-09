"""Bigtable admin + data client -- the Bigtable counterpart to PgAdmin/OraAdmin/
SpannerAdmin, on the google-cloud-bigtable client (google-cloud-bigtable 2.50.x, which also
ships the table-admin API). It talks
to the CBTEMULATOR -- gcloud's Cloud Bigtable emulator -- at the dsn's `emulator_host`,
over an insecure channel without auth. No service-account key is generated (unlike
Spanner/GCS).

Unlike every other admin, Bigtable has NO SQL. So instead of run_sql/run_statements this
admin builds the table + its column families + GC rules from a spec, writes reads rows via
cells, and drops a row range. Reads render each cell as its raw value's hex string, a
JSON-comparable form (no manifest assertion reads Bigtable yet: this is groundwork), and
apply `CellsColumnLimitFilter(num_cells=1)` so one row yields its newest cell per
(family, qualifier) column, i.e. Bigtable's own "latest version" semantics.

Bigtable change streams are not supported here: nothing in the framework reads one from the
emulator, so this admin offers NO change-stream surface and `enable_change_stream`/
`disable_change_stream` raise rather than fake one. Whether a fake is acceptable belongs to a
product owner; the framework does not ship one.

The client reaches the emulator at {emulator_host} (a "host:port" string). The framework
reaches it at the base host:port (localhost:9470) on this side, while the Striim app
reaches it through the JVM-global BIGTABLE_EMULATOR_HOST (the striim overlay sets it).
The admin is injectable for hermetic tests via its __init__ client/data_client knobs.

SDK surface used (google-cloud-bigtable 2.50.2): the table-admin client's create_table/
get_table/delete_table/list_tables/drop_row_range, and the data `BigtableDataClient`'s
`.get_table(I, T)` → a `Table` with `.mutate_row(key, [SetCell(...)])`, `.read_row(key)` and
`.read_rows(query)`. `SetCell(family, qualifier, new_value, timestamp_micros)`; a read cell
exposes `.family` (str), `.qualifier` (bytes), `.value` (bytes). Bigtable tables default to
millisecond timestamp granularity, so timestamps are millis-aligned.
"""

from __future__ import annotations
import os
import re
import threading
import time

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]*$")    # a Bigtable identifier (table / family / col)
_GC_KINDS = frozenset({"num_versions", "age_millis", "age_seconds", "age_days"})
_MILLIS = 1000
_MILLIS_PER_DAY = 86_400 * 1000
# The "really prove readiness" gate: create + delete this throwaway table so a half-booted
# CBTEMULATOR (listener up, store not accepting admin RPCs yet) is caught rather than wedged.
HEALTH_TABLE = ".__slt_readiness"

# Cache of Clients by (project, emulator_host) -- the same fd-leak hazard as SpannerAdmin's:
# a client + every Table it hands out own gRPC channels that are never closed, and each burns
# fds. Keyed so a long SLT_KEEP_SERVICES session reuses one channel pair instead of leaking.
_CLIENTS: dict = {}
_CLIENTS_LOCK = threading.Lock()


# One monotonic budget per operation (see _Budget). The SDK's own defaults are far longer -- the
# data client allows a read or mutation 600s -- and they would not stop at a case's deadline.
OPERATION_TIMEOUT_S = 30.0   # one admin operation, however many RPCs it makes
READINESS_TIMEOUT_S = 60.0   # the readiness gate, all of its attempts together


class _Budget:
    """One monotonic deadline for one operation. Each RPC gets what is left of it: a table-admin
    call as its gRPC timeout with no retry (a retry could outlive the budget), a data call as its
    operation_timeout (the data client retries only within that). Once the budget is spent, the
    next RPC is not sent: TimeoutError is raised instead."""

    def __init__(self, seconds, what: str):
        self.seconds = OPERATION_TIMEOUT_S if seconds is None else float(seconds)
        self.what = what
        self._end = time.monotonic() + self.seconds

    def left(self) -> float:
        return self._end - time.monotonic()

    def _remaining(self) -> float:
        left = self.left()
        if left <= 0:
            raise TimeoutError(f"Bigtable {self.what} exceeded its {self.seconds:g}s budget")
        return left

    def admin(self) -> dict:
        return {"timeout": self._remaining(), "retry": None}

    def data(self) -> dict:
        return {"operation_timeout": self._remaining()}


def _check_ident(name, what: str = "name") -> str:
    if not isinstance(name, str) or not _IDENT.fullmatch(name):
        raise ValueError(f"unsafe {what}: {name!r} must match {_IDENT.pattern}")
    return name


def _gc_to_rule(gc):
    """Turn a YAML/JSON GC rule dict into a `GcRule` proto. Exactly one key, two shapes:
        {"num_versions": N}  -> GcRule(max_num_versions=N): keep the newest N versions
        {"age_millis": M} / {"age_seconds": S} / {"age_days": D}
            -> the union of max_age and max_num_versions=1: keep one version, and collect it
               once it is older than the age
    GcRule's `rule` is a oneof, so the two limits of an age rule are combined explicitly.
    None / empty -> None (the family has no GC rule; it never expires)."""
    if not gc:
        return None
    from google.cloud.bigtable_admin_v2.types import GcRule
    from google.protobuf.duration_pb2 import Duration
    keys = set(gc)
    if not keys <= _GC_KINDS:
        raise ValueError(f"unknown GC rule keys {sorted(keys - _GC_KINDS)!r}; "
                         f"allowed: {sorted(_GC_KINDS)}")
    if len(keys) != 1:
        raise ValueError(f"a GC rule must set exactly one of {sorted(_GC_KINDS)}, got {sorted(keys)}")
    (kind,) = keys
    if kind == "num_versions":
        return GcRule(max_num_versions=int(gc[kind]))
    scale = {"age_millis": 1, "age_seconds": _MILLIS, "age_days": _MILLIS_PER_DAY}[kind]
    age = Duration()
    age.FromMilliseconds(int(gc[kind]) * scale)
    return GcRule(union=GcRule.Union(rules=[GcRule(max_age=age), GcRule(max_num_versions=1)]))


def _as_bytes(v):
    # A row key or value may be str (utf-8), bytes, or a hex token "0xAB" / "ABCD".
    if isinstance(v, bytes):
        return v
    if isinstance(v, str):
        if v.startswith("0x"):
            return bytes.fromhex(v[2:])
        return v.encode()
    raise TypeError(f"unexpected key/value type {type(v).__name__}")


def _require_emulator(dsn: dict) -> None:
    """Refuse anything but a CBTEMULATOR. Real Bigtable is not supported yet: it would need
    explicit credential binding and the ${TID}/ownership checks real Spanner has before any
    destructive call, and without them create_table's drop-first could delete a real table."""
    if dsn.get("credentials"):
        raise ValueError("BigtableAdmin is emulator-only: real Bigtable credentials are not "
                         "supported; remove BIGTABLE_CREDENTIALS and use the bigtable service")
    if not (dsn.get("emulator_host") or "").strip():
        raise ValueError("BigtableAdmin is emulator-only: the dsn names no emulator_host")


class _EmulatorTableAdmin:
    """The admin side of one emulator: `.table_admin_client`, as on the SDK's own admin Client."""

    def __init__(self, table_admin_client):
        self.table_admin_client = table_admin_client


class _EmulatorDataClient:
    """The data side of one emulator, on the SDK's GAPIC `BigtableClient` with an explicit channel.

    The SDK's `BigtableDataClient` can reach an emulator only through BIGTABLE_EMULATOR_HOST, read
    in its constructor and shared by every client in the process, so it is not used. This exposes
    the part of its surface BigtableAdmin uses -- `.get_table(I, T)` -> a table with mutate_row /
    read_row / read_rows taking the SDK's own SetCell, DeleteAllFromRow and ReadRowsQuery and
    returning its Row/Cell -- and sends each call as one bigtable_v2 RPC bounded by
    `operation_timeout` (no retry, as on the admin side)."""

    def __init__(self, project: str, gapic_client):
        self.project = project
        self._gapic_client = gapic_client

    def get_table(self, instance_id: str, table_id: str):
        return _EmulatorTable(self._gapic_client,
                              f"projects/{self.project}/instances/{instance_id}/tables/{table_id}")


class _EmulatorTable:
    def __init__(self, gapic_client, table_name: str):
        self._gapic = gapic_client
        self.table_name = table_name

    @staticmethod
    def _mutation(m):
        from google.cloud.bigtable.data import DeleteAllFromRow, SetCell
        from google.cloud.bigtable_v2.types import Mutation
        if isinstance(m, SetCell):
            return Mutation(set_cell=Mutation.SetCell(
                family_name=m.family, column_qualifier=m.qualifier, timestamp_micros=m.timestamp_micros,
                value=m.new_value))
        if isinstance(m, DeleteAllFromRow):
            return Mutation(delete_from_row=Mutation.DeleteFromRow())
        raise TypeError(f"unsupported Bigtable mutation {type(m).__name__}")

    def mutate_row(self, row_key: bytes, mutations, *, operation_timeout: float):
        from google.cloud.bigtable_v2.types import MutateRowRequest
        self._gapic.mutate_row(
            request=MutateRowRequest(table_name=self.table_name, row_key=row_key,
                                     mutations=[self._mutation(m) for m in mutations]),
            timeout=operation_timeout, retry=None)

    def _read(self, request, operation_timeout: float):
        return list(_rows_from_chunks(self._gapic.read_rows(request=request, timeout=operation_timeout,
                                                            retry=None)))

    def read_row(self, row_key: bytes, *, operation_timeout: float):
        from google.cloud.bigtable_v2.types import ReadRowsRequest, RowSet
        rows = self._read(ReadRowsRequest(table_name=self.table_name, rows=RowSet(row_keys=[row_key]),
                                          rows_limit=1), operation_timeout)
        return rows[0] if rows else None

    def read_rows(self, query, *, operation_timeout: float):
        from google.cloud.bigtable.data.row_filters import CellsColumnLimitFilter
        from google.cloud.bigtable_v2.types import ReadRowsRequest, RowFilter
        if query.row_keys or query.row_ranges or query.limit:
            raise TypeError("only a whole-table ReadRowsQuery is supported")
        request = ReadRowsRequest(table_name=self.table_name)
        if isinstance(query.filter, CellsColumnLimitFilter):
            request.filter = RowFilter(cells_per_column_limit_filter=query.filter.num_cells)
        elif query.filter is not None:
            raise TypeError(f"unsupported Bigtable row filter {type(query.filter).__name__}")
        return self._read(request, operation_timeout)


def _rows_from_chunks(responses):
    """Merge a ReadRows response stream's cell chunks into the SDK's own Row/Cell objects.

    A row starts with a chunk naming its row_key (a later chunk may restate that key, but not
    name another before the row ends); family_name and qualifier carry over until a
    chunk names new ones; a value split across chunks carries value_size on every chunk but its
    last; reset_row discards the row so far; commit_row ends it."""
    from google.cloud.bigtable.data import Cell, Row
    from google.cloud.bigtable_v2.types import ReadRowsResponse
    key = family = qualifier = None
    cells, value, timestamp, labels = [], None, 0, []
    for response in responses:
        if isinstance(response, ReadRowsResponse):
            response = ReadRowsResponse.pb(response)
        for chunk in response.chunks:
            if chunk.reset_row:
                key = family = qualifier = None
                cells, value = [], None
                continue
            if chunk.row_key:
                if key is None:                    # the first chunk of a row
                    key, cells = chunk.row_key, []
                elif chunk.row_key != key:
                    raise ValueError("ReadRows chunk names a new row before the open row was "
                                     "committed or reset")
            if key is None:
                raise ValueError("ReadRows chunk outside a row")
            if chunk.HasField("family_name"):
                family = chunk.family_name.value
            if chunk.HasField("qualifier"):
                qualifier = chunk.qualifier.value
            if value is None:                      # the first chunk of a cell
                value, timestamp, labels = [], chunk.timestamp_micros, list(chunk.labels)
            value.append(chunk.value)
            if chunk.value_size == 0:              # its last chunk
                cells.append(Cell(b"".join(value), key, family, qualifier, timestamp, labels))
                value = None
            if chunk.commit_row:
                if value is not None:
                    raise ValueError("ReadRows committed a row inside a split cell value")
                yield Row(key, cells)
                key = family = qualifier = None
                cells = []
    if key is not None or value is not None:
        raise ValueError("ReadRows stream ended inside a row")


def _emulator_clients(project: str, endpoint: str):
    """An (admin, data) pair that dials `endpoint` and nothing else. Call with _CLIENTS_LOCK held.

    Both sides are the SDK's GAPIC clients on their own insecure channel to `endpoint` -- what the
    SDK builds for an emulator, minus its BIGTABLE_EMULATOR_HOST lookup. Nothing here reads or
    writes the process environment, so another client in the process cannot redirect these and
    these cannot redirect it; and with a transport supplied, no credentials are looked up."""
    import grpc
    from google.cloud.bigtable_admin_v2 import BaseBigtableTableAdminClient
    from google.cloud.bigtable_admin_v2.services.bigtable_table_admin.transports import (
        BigtableTableAdminGrpcTransport)
    from google.cloud.bigtable_v2 import BigtableClient
    from google.cloud.bigtable_v2.services.bigtable.transports import BigtableGrpcTransport
    admin = _EmulatorTableAdmin(BaseBigtableTableAdminClient(transport=BigtableTableAdminGrpcTransport(
        channel=grpc.insecure_channel(endpoint), host=endpoint)))
    data = _EmulatorDataClient(project, BigtableClient(transport=BigtableGrpcTransport(
        channel=grpc.insecure_channel(endpoint), host=endpoint)))
    return admin, data


class BigtableAdmin:
    """One admin bound to one project + instance. Inject client/data_client to test hermetically.

    dsn shape: {project, instance, emulator_host}. Emulator-only: see _require_emulator."""

    def __init__(self, dsn: dict, client=None, data_client=None):
        _require_emulator(dsn)
        self.dsn = dsn
        self._inj_admin = client   # injectable table-admin-capable Client (admin="instance")
        self._inj_data = data_client  # injectable BigtableDataClient
        self._table_cache = {}       # memoized data-client Tables by table name

    def _pair(self):
        if self._inj_admin is not None:
            return self._inj_admin, self._inj_data
        emulator = self.dsn["emulator_host"]
        key = (self.dsn["project"], emulator)
        with _CLIENTS_LOCK:
            if key not in _CLIENTS:
                _CLIENTS[key] = _emulator_clients(self.dsn["project"], emulator)
            return _CLIENTS[key]

    def _admin(self):
        if self._inj_admin is not None:
            return self._inj_admin
        admin, _ = self._pair()
        return admin

    def _data(self):
        if self._inj_data is not None:
            return self._inj_data
        _, data = self._pair()
        return data

    def _table(self, table_name: str):
        name = _check_ident(table_name, "table")
        cached = self._table_cache.get(name)
        if cached is not None:
            return cached
        t = self._data().get_table(self.dsn["instance"], name)
        self._table_cache[name] = t
        return t

    def _parent_instance(self) -> str:
        return f"projects/{self.dsn['project']}/instances/{self.dsn['instance']}"

    def _full_table_name(self, table_name: str) -> str:
        return f"{self._parent_instance()}/tables/{table_name}"

    # -- service bootstrap ---------------------------------------------------------
    # The admin client retries this loop until the CBTEMULATOR is accepting admin RPCs; it is
    # the readiness gate the provision step once across xdist workers.
    def readiness(self, attempts: int = 20, delay: float = 1.5,
                  timeout: float = READINESS_TIMEOUT_S) -> None:
        budget = _Budget(timeout, "readiness")
        from google.cloud.bigtable_admin_v2.types import ColumnFamily, Table, CreateTableRequest
        admin = self._admin()
        p = self._parent_instance()
        full = self._full_table_name(HEALTH_TABLE)
        from google.api_core import exceptions as _gx
        last = None
        for _ in range(attempts):
            try:
                tac = admin.table_admin_client
                # Creating the throwaway proves the store accepts admin RPCs. `AlreadyExists`
                # (our own async delete from a prior gate not yet settled) is ALSO that proof,
                # so it counts as success; the delete is best-effort async-consistent.
                try:
                    tac.create_table(CreateTableRequest(parent=p, table_id=HEALTH_TABLE,
                                                        table=Table(name=full,
                                                                    column_families={"slt": ColumnFamily()})),
                                    **budget.admin())
                except _gx.AlreadyExists:
                    pass
                try:
                    tac.delete_table(name=full, **budget.admin())
                except _gx.NotFound:    # noqa: BLE001 -- a delete that already settled is fine
                    pass
                self._table_cache.pop(HEALTH_TABLE, None)
                return
            except Exception as exc:    # noqa: BLE001 -- a half-booted emulator; retry the whole gate
                last = exc
                if budget.left() <= 0:
                    break
                time.sleep(min(delay, budget.left()))
        raise RuntimeError(f"CBTEMULATOR not ready within {budget.seconds:g}s for "
                           f"{self.dsn['project']}/{self.dsn['instance']}: {last!r}")

    # `ensure()` is the readiness gate; create_table + write_cells build a table.
    def ensure(self, *, attempts: int = 20, delay: float = 1.5,
               timeout: float = READINESS_TIMEOUT_S) -> None:
        self.readiness(attempts=attempts, delay=delay, timeout=timeout)

    # -- schema --------------------------------------------------------------------
    @staticmethod
    def _column_families(families: dict, gc_rules: dict):
        from google.cloud.bigtable_admin_v2.types import ColumnFamily, GcRule  # noqa: F401 (GcRule via _gc_to_rule)
        out = {}
        for fam, gc in dict(families or {}).items():
            _check_ident(fam, "family")
            gcrule = _gc_to_rule(gc_rules.get(fam, gc))
            cf = ColumnFamily()
            if gcrule is not None:
                cf.gc_rule = gcrule
            out[fam] = cf
        return out

    def create_table(self, table_name: str, families: dict, *, gc_rules=None,
                     timeout: float | None = None) -> None:
        """Create `table_name` with the given families + GC rules.
        Idempotent: `drop_table(...)` first -- a re-run of a case rebuilds its table."""
        name = _check_ident(table_name, "table")
        admin = self._admin()
        tac = admin.table_admin_client
        p = self._parent_instance()
        full = self._full_table_name(name)
        budget = _Budget(timeout, f"create_table {name}")
        try:                      # drop-first keeps a re-run clean; a missing table is the common case
            tac.delete_table(name=full, **budget.admin())
        except Exception:         # noqa: BLE001
            pass
        from google.cloud.bigtable_admin_v2.types import ColumnFamily, Table, CreateTableRequest
        cf = self._column_families(families, gc_rules or {})
        tac.create_table(CreateTableRequest(parent=p, table_id=name,
                                            table=Table(name=full, column_families=cf)),
                         **budget.admin())
        self._table_cache.pop(name, None)

    def drop_table(self, table_name: str, *, timeout: float | None = None) -> None:
        name = _check_ident(table_name, "table")
        budget = _Budget(timeout, f"drop_table {name}")
        self._table_cache.pop(name, None)
        admin = self._admin()
        tac = admin.table_admin_client
        from google.api_core.exceptions import NotFound
        try:               # an already-missing table is the only failure that means "gone"
            tac.delete_table(name=self._full_table_name(name), **budget.admin())
        except NotFound:
            pass

    def list_families(self, table_name: str, *, timeout: float | None = None) -> list:
        name = _check_ident(table_name, "table")
        budget = _Budget(timeout, f"list_families {name}")
        admin = self._admin()
        tac = admin.table_admin_client
        from google.cloud.bigtable_admin_v2.types import GetTableRequest
        tbl = tac.get_table(GetTableRequest(name=self._full_table_name(name)), **budget.admin())
        return list(tbl.column_families.keys())

    # -- rows (cells) --------------------------------------------------------------
    def write_cells(self, table_name: str, rows: list, *, timeout: float | None = None) -> None:
        """rows: a list of `{"row": key, "cells": {"<family>:<qualifier>": value}}`.
        value may be str (UTF-8 encoded), bytes, or a hex token "0xAB...".
        Each cell is written via `SetCell(family, qualifier, new_value,
        timestamp_micros=<now, millis-aligned>)` -- CBTEMULATOR enforces MILLIS granularity."""
        from google.cloud.bigtable.data import SetCell
        table = self._table(table_name)
        budget = _Budget(timeout, f"write_cells {table_name}")   # every row's mutation together
        now = int(time.time() * _MILLIS) * _MILLIS
        for row in rows:
            key = _as_bytes(row["row"])
            muts = []
            for col, value in row.get("cells", {}).items():
                fam, _, qual = col.partition(":")
                _check_ident(fam, "family")
                _check_ident(qual, "qualifier")
                muts.append(SetCell(family=fam,
                                    qualifier=_as_bytes(qual),
                                    new_value=_as_bytes(value),
                                    timestamp_micros=now))
            table.mutate_row(key, muts, **budget.data())

    # The task/CLI call this `write_rows`; keep the alias.
    write_rows = write_cells

    def read_row(self, table_name: str, key, *, timeout: float | None = None) -> dict:
        """Read one row by key; return `{"cells": {"<family>:<qualifier>": <hex value>}}` or
        None if the row is absent."""
        table = self._table(table_name)
        row = table.read_row(_as_bytes(key), **_Budget(timeout, f"read_row {table_name}").data())
        if row is None:
            return None
        cells = {}
        for c in row.cells:
            cells[f"{c.family}:{c.qualifier.decode()}"] = c.value.hex()
        return {"cells": cells}

    def read_all(self, table_name: str, *, cells_per_column_limit=1,
                 timeout: float | None = None) -> list:
        """Read every row in `table_name`; return a list of
        `{"row": <hex key>, "cells": {"<family>:<qualifier>": <hex value>}}` sorted by row
        key. `CellsColumnLimitFilter(num_cells=1)` makes each column return its newest cell --
        Bigtable's own "latest version" semantics."""
        from google.cloud.bigtable.data import ReadRowsQuery
        from google.cloud.bigtable.data.row_filters import CellsColumnLimitFilter
        table = self._table(table_name)
        q = ReadRowsQuery(row_filter=CellsColumnLimitFilter(num_cells=cells_per_column_limit))
        out = []
        for r in table.read_rows(q, **_Budget(timeout, f"read_all {table_name}").data()):
            cells = {}
            for c in r.cells:
                cells[f"{c.family}:{c.qualifier.decode()}"] = c.value.hex()
            out.append({"row": r.row_key.hex(), "cells": cells})
        out.sort(key=lambda x: x["row"])
        return out

    # Other names callers use for the same read.
    read_cells = read_all
    read_table = read_all

    def drop_all_rows(self, table_name: str, *, timeout: float | None = None) -> None:
        """Drop every row in one call via DropRowRange(delete_all_data_from_table=True)."""
        from google.cloud.bigtable_admin_v2.types import DropRowRangeRequest
        name = _check_ident(table_name, "table")
        admin = self._admin()
        tac = admin.table_admin_client
        tac.drop_row_range(DropRowRangeRequest(
            name=self._full_table_name(name), delete_all_data_from_table=True),
            **_Budget(timeout, f"drop_all_rows {name}").admin())

    def drop_row_range(self, table_name: str, *, row_key_prefix=None,
                       start_row_key=None, end_row_key=None,
                       timeout: float | None = None) -> None:
        """Drop every row whose key starts with `row_key_prefix`. DropRowRange targets only a
        key prefix or the whole table (drop_all_rows); it has no start/end form, so bounded keys
        are refused rather than sent."""
        name = _check_ident(table_name, "table")
        if start_row_key is not None or end_row_key is not None:
            raise ValueError("drop_row_range takes only row_key_prefix: DropRowRange cannot drop "
                             "a start/end key range")
        prefix = b"" if row_key_prefix is None else _as_bytes(row_key_prefix)
        if not prefix:
            raise ValueError("drop_row_range needs a non-empty row_key_prefix; "
                             "drop_all_rows empties the whole table")
        admin = self._admin()
        tac = admin.table_admin_client
        from google.cloud.bigtable_admin_v2.types import DropRowRangeRequest
        req = DropRowRangeRequest(name=self._full_table_name(name), row_key_prefix=prefix)
        tac.drop_row_range(req, **_Budget(timeout, f"drop_row_range {name}").admin())

    def delete_row(self, table_name: str, key, *, timeout: float | None = None) -> None:
        from google.cloud.bigtable.data import DeleteAllFromRow
        self._table(table_name).mutate_row(_as_bytes(key), [DeleteAllFromRow()],
                                           **_Budget(timeout, f"delete_row {table_name}").data())

    # -- change streams: NOT SUPPORTED by the CBTEMULATOR ---------------------------
    def enable_change_stream(self, *_a, **_k):
        # No fake is offered; a product owner decides whether one is acceptable.
        raise RuntimeError(
            "Bigtable change streams are not supported against the emulator: this admin offers "
            "no change-stream surface and no fake is in place.")

    disable_change_stream = enable_change_stream

    # -- ownership-ledger hooks ----------------------------------------------------
    def drop_owned_table(self, table_name: str, *, timeout: float | None = None) -> None:
        self.drop_table(table_name, timeout=timeout)

    def list_owned_tables(self, *, timeout: float | None = None) -> set:
        admin = self._admin()
        tac = admin.table_admin_client
        # A failed listing raises: an empty set would let the ledger verify a table absent
        # that it never saw deleted.
        names = set()
        for t in tac.list_tables(_list_tables_req(self._parent_instance()),
                                 **_Budget(timeout, "list_owned_tables").admin()):
            names.add(t.name.split("/")[-1])
        return names


def _list_tables_req(instance_path: str):
    from google.cloud.bigtable_admin_v2.types import ListTablesRequest
    return ListTablesRequest(parent=instance_path)


# --- CLI shim ------------------------------------------------------------------------
# `python3 -m livetest.bigtableadmin <subcommand> ...`. The integration and live tiers both
# drive these from run.sh. Subcommands:
#   setup    <families.json>     create a table with named families/GC rules (+ optional seed)
#   write    <table> <rows.json> write rows from a JSON spec
#   read     <table>            print the read_all JSON
#   drop     <table>           drop_table
#   families <table>           list_families
#   drop-all-rows <table>      drop_all_rows
def _cli(argv) -> int:
    import json
    import sys
    dsn = {
        "project": os.environ.get("BIGTABLE_PROJECT", "test-project"),
        "instance": os.environ.get("BIGTABLE_INSTANCE", "test-instance"),
        "emulator_host": os.environ.get("BIGTABLE_EMULATOR_HOST", "127.0.0.1:9470"),
    }
    admin = BigtableAdmin(dsn)
    sub = argv[0] if argv else "help"
    if sub == "setup":
        spec = json.loads(open(argv[1]).read())
        admin.create_table(spec["table"], spec.get("families", {}), gc_rules=spec.get("gc_rules"))
        for row in spec.get("seed", []):
            admin.write_cells(spec["table"], [row])
    elif sub == "write":
        admin.write_cells(argv[1], json.loads(open(argv[2]).read()))
    elif sub == "read":
        print(json.dumps(admin.read_all(argv[1]), indent=2, sort_keys=True))
    elif sub == "drop":
        admin.drop_table(argv[1])
    elif sub == "families":
        print("\n".join(admin.list_families(argv[1])))
    elif sub == "drop-all-rows":
        admin.drop_all_rows(argv[1])
    elif sub in ("help", None, "-h", "--help"):
        print(__doc__)
    else:
        print(f"unknown subcommand {sub!r}", file=sys.stderr)
        return 1
    return 0


def main(argv=None) -> int:
    import sys
    return _cli(argv if argv is not None else sys.argv[1:])


if __name__ == "__main__":
    import sys
    sys.exit(main())
