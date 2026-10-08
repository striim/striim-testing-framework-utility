from __future__ import annotations
import datetime
from pathlib import Path
from typing import Callable
from . import record as rec
from . import ddl as ddlmod
from .header import write_header
from .defwriter import TableSchema, write_def_file


class TrailFileWriter:
    """Ergonomic facade over ggtrail's header/record/ddl encoders -- generates a
    directory of rt0000NN trail files GGTrailReader can tail directly (no GG-side
    process or handshake required, confirmed via GGTrailReader_1_0.java's plain
    directory-wildcard file tailing).

    Multi-table (spec §12.1): the writer owns a list of TableSchema and every
    operation names its target table, so one trail directory + one schema.def can
    carry a normalized multi-table workload. Timestamps come from an injectable
    clock (spec §12.2) so a fixed seed yields byte-identical output.
    """

    def __init__(self, directory: Path, schemas: list[TableSchema], max_records_per_file: int = 1000,
                 txn_id_start: int = 1, clock: Callable[[], datetime.datetime] | None = None):
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._schemas = list(schemas)
        self._by_name = {s.name: s for s in self._schemas}
        self._max = max_records_per_file
        self._seqno = -1
        self._next_txn_id = txn_id_start
        self._count_in_file = 0
        self._fh = None
        self._clock = clock if clock is not None else (lambda: datetime.datetime.now(datetime.UTC))
        # One dictionary for ALL tables -- write_def_file already takes a list.
        write_def_file(self._dir / "schema.def", self._schemas)
        self._roll()

    def _schema_for(self, table: str) -> TableSchema:
        try:
            return self._by_name[table]
        except KeyError:
            raise KeyError(f"unknown table {table!r}; writer knows {sorted(self._by_name)}") from None

    def _roll(self) -> None:
        if self._fh is not None:
            self._fh.close()
        self._seqno += 1
        self._count_in_file = 0
        path = self._dir / f"rt0000{self._seqno:03d}"
        ts = self._clock()
        header = write_header(uri=f"uri:{self._dir}", filename=path.name, seqno=self._seqno, creation_time=ts)
        self._fh = open(path, "wb")
        self._fh.write(header)

    def _write(self, body: bytes) -> None:
        if self._count_in_file >= self._max:
            self._roll()
        self._fh.write(body)
        self._count_in_file += 1

    def _write_one(self, body: bytes) -> None:
        # A standalone (SOLE) operation consumes one transaction id.
        self._write(body)
        self._next_txn_id += 1

    def insert(self, table: str, values: dict) -> None:
        schema = self._schema_for(table)
        self._write_one(rec.write_insert(schema, values, self._next_txn_id, self._clock()))

    def delete(self, table: str, values: dict) -> None:
        schema = self._schema_for(table)
        self._write_one(rec.write_delete(schema, values, self._next_txn_id, self._clock()))

    def update(self, table: str, before: dict, after: dict) -> None:
        schema = self._schema_for(table)
        self._write_one(rec.write_update(schema, before, after, self._next_txn_id, self._clock()))

    def truncate(self, table: str) -> None:
        schema = self._schema_for(table)
        self._write_one(rec.write_truncate(schema, self._next_txn_id, self._clock()))

    def ddl(self, schema: str, object_name: str, ddl_text: str,
            catalog_object_type: str = "TABLE", operation_name: str = "ALTER") -> None:
        # DDL is not row-scoped: it names its own schema/object rather than a TableSchema.
        self._write_one(ddlmod.write_ddl(schema, object_name, ddl_text, self._next_txn_id,
                                         self._clock(), catalog_object_type=catalog_object_type,
                                         operation_name=operation_name))

    def transaction(self, ops: list) -> None:
        """Write one transaction whose ops may span tables (spec §12.3).

        Each op is `(table, "insert"|"delete", values)` or `(table, "update", before, after)`.
        All records share one txn_id; txn_part runs BEGIN/MIDDLE.../END, or SOLE when the
        transaction holds a single op -- the same framing record.write_transaction emits
        for the single-table case.
        """
        if not ops:
            return
        txn_id = self._next_txn_id
        ts = self._clock()
        last = len(ops) - 1
        for i, op in enumerate(ops):
            if len(ops) == 1:
                part = rec._TXN_PART_SOLE
            elif i == 0:
                part = rec._TXN_PART_BEGIN
            elif i == last:
                part = rec._TXN_PART_END
            else:
                part = rec._TXN_PART_MIDDLE
            table, kind = op[0], op[1]
            schema = self._schema_for(table)
            if kind == "insert":
                body = rec.write_insert(schema, op[2], txn_id, ts, txn_part=part)
            elif kind == "delete":
                body = rec.write_delete(schema, op[2], txn_id, ts, txn_part=part)
            elif kind == "update":
                body = rec.write_update(schema, op[2], op[3], txn_id, ts, txn_part=part)
            else:
                raise ValueError(f"unknown transaction op kind {kind!r} (insert|update|delete)")
            self._write(body)
        self._next_txn_id += 1

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
