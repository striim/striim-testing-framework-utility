from __future__ import annotations
import re
from dataclasses import dataclass, field

from .defwriter import ColumnSpec, TableSchema, ASCII_F, ASCII_V, SINT64

# Layer 1 logical schema (spec §4). The user declares *logical* tables/columns/types; this
# module validates the referential shape of that declaration and maps it onto the Layer 0
# wire types. Nothing here knows about trail bytes -- wire_spec() is the single seam, so
# pinning DATETIME later (spec D3) is a one-line change per logical type.

_NAME_RE = re.compile(r"^[A-Z0-9_.]+$")

# Logical type -> (gg_type, ext_length, scale) for the parameterless types. Parameterized
# types (decimal/varchar/char) are computed in wire_spec from their parsed arguments.
# int's ext_length 19 = the decimal width of a 64-bit signed integer; date/timestamp are
# ASCII_V strings of their rendered width (spec §4.1, decision D3).
_SIMPLE_WIRE = {
    "int": (SINT64, 19, 0),
    "uuid": (ASCII_V, 36, 0),
    "date": (ASCII_V, 10, 0),
    "timestamp": (ASCII_V, 19, 0),
}
_PARAM_ARITY = {"decimal": 2, "varchar": 1, "char": 1}

DTYPES = tuple(sorted(set(_SIMPLE_WIRE) | set(_PARAM_ARITY)))


class SchemaError(ValueError):
    """A logical schema that cannot produce a referentially consistent workload."""


@dataclass
class Column:
    name: str
    dtype: str
    pk: bool = False
    fk: str | None = (
        None  # "SCHEMA.TABLE.COLUMN" -- must name a PK column of the parent
    )
    nullable: bool = False
    null_rate: float = 0.0  # only consulted when nullable

    def __post_init__(self) -> None:
        self.name = str(self.name).upper()
        self.dtype = str(self.dtype).strip().lower()
        if self.fk is not None:
            self.fk = str(self.fk).upper()

    @property
    def parent_table(self) -> str | None:
        # "SCOTT.CUSTOMERS.ID" -> "SCOTT.CUSTOMERS"; the parent COLUMN is the last segment.
        return None if self.fk is None else self.fk.rsplit(".", 1)[0]

    @property
    def parent_column(self) -> str | None:
        return None if self.fk is None else self.fk.rsplit(".", 1)[1]


@dataclass
class Table:
    name: str  # "SCHEMA.TABLE", uppercased
    columns: list[Column] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.name = str(self.name).upper()

    @property
    def pk_columns(self) -> list[Column]:
        return [c for c in self.columns if c.pk]

    @property
    def pk_names(self) -> list[str]:
        return [c.name for c in self.columns if c.pk]

    def column(self, name: str) -> Column:
        for c in self.columns:
            if c.name == name.upper():
                return c
        raise KeyError(f"{self.name} has no column {name!r}")

    def fk_groups(self) -> dict[str, list[Column]]:
        # FK columns grouped by parent table. A composite FK is several columns pointing at
        # the same parent; they must be sampled together from ONE parent row (sampling them
        # independently would synthesize a PK tuple that never existed -- R4).
        groups: dict[str, list[Column]] = {}
        for c in self.columns:
            if c.fk is not None:
                groups.setdefault(c.parent_table, []).append(c)
        return groups


