from __future__ import annotations
import pytest

from livetest.ggtrail.defwriter import ASCII_F, ASCII_V, SINT64
from livetest.ggtrail.model import (
    Column,
    Schema,
    SchemaError,
    Table,
    parse_dtype,
    table_schema,
    wire_spec,
)

# Tier 1 (spec §10) -- schema validation, topo order, and the logical->wire mapping.
# Traceability: R2 (user-defined tables/columns/types/PKs/FKs) lives here, together with
# the rejections that make R4/R5 enforceable downstream.


def _col(name, dtype="int", **kw):
    return Column(name=name, dtype=dtype, **kw)


def _customers_orders() -> Schema:
    return Schema(
        [
            Table(
                "SCOTT.CUSTOMERS", [_col("ID", pk=True), _col("NAME", "varchar(50)")]
            ),
            Table(
                "SCOTT.ORDERS",
                [_col("ID", pk=True), _col("CUSTOMER_ID", fk="SCOTT.CUSTOMERS.ID")],
            ),
        ]
    )


# --- validation rejections --------------------------------------------------------------


def test_table_without_primary_key_is_rejected():
    schema = Schema([Table("SCOTT.T", [_col("A"), _col("B")])])
    with pytest.raises(SchemaError, match="no primary-key column"):
        schema.validate()


def test_dangling_foreign_key_target_table_is_rejected():
    schema = Schema(
        [
            Table(
                "SCOTT.ORDERS",
                [_col("ID", pk=True), _col("CID", fk="SCOTT.MISSING.ID")],
            )
        ]
    )
    with pytest.raises(SchemaError, match="unknown table"):
        schema.validate()


def test_foreign_key_to_unknown_column_is_rejected():
    schema = _customers_orders()
    schema.tables[1].columns[1].fk = "SCOTT.CUSTOMERS.NOPE"
    with pytest.raises(SchemaError, match="unknown column"):
        schema.validate()


def test_partial_composite_foreign_key_is_rejected():
    # The parent's identity is (REGION, ID); referencing only ID cannot name one parent ROW,
    # so refcounting -- and therefore the delete guard (R4/R8) -- would have nothing to hold.
    schema = Schema(
        [
            Table(
                "SCOTT.P", [_col("REGION", "varchar(4)", pk=True), _col("ID", pk=True)]
            ),
            Table("SCOTT.C", [_col("ID", pk=True), _col("P_ID", fk="SCOTT.P.ID")]),
        ]
    )
    with pytest.raises(SchemaError, match="must cover the parent's full primary key"):
        schema.validate()


def test_full_composite_foreign_key_is_accepted():
    schema = Schema(
        [
            Table(
                "SCOTT.P", [_col("REGION", "varchar(4)", pk=True), _col("ID", pk=True)]
            ),
            Table(
                "SCOTT.C",
                [
                    _col("ID", pk=True),
                    _col("P_REGION", "varchar(4)", fk="SCOTT.P.REGION"),
                    _col("P_ID", fk="SCOTT.P.ID"),
                ],
            ),
        ]
    )
    assert schema.validate() is schema
    groups = schema.table("SCOTT.C").fk_groups()
    assert list(groups) == ["SCOTT.P"] and len(groups["SCOTT.P"]) == 2


def test_duplicate_reference_to_the_same_parent_column_is_rejected():
    schema = Schema(
        [
            Table("SCOTT.P", [_col("ID", pk=True)]),
            Table(
                "SCOTT.C",
                [
                    _col("ID", pk=True),
                    _col("A", fk="SCOTT.P.ID"),
                    _col("B", fk="SCOTT.P.ID"),
                ],
            ),
        ]
    )
    with pytest.raises(SchemaError, match="more than once"):
        schema.validate()


