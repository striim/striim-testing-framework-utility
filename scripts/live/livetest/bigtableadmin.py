"""Bigtable admin + data client -- the Bigtable counterpart to PgAdmin/OraAdmin/
SpannerAdmin, on the google-cloud-bigtable client (google-cloud-bigtable 2.50.x,
google-cloud-bigtable-admin 2.55.x, both confirmed against the CBTEMULATOR). It talks
to the CBTEMULATOR -- gcloud's Cloud Bigtable emulator -- exactly when
`BIGTABLE_EMULATOR_HOST` is set, which also skips auth. No service-account key is
generated (unlike Spanner/GCS).

Unlike every other admin, Bigtable has NO SQL. So instead of run_sql/run_statements this
admin builds the table + its column families + GC rules from a spec, writes reads rows via
cells, and drops a row range. Reads render each cell as its raw value's hex string -- the
JSON-comparable form the `assert.target` tier compares against `expected/*.json` -- and
apply `CellsColumnLimitFilter(num_cells=1)` so one row yields its newest cell per
(family, qualifier) column, i.e. Bigtable's own "latest version" semantics.

The emulator does NOT support Bigtable Change Streams (GenerateInitialChangeStreamPartitions
and ReadChangeStream both SIGSEGV the bttest server -- see docs/BIGTABLE_EMULATOR.md
"BCR-1"), so this admin offers NO change-stream surface: `enable_change_stream`/
`disable_change_stream` raise rather than fake one. That decision belongs to a product
owner; the framework does not ship a fake.

The client reaches the emulator at {emulator_host} (a "host:port" string). The framework
reaches it at the base host:port (localhost:9470) on this side, while the Striim app
reaches it through the JVM-global BIGTABLE_EMULATOR_HOST (the striim overlay sets it).
The admin is injectable for hermetic tests via its __init__ client/data_client knobs.

Confirmed API (google-cloud-bigtable 2.50.2, Python 3.15, probe one_probe.py against the
gcloud 591.0.0 CBTEMULATOR): an admin `Client(project=P, admin="instance")` exposes
`.table_admin_client` (create_table/get_table/delete_table/list_tables/drop_row_range) and
`.instance(I).table(T)`; the data `BigtableDataClient(project=P)` exposes `.get_table(I, T)`
→ a `Table` with `.mutate_row(key, [SetCell(...)])`, `.read_row(key)`,
`.read_rows(start_key, end_key, filter_=...)`. `SetCell(family, qualifier, new_value,
timestamp_micros)`; a read cell exposes `.family` (str), `.qualifier` (bytes), `.value`
(bytes). CBTEMULATOR enforces MILLIS timestamp granularity, so timestamps are millis-aligned.
"""

from __future__ import annotations
import os
import re
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


def _check_ident(name, what: str = "name") -> str:
    if not isinstance(name, str) or not _IDENT.fullmatch(name):
        raise ValueError(f"unsafe {what}: {name!r} must match {_IDENT.pattern}")
    return name