@dataclass
class Schema:
    tables: list[Table] = field(default_factory=list)

    @property
    def by_name(self) -> dict[str, Table]:
        return {t.name: t for t in self.tables}

    def table(self, name: str) -> Table:
        try:
            return self.by_name[name.upper()]
        except KeyError:
            raise SchemaError(
                f"unknown table {name!r}; schema has {sorted(self.by_name)}"
            ) from None

    def validate(self) -> Schema:
        """Reject any schema that cannot yield a referentially consistent stream (spec §4).

        Every rule here exists because violating it would make some downstream invariant
        unenforceable rather than merely awkward -- so this is the single gate the engine
        relies on and never re-checks at op time.
        """
        by_name = {}
        for t in self.tables:
            if not _NAME_RE.match(t.name):
                raise SchemaError(
                    f"table name {t.name!r} must match {_NAME_RE.pattern} (after uppercasing)"
                )
            if t.name in by_name:
                raise SchemaError(f"duplicate table {t.name!r}")
            if not t.columns:
                raise SchemaError(f"table {t.name} has no columns")
            by_name[t.name] = t

            seen_cols = set()
            for c in t.columns:
                if not _NAME_RE.match(c.name):
                    raise SchemaError(
                        f"{t.name}.{c.name!r}: column name must match {_NAME_RE.pattern} (after uppercasing)"
                    )
                if c.name in seen_cols:
                    raise SchemaError(f"{t.name}: duplicate column {c.name!r}")
                seen_cols.add(c.name)
                parse_dtype(c.dtype, where=f"{t.name}.{c.name}")
                if c.pk and c.nullable:
                    raise SchemaError(
                        f"{t.name}.{c.name}: a PK column cannot be nullable"
                    )
                if c.pk and c.fk is not None:
                    # A PK that is also an FK would have to come from BOTH the keygen (which
                    # guarantees never-reissued uniqueness, R3) and the parent's live set
                    # (which guarantees nothing about uniqueness). v1 refuses rather than
                    # silently breaking one of the two.
                    raise SchemaError(
                        f"{t.name}.{c.name}: a column that is both pk and fk is out of scope in v1"
                    )
                if c.nullable and not (0.0 <= c.null_rate <= 1.0):
                    raise SchemaError(
                        f"{t.name}.{c.name}: null_rate must be in [0,1], got {c.null_rate}"
                    )
            if not t.pk_columns:
                raise SchemaError(
                    f"table {t.name} has no primary-key column (need >=1)"
                )

        for t in self.tables:
            for parent_name, cols in t.fk_groups().items():
                if parent_name == t.name:
                    raise SchemaError(
                        f"{t.name}: self-referencing foreign key {cols[0].fk!r} is out of scope in v1"
                    )
                parent = by_name.get(parent_name)
                if parent is None:
                    raise SchemaError(
                        f"{t.name}.{cols[0].name}: foreign key {cols[0].fk!r} references unknown table "
                        f"{parent_name!r}"
                    )
                referenced = [c.parent_column for c in cols]
                if len(set(referenced)) != len(referenced):
                    raise SchemaError(
                        f"{t.name}: foreign key to {parent_name} references column "
                        f"{sorted(referenced)} more than once"
                    )
                unknown = [
                    r for r in referenced if r not in {c.name for c in parent.columns}
                ]
                if unknown:
                    raise SchemaError(
                        f"{t.name}: foreign key references unknown column(s) "
                        f"{sorted(unknown)} of {parent_name}"
                    )
                if set(referenced) != set(parent.pk_names):
                    # A partial-PK FK cannot identify one parent ROW, so refcounting (and
                    # therefore the delete guard, R4/R8) has nothing to hang on.
                    raise SchemaError(
                        f"{t.name}: foreign key to {parent_name} must cover the parent's full primary key "
                        f"{sorted(parent.pk_names)}, got {sorted(referenced)}"
                    )
                for c in cols:
                    child_kind = parse_dtype(c.dtype)[0]
                    parent_kind = parse_dtype(parent.column(c.parent_column).dtype)[0]
                    if child_kind != parent_kind:
                        raise SchemaError(
                            f"{t.name}.{c.name}: foreign key type {c.dtype!r} does not match "
                            f"{c.fk} type {parent.column(c.parent_column).dtype!r}"
                        )

        self.topo_order()  # raises on a cycle
        return self

    def topo_order(self) -> list[Table]:
        """Parents before children -- the order Phase A loads in, so FK sampling always has
        candidates (R5), and the order schema.def declares tables in."""
        by_name = self.by_name
        state: dict[str, int] = {}  # 0 = visiting, 1 = done
        order: list[Table] = []

        def visit(name: str, trail: list[str]) -> None:
            if state.get(name) == 1:
                return
            if state.get(name) == 0:
                cycle = " -> ".join(trail + [name])
                raise SchemaError(f"foreign-key cycle is out of scope in v1: {cycle}")
            state[name] = 0
            table = by_name[name]
            for parent_name in table.fk_groups():
                if parent_name in by_name:
                    visit(parent_name, trail + [name])
            state[name] = 1
            order.append(table)

        for t in self.tables:
            visit(t.name, [])
        return order