def test_cyclic_foreign_keys_are_rejected():
    schema = Schema(
        [
            Table("SCOTT.A", [_col("ID", pk=True), _col("B_ID", fk="SCOTT.B.ID")]),
            Table("SCOTT.B", [_col("ID", pk=True), _col("A_ID", fk="SCOTT.A.ID")]),
        ]
    )
    with pytest.raises(SchemaError, match="cycle"):
        schema.validate()


def test_self_referencing_foreign_key_is_rejected():
    schema = Schema(
        [Table("SCOTT.T", [_col("ID", pk=True), _col("PARENT_ID", fk="SCOTT.T.ID")])]
    )
    with pytest.raises(SchemaError, match="self-referencing"):
        schema.validate()


def test_bad_table_and_column_names_are_rejected():
    with pytest.raises(SchemaError, match="table name"):
        Schema([Table("SCOTT-BAD", [_col("ID", pk=True)])]).validate()
    with pytest.raises(SchemaError, match="column name"):
        Schema([Table("SCOTT.T", [_col("ID", pk=True), _col("BAD NAME")])]).validate()


def test_duplicate_table_and_column_names_are_rejected():
    with pytest.raises(SchemaError, match="duplicate table"):
        Schema(
            [
                Table("SCOTT.T", [_col("ID", pk=True)]),
                Table("SCOTT.T", [_col("ID", pk=True)]),
            ]
        ).validate()
    with pytest.raises(SchemaError, match="duplicate column"):
        Schema([Table("SCOTT.T", [_col("ID", pk=True), _col("ID")])]).validate()


def test_pk_that_is_also_an_fk_is_rejected_in_v1():
    schema = Schema(
        [
            Table("SCOTT.P", [_col("ID", pk=True)]),
            Table("SCOTT.C", [_col("ID", pk=True, fk="SCOTT.P.ID")]),
        ]
    )
    with pytest.raises(SchemaError, match="both pk and fk"):
        schema.validate()


def test_nullable_primary_key_is_rejected():
    schema = Schema([Table("SCOTT.T", [_col("ID", pk=True, nullable=True)])])
    with pytest.raises(SchemaError, match="cannot be nullable"):
        schema.validate()


def test_foreign_key_type_must_match_its_parent():
    schema = Schema(
        [
            Table("SCOTT.P", [_col("ID", pk=True)]),
            Table(
                "SCOTT.C",
                [_col("ID", pk=True), _col("P_ID", "varchar(10)", fk="SCOTT.P.ID")],
            ),
        ]
    )
    with pytest.raises(SchemaError, match="does not match"):
        schema.validate()


def test_names_are_uppercased_so_yaml_casing_does_not_matter():
    schema = Schema(
        [
            Table("scott.customers", [Column("id", "int", pk=True)]),
            Table(
                "scott.orders",
                [
                    Column("id", "int", pk=True),
                    Column("cust", "int", fk="scott.customers.id"),
                ],
            ),
        ]
    ).validate()
    assert [t.name for t in schema.tables] == ["SCOTT.CUSTOMERS", "SCOTT.ORDERS"]
    assert schema.table("SCOTT.ORDERS").columns[1].fk == "SCOTT.CUSTOMERS.ID"


# --- topo order ---------------------------------------------------------------------------


def test_topo_order_puts_parents_before_children():
    schema = Schema(
        [
            Table(
                "SCOTT.ORDER_ITEMS",
                [_col("ID", pk=True), _col("ORDER_ID", fk="SCOTT.ORDERS.ID")],
            ),
            Table(
                "SCOTT.ORDERS",
                [_col("ID", pk=True), _col("CUSTOMER_ID", fk="SCOTT.CUSTOMERS.ID")],
            ),
            Table("SCOTT.CUSTOMERS", [_col("ID", pk=True)]),
        ]
    ).validate()
    order = [t.name for t in schema.topo_order()]
    assert (
        order.index("SCOTT.CUSTOMERS")
        < order.index("SCOTT.ORDERS")
        < order.index("SCOTT.ORDER_ITEMS")
    )