def _gc_to_rule(gc):
    """Turn a YAML/JSON GC rule dict into a `GcRule` proto. Two supported shapes:
        {"num_versions": N}                 -> GcRule(max_num_versions=N)
        {"age_millis": M} / {"age_seconds": S} / {"age_days": D}
                                    -> GcRule(max_age=Duration(...), max_num_versions=1)
    None / empty -> None (the family has no GC rule; it never expires)."""
    if not gc:
        return None
    from google.cloud.bigtable_admin_v2.types import GcRule
    from google.protobuf.duration_pb2 import Duration
    keys = set(gc)
    if not keys <= _GC_KINDS:
        raise ValueError(f"unknown GC rule keys {sorted(keys - _GC_KINDS)!r}; "
                         f"allowed: {sorted(_GC_KINDS)}")
    if "num_versions" in keys:
        return GcRule(max_num_versions=int(gc["num_versions"]))
    if "age_millis" in keys:
        age_millis = int(gc["age_millis"])
    elif "age_seconds" in keys:
        age_millis = int(gc["age_seconds"]) * _MILLIS
    elif "age_days" in keys:
        age_millis = int(gc["age_days"]) * _MILLIS_PER_DAY
    else:
        raise ValueError("a GC rule must set exactly one of num_versions/age_*/age_days")
    return GcRule(max_age=Duration(milliseconds=age_millis, seconds=age_millis // 1000),
                  max_num_versions=1)


def _as_bytes(v):
    # A row key or value may be str (utf-8), bytes, or a hex token "0xAB" / "ABCD".
    if isinstance(v, bytes):
        return v
    if isinstance(v, str):
        if v.startswith("0x"):
            return bytes.fromhex(v[2:])
        return v.encode()
    raise TypeError(f"unexpected key/value type {type(v).__name__}")


class BigtableAdmin:
    """One admin bound to one project + instance. Inject client/data_client to test hermetically.

    dsn shape: {project, instance, emulator_host, credentials(=None optional)}."""

    def __init__(self, dsn: dict, client=None, data_client=None):
        self.dsn = dsn
        self._inj_admin = client   # injectable table-admin-capable Client (admin="instance")
        self._inj_data = data_client  # injectable BigtableDataClient
        self._table_cache = {}       # memoized data-client Tables by table name

    def _pair(self):
        if self._inj_admin is not None:
            return self._inj_admin, self._inj_data
        emulator = self.dsn.get("emulator_host")
        if emulator:
            os.environ.setdefault("BIGTABLE_EMULATOR_HOST", emulator)
        key = (self.dsn["project"], emulator)
        if key not in _CLIENTS:
            from google.cloud.bigtable.client import Client
            from google.cloud.bigtable.data import BigtableDataClient
            admin = Client(project=self.dsn["project"], admin="instance")
            data = BigtableDataClient(project=self.dsn["project"])
            _CLIENTS[key] = (admin, data)
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

    def _instance(self):
        # `admin.instance(I)` is the admin-Instance -- the entry to `table(...).create(...)`.
        return self._admin().instance(self.dsn["instance"])

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
    def readiness(self, attempts: int = 20, delay: float = 1.5) -> None:
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
                                                                    column_families={"slt": ColumnFamily()})))
                except _gx.AlreadyExists:
                    pass
                try:
                    tac.delete_table(name=full)
                except _gx.NotFound:    # noqa: BLE001 -- a delete that already settled is fine
                    pass
                self._table_cache.pop(HEALTH_TABLE, None)
                return
            except Exception as exc:    # noqa: BLE001 -- a half-booted emulator; retry the whole gate
                last = exc
                time.sleep(delay)
        raise RuntimeError(f"CBTEMULATOR not ready for {self.dsn['project']}/{self.dsn['instance']}: "
                           f"{last!r}")

    # `ensure()` is the readiness gate the provision loop calls; the smoke `setup` step calls
    # create_table + write_cells to build its table.
    def ensure(self, *, attempts: int = 20, delay: float = 1.5) -> None:
        self.readiness(attempts=attempts, delay=delay)

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

    def create_table(self, table_name: str, families: dict, *, gc_rules=None) -> None:
        """Create `table_name` with the given families + GC rules.
        Idempotent: `drop_table(...)` first -- a re-run of a case rebuilds its table."""
        name = _check_ident(table_name, "table")
        admin = self._admin()
        tac = admin.table_admin_client
        p = self._parent_instance()
        full = self._full_table_name(name)
        try:                      # drop-first keeps a re-run clean; a missing table is the common case
            tac.delete_table(name=full)
        except Exception:         # noqa: BLE001
            pass
        from google.cloud.bigtable_admin_v2.types import ColumnFamily, Table, CreateTableRequest
        cf = self._column_families(families, gc_rules or {})
        tac.create_table(CreateTableRequest(parent=p, table_id=name,
                                            table=Table(name=full, column_families=cf)))
        self._table_cache.pop(name, None)

    def drop_table(self, table_name: str) -> None:
        name = _check_ident(table_name, "table")
        self._table_cache.pop(name, None)
        admin = self._admin()
        tac = admin.table_admin_client
        try:               # best-effort teardown: a missing table no-ops
            tac.delete_table(name=self._full_table_name(name))
        except Exception:  # noqa: BLE001
            pass

    def list_families(self, table_name: str) -> list:
        name = _check_ident(table_name, "table")
        admin = self._admin()
        tac = admin.table_admin_client
        from google.cloud.bigtable_admin_v2.types import GetTableRequest
        tbl = tac.get_table(GetTableRequest(name=self._full_table_name(name)))
        return list(tbl.column_families.keys())

    # -- rows (cells) --------------------------------------------------------------
    def write_cells(self, table_name: str, rows: list) -> None:
        """rows: a list of `{"row": key, "cells": {"<family>:<qualifier>": value}}`.
        value may be str (UTF-8 encoded), bytes, or a hex token "0xAB...".
        Each cell is written via `SetCell(family, qualifier, new_value,
        timestamp_micros=<now, millis-aligned>)` -- CBTEMULATOR enforces MILLIS granularity."""
        from google.cloud.bigtable.data import SetCell
        table = self._table(table_name)
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
            table.mutate_row(key, muts)

    # The task/CLI call this `write_rows`; keep the alias.
    write_rows = write_cells

    def read_row(self, table_name: str, key) -> dict:
        """Read one row by key; return `{"cells": {"<family>:<qualifier>": <hex value>}}` or
        None if the row is absent."""
        table = self._table(table_name)
        row = table.read_row(_as_bytes(key))
        if row is None:
            return None
        cells = {}
        for c in row.cells:
            cells[f"{c.family}:{c.qualifier.decode()}"] = c.value.hex()
        return {"cells": cells}

    def read_all(self, table_name: str, *, cells_per_column_limit=1) -> list:
        """Read every row in `table_name`; return a list of
        `{"row": <hex key>, "cells": {"<family>:<qualifier>": <hex value>}}` sorted by row
        key. `CellsColumnLimitFilter(num_cells=1)` makes each column return its newest cell --
        Bigtable's own "latest version" semantics, and the form `expected/*.json` records."""
        from google.cloud.bigtable.data import ReadRowsQuery
        from google.cloud.bigtable.data.row_filters import CellsColumnLimitFilter
        table = self._table(table_name)
        q = ReadRowsQuery(row_filter=CellsColumnLimitFilter(num_cells=cells_per_column_limit))
        out = []
        for r in table.read_rows(q):
            cells = {}
            for c in r.cells:
                cells[f"{c.family}:{c.qualifier.decode()}"] = c.value.hex()
            out.append({"row": r.row_key.hex(), "cells": cells})
        out.sort(key=lambda x: x["row"])
        return out

    # Aliases for the assert.target read form.
    read_cells = read_all
    read_table = read_all

    def drop_all_rows(self, table_name: str) -> None:
        """Drop every row in one call via DropRowRange(delete_all_data_from_table=True).
        CBTEMULATOR supports this (BT-9 confirmed; see docs/BIGTABLE_EMULATOR.md)."""
        from google.cloud.bigtable_admin_v2.types import DropRowRangeRequest
        name = _check_ident(table_name, "table")
        admin = self._admin()
        tac = admin.table_admin_client
        tac.drop_row_range(DropRowRangeRequest(
            name=self._full_table_name(name), delete_all_data_from_table=True))

    def drop_row_range(self, table_name: str, *, row_key_prefix=None,
                       start_row_key=None, end_row_key=None) -> None:
        name = _check_ident(table_name, "table")
        admin = self._admin()
        tac = admin.table_admin_client
        from google.cloud.bigtable_admin_v2.types import DropRowRangeRequest
        req = DropRowRangeRequest(name=self._full_table_name(name))
        if row_key_prefix is not None:
            req.row_key_prefix = _as_bytes(row_key_prefix)
        if start_row_key is not None:
            req.start_row_key = _as_bytes(start_row_key)
        if end_row_key is not None:
            req.end_row_key = _as_bytes(end_row_key)
        tac.drop_row_range(req)

    def delete_row(self, table_name: str, key) -> None:
        from google.cloud.bigtable.data import DeleteAllFromRow
        self._table(table_name).mutate_row(_as_bytes(key), [DeleteAllFromRow()])

    # -- change streams: NOT SUPPORTED by the CBTEMULATOR ---------------------------
    def enable_change_stream(self, *_a, **_k):
        # BCR-1: the CBTEMULATOR in gcloud 591.0.0 (and earlier) does NOT support Bigtable
        # Change Streams -- GenerateInitialChangeStreamPartitions and ReadChangeStream both
        # SIGSEGV the bttest server (see docs/BIGTABLE_EMULATOR.md). No fake is offered; a
        # product owner decides if a fake is acceptable for the reader's live read tier.
        raise RuntimeError(
            "BCR-1: the CBTEMULATOR in the pinned gcloud/emulators image does not support "
            "Bigtable Change Streams -- GenerateInitialChangeStreamPartitions/ReadChangeStream "
            "SIGSEGV the bttest server. No fake is in place; a product owner must decide.")

    disable_change_stream = enable_change_stream

    # -- ownership-ledger hooks ----------------------------------------------------
    def drop_owned_table(self, table_name: str) -> None:
        self.drop_table(table_name)

    def list_owned_tables(self) -> set:
        admin = self._admin()
        tac = admin.table_admin_client
        try:
            p = self._parent_instance()
            names = set()
            for t in tac.list_tables(_list_tables_req(p)):
                names.add(t.name.split("/")[-1])
            return names
        except Exception:      # noqa: BLE001 -- best-effort teardown list
            return set()


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