def parse_dtype(dtype: str, where: str = "") -> tuple[str, tuple[int, ...]]:
    """ "decimal(10,2)" -> ("decimal", (10, 2)); "int" -> ("int", ())."""
    ctx = f"{where}: " if where else ""
    text = str(dtype).strip().lower()
    base, _, rest = text.partition("(")
    base = base.strip()
    if not rest:
        if base not in _SIMPLE_WIRE:
            if base in _PARAM_ARITY:
                raise SchemaError(
                    f"{ctx}type {dtype!r} needs {_PARAM_ARITY[base]} argument(s), "
                    f"e.g. {base}(10)"
                    if _PARAM_ARITY[base] == 1
                    else f"{ctx}type {dtype!r} needs 2 arguments, e.g. decimal(10,2)"
                )
            raise SchemaError(
                f"{ctx}unknown logical type {dtype!r}; known types: {list(DTYPES)}"
            )
        return base, ()
    if base not in _PARAM_ARITY:
        raise SchemaError(
            f"{ctx}logical type {base!r} takes no arguments, got {dtype!r}"
        )
    if not rest.rstrip().endswith(")"):
        raise SchemaError(
            f"{ctx}malformed logical type {dtype!r} (unbalanced parentheses)"
        )
    raw_args = [
        a.strip() for a in rest.rstrip().rstrip(")").split(",") if a.strip() != ""
    ]
    try:
        args = tuple(int(a) for a in raw_args)
    except ValueError:
        raise SchemaError(
            f"{ctx}logical type {dtype!r} takes integer arguments"
        ) from None
    if len(args) != _PARAM_ARITY[base]:
        raise SchemaError(
            f"{ctx}logical type {base!r} takes {_PARAM_ARITY[base]} argument(s), got {dtype!r}"
        )
    if any(a <= 0 for a in args[:1]) or any(a < 0 for a in args):
        raise SchemaError(f"{ctx}logical type {dtype!r} arguments must be positive")
    if base == "decimal":
        precision, scale = args
        if scale > precision:
            raise SchemaError(
                f"{ctx}decimal scale {scale} exceeds precision {precision} in {dtype!r}"
            )
    return base, args


def wire_spec(col: Column) -> ColumnSpec:
    """Logical column -> Layer 0 ColumnSpec (spec §4.1). The ONE place logical types meet
    the wire, so DATETIME(192) can be adopted per-type without touching the engine."""
    base, args = parse_dtype(col.dtype, where=col.name)
    if base in _SIMPLE_WIRE:
        gg_type, ext_length, scale = _SIMPLE_WIRE[base]
    elif base == "decimal":
        precision, scale = args
        # NUMBER(p,s) rides SINT64: ext_length is the precision, and the encoder scales the
        # logical value by 10**scale (verified against rt000002's AUTH_AMOUNT).
        gg_type, ext_length = SINT64, precision
    elif base == "varchar":
        gg_type, ext_length, scale = ASCII_V, args[0], 0
    elif base == "char":
        gg_type, ext_length, scale = ASCII_F, args[0], 0
    else:  # pragma: no cover - parse_dtype gates this
        raise SchemaError(f"no wire mapping for logical type {col.dtype!r}")
    return ColumnSpec(
        name=col.name,
        gg_type=gg_type,
        ext_length=ext_length,
        scale=scale,
        is_key=col.pk,
    )


def table_schema(table: Table) -> TableSchema:
    """Layer 0 TableSchema for one logical table (column order preserved -- the trail and
    the .def agree positionally, and GGTrailParser's `data[i]` indexes this order)."""
    return TableSchema(name=table.name, columns=[wire_spec(c) for c in table.columns])


def wire_schemas(schema: Schema) -> list[TableSchema]:
    return [table_schema(t) for t in schema.topo_order()]