def test_topo_order_covers_every_table_exactly_once():
    schema = _customers_orders().validate()
    order = [t.name for t in schema.topo_order()]
    assert sorted(order) == sorted(t.name for t in schema.tables)
    assert len(order) == len(set(order))


def test_unrelated_tables_keep_declaration_order():
    schema = Schema(
        [
            Table("SCOTT.A", [_col("ID", pk=True)]),
            Table("SCOTT.B", [_col("ID", pk=True)]),
        ]
    ).validate()
    assert [t.name for t in schema.topo_order()] == ["SCOTT.A", "SCOTT.B"]


# --- dtype parsing ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("int", ("int", ())),
        ("uuid", ("uuid", ())),
        ("date", ("date", ())),
        ("timestamp", ("timestamp", ())),
        ("decimal(10,2)", ("decimal", (10, 2))),
        ("decimal(10, 2)", ("decimal", (10, 2))),
        ("DECIMAL(5,0)", ("decimal", (5, 0))),
        ("varchar(50)", ("varchar", (50,))),
        ("char(4)", ("char", (4,))),
        ("  int  ", ("int", ())),
    ],
)
def test_parse_dtype_accepts_the_documented_grammar(text, expected):
    assert parse_dtype(text) == expected


@pytest.mark.parametrize(
    "text,match",
    [
        ("blob", "unknown logical type"),
        ("varchar", "argument"),
        ("decimal", "argument"),
        ("int(4)", "takes no arguments"),
        ("varchar(50", "unbalanced"),
        ("varchar(abc)", "integer arguments"),
        ("decimal(2,5)", "scale 5 exceeds precision 2"),
        ("varchar(50,2)", "takes 1 argument"),
    ],
)
def test_parse_dtype_rejects_malformed_types(text, match):
    with pytest.raises(SchemaError, match=match):
        parse_dtype(text)


def test_parse_dtype_error_names_the_offending_column():
    with pytest.raises(SchemaError, match=r"SCOTT.T.AMT: unknown logical type"):
        parse_dtype("money", where="SCOTT.T.AMT")


# --- wire mapping (spec §4.1) ---------------------------------------------------------------


@pytest.mark.parametrize(
    "dtype,gg_type,ext_length,scale",
    [
        ("int", SINT64, 19, 0),
        ("decimal(10,2)", SINT64, 10, 2),
        ("decimal(18,4)", SINT64, 18, 4),
        ("varchar(50)", ASCII_V, 50, 0),
        ("char(4)", ASCII_F, 4, 0),
        ("uuid", ASCII_V, 36, 0),
        ("date", ASCII_V, 10, 0),
        ("timestamp", ASCII_V, 19, 0),
    ],
)
def test_wire_spec_maps_every_logical_type(dtype, gg_type, ext_length, scale):
    spec = wire_spec(_col("C", dtype))
    assert (spec.gg_type, spec.ext_length, spec.scale) == (gg_type, ext_length, scale)
    assert spec.name == "C"


def test_wire_spec_marks_primary_keys_as_key_columns():
    assert wire_spec(_col("ID", "int", pk=True)).is_key is True
    assert wire_spec(_col("NAME", "varchar(10)")).is_key is False


def test_table_schema_preserves_column_order():
    # GGTrailParser indexes `data[i]` positionally, so the .def and the trail must agree
    # with the declared order -- the whole ops.csv projection rests on this.
    table = Table(
        "SCOTT.T",
        [
            _col("ID", "int", pk=True),
            _col("NAME", "varchar(8)"),
            _col("AMT", "decimal(6,2)"),
        ],
    )
    wire = table_schema(table)
    assert wire.name == "SCOTT.T"
    assert [c.name for c in wire.columns] == ["ID", "NAME", "AMT"]
    assert [c.gg_type for c in wire.columns] == [SINT64, ASCII_V, SINT64]
