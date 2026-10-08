from __future__ import annotations
import textwrap
from pathlib import Path

import pytest

from livetest.ggtrail.model import SchemaError
from livetest.ggtrail.workload import WorkloadEngine
from livetest.ggtrail.yamlio import WorkloadFileError, load_workload

# Tier 1 (spec §10) -- R2 as it reaches a user: the checked-in workload.yaml grammar.
# The first test pins the REGRESSION SCAFFOLD's file verbatim, so a grammar change that
# would break regression/services/ggtrail/ggtrail-cdc-file-diff fails here first, in a
# hermetic test, rather than on the live tier.

# Verbatim copy of regression/services/ggtrail/ggtrail-cdc-file-diff/workload.yaml.
SCAFFOLD_YAML = """\
seed: 42
keys: sequential
tables:
  SCOTT.CUSTOMERS:
    columns:
      - {name: ID, type: int, pk: true}
      - {name: NAME, type: "varchar(50)"}
      - {name: TIER, type: "char(1)"}
  SCOTT.ORDERS:
    columns:
      - {name: ID, type: int, pk: true}
      - {name: CUSTOMER_ID, type: int, fk: SCOTT.CUSTOMERS.ID}
      - {name: AMOUNT, type: "decimal(10,2)"}
      - {name: PLACED_AT, type: timestamp}
  SCOTT.ORDER_ITEMS:
    columns:
      - {name: ID, type: int, pk: true}
      - {name: ORDER_ID, type: int, fk: SCOTT.ORDERS.ID}
      - {name: SKU, type: "varchar(20)"}
      - {name: QTY, type: int}
workload:
  initial_rows: {SCOTT.CUSTOMERS: 20, SCOTT.ORDERS: 40, SCOTT.ORDER_ITEMS: 60}
  ops: 100
  mix: {insert: 0.6, update: 0.3, delete: 0.1}
  table_weights: {SCOTT.CUSTOMERS: 1, SCOTT.ORDERS: 2, SCOTT.ORDER_ITEMS: 2}
  txn_ops: [1, 3]
max_records_per_file: 1000
"""

SCAFFOLD_PATH = (
    Path(__file__).resolve().parents[1]
    / "regression"
    / "services"
    / "ggtrail"
    / "ggtrail-cdc-file-diff"
    / "workload.yaml"
)


def write(tmp_path: Path, text: str, name: str = "workload.yaml") -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(text))
    return path


def _assert_scaffold(schema, spec, extras) -> None:
    assert [t.name for t in schema.tables] == [
        "SCOTT.CUSTOMERS",
        "SCOTT.ORDERS",
        "SCOTT.ORDER_ITEMS",
    ]
    customers = schema.table("SCOTT.CUSTOMERS")
    assert [(c.name, c.dtype, c.pk) for c in customers.columns] == [
        ("ID", "int", True),
        ("NAME", "varchar(50)", False),
        ("TIER", "char(1)", False),
    ]
    assert schema.table("SCOTT.ORDERS").column("CUSTOMER_ID").fk == "SCOTT.CUSTOMERS.ID"
    assert schema.table("SCOTT.ORDER_ITEMS").column("ORDER_ID").fk == "SCOTT.ORDERS.ID"

    assert spec.seed == 42
    assert spec.keys == "sequential"
    assert spec.initial_rows == {
        "SCOTT.CUSTOMERS": 20,
        "SCOTT.ORDERS": 40,
        "SCOTT.ORDER_ITEMS": 60,
    }
    assert spec.ops == 100
    assert spec.mix == {"insert": 0.6, "update": 0.3, "delete": 0.1}
    assert spec.table_weights == {
        "SCOTT.CUSTOMERS": 1.0,
        "SCOTT.ORDERS": 2.0,
        "SCOTT.ORDER_ITEMS": 2.0,
    }
    assert spec.txn_ops == (1, 3)
    assert extras["max_records_per_file"] == 1000
    schema.validate()
    WorkloadEngine(schema, spec)  # the config is runnable as parsed


def test_the_regression_scaffold_grammar_parses(tmp_path):
    _assert_scaffold(*load_workload(write(tmp_path, SCAFFOLD_YAML)))


@pytest.mark.skipif(
    not SCAFFOLD_PATH.exists(), reason="regression scaffold not in this checkout"
)
def test_the_checked_in_scaffold_file_itself_parses():
    # Guards against the verbatim copy above drifting from the real file.
    _assert_scaffold(*load_workload(SCAFFOLD_PATH))


# --- defaults ---------------------------------------------------------------------------


def test_minimal_file_defaults_everything(tmp_path):
    schema, spec, extras = load_workload(
        write(
            tmp_path,
            """
        tables:
          SCOTT.T:
            columns:
              - {name: ID, type: int, pk: true}
    """,
        )
    )
    assert [t.name for t in schema.tables] == ["SCOTT.T"]
    assert (spec.seed, spec.ops, spec.txn_ops, spec.keys) == (
        0,
        0,
        (1, 1),
        "sequential",
    )
    assert spec.mix == {"insert": 1.0}
    assert spec.initial_rows == {} and spec.table_weights == {}
    assert extras["max_records_per_file"] is None


def test_column_flags_default_to_the_simple_case(tmp_path):
    schema, _spec, _extras = load_workload(
        write(
            tmp_path,
            """
        tables:
          SCOTT.T:
            columns:
              - {name: ID, type: int, pk: true}
              - {name: NOTE, type: "varchar(5)"}
              - {name: OPT, type: "varchar(5)", nullable: true, null_rate: 0.25}
    """,
        )
    )
    note = schema.table("SCOTT.T").column("NOTE")
    opt = schema.table("SCOTT.T").column("OPT")
    assert (note.pk, note.fk, note.nullable, note.null_rate) == (
        False,
        None,
        False,
        0.0,
    )
    assert (opt.nullable, opt.null_rate) == (True, 0.25)


def test_lowercase_names_are_uppercased_everywhere(tmp_path):
    schema, spec, _extras = load_workload(
        write(
            tmp_path,
            """
        tables:
          scott.parent:
            columns: [{name: id, type: int, pk: true}]
          scott.child:
            columns:
              - {name: id, type: int, pk: true}
              - {name: pid, type: int, fk: scott.parent.id}
        workload:
          initial_rows: {scott.parent: 3}
          table_weights: {scott.child: 2}
    """,
        )
    )
    schema.validate()
    assert sorted(schema.by_name) == ["SCOTT.CHILD", "SCOTT.PARENT"]
    assert spec.initial_rows == {"SCOTT.PARENT": 3}
    assert spec.table_weights == {"SCOTT.CHILD": 2.0}


# --- key strategies -------------------------------------------------------------------------


def test_global_key_strategy(tmp_path):
    _schema, spec, _extras = load_workload(
        write(
            tmp_path,
            """
        keys: uuid
        tables:
          SCOTT.T:
            columns: [{name: ID, type: uuid, pk: true}]
    """,
        )
    )
    assert spec.keys == "uuid"


def test_per_table_key_override(tmp_path):
    _schema, spec, _extras = load_workload(
        write(
            tmp_path,
            """
        keys: {SCOTT.ORDERS: uuid}
        tables:
          SCOTT.CUSTOMERS:
            columns: [{name: ID, type: int, pk: true}]
          SCOTT.ORDERS:
            columns: [{name: ID, type: uuid, pk: true}]
    """,
        )
    )
    assert spec.keys == {"SCOTT.ORDERS": "uuid"}


def test_key_strategy_of_the_wrong_shape_is_rejected(tmp_path):
    with pytest.raises(WorkloadFileError, match="'keys' must be a strategy name"):
        load_workload(
            write(
                tmp_path,
                """
            keys: [uuid]
            tables:
              SCOTT.T:
                columns: [{name: ID, type: int, pk: true}]
        """,
            )
        )


# --- rejections -------------------------------------------------------------------------------


def test_missing_file_names_the_path(tmp_path):
    with pytest.raises(WorkloadFileError, match="workload file not found"):
        load_workload(tmp_path / "nope.yaml")


def test_empty_file_is_rejected(tmp_path):
    with pytest.raises(WorkloadFileError, match="file is empty"):
        load_workload(write(tmp_path, "\n"))


def test_invalid_yaml_is_rejected(tmp_path):
    with pytest.raises(WorkloadFileError, match="not valid YAML"):
        load_workload(write(tmp_path, "tables: [unclosed\n"))


def test_non_mapping_top_level_is_rejected(tmp_path):
    with pytest.raises(WorkloadFileError, match="top level must be a mapping"):
        load_workload(write(tmp_path, "- a\n- b\n"))


def test_missing_tables_key_is_rejected(tmp_path):
    with pytest.raises(WorkloadFileError, match="'tables' is required"):
        load_workload(write(tmp_path, "seed: 1\n"))


def test_unknown_top_level_key_is_rejected(tmp_path):
    # A silently-ignored typo would produce a plausible but wrong workload.
    with pytest.raises(WorkloadFileError, match=r"unknown key\(s\) \['tabels'\]"):
        load_workload(write(tmp_path, "tabels: {}\n"))


def test_unknown_workload_key_is_rejected(tmp_path):
    with pytest.raises(
        WorkloadFileError, match=r"workload: unknown key\(s\) \['table_weight'\]"
    ):
        load_workload(
            write(
                tmp_path,
                """
            tables:
              SCOTT.T:
                columns: [{name: ID, type: int, pk: true}]
            workload:
              table_weight: {SCOTT.T: 2}
        """,
            )
        )


def test_unknown_column_key_is_rejected(tmp_path):
    with pytest.raises(WorkloadFileError, match=r"unknown key\(s\) \['primary_key'\]"):
        load_workload(
            write(
                tmp_path,
                """
            tables:
              SCOTT.T:
                columns: [{name: ID, type: int, primary_key: true}]
        """,
            )
        )


def test_column_without_a_name_or_type_is_rejected(tmp_path):
    with pytest.raises(WorkloadFileError, match="needs a 'name'"):
        load_workload(
            write(
                tmp_path,
                """
            tables:
              SCOTT.T:
                columns: [{type: int, pk: true}]
        """,
            )
        )
    with pytest.raises(WorkloadFileError, match="needs a 'type'"):
        load_workload(
            write(
                tmp_path,
                """
            tables:
              SCOTT.T:
                columns: [{name: ID, pk: true}]
        """,
            )
        )


def test_empty_column_list_is_rejected(tmp_path):
    with pytest.raises(WorkloadFileError, match="must be a non-empty list"):
        load_workload(
            write(
                tmp_path,
                """
            tables:
              SCOTT.T:
                columns: []
        """,
            )
        )


def test_malformed_txn_ops_is_rejected(tmp_path):
    for bad in ("[1]", "[3, 1]", "[0, 2]", "5"):
        with pytest.raises(WorkloadFileError, match="txn_ops"):
            load_workload(
                write(
                    tmp_path,
                    f"""
                tables:
                  SCOTT.T:
                    columns: [{{name: ID, type: int, pk: true}}]
                workload:
                  txn_ops: {bad}
            """,
                )
            )


def test_non_numeric_counts_are_rejected(tmp_path):
    with pytest.raises(WorkloadFileError, match="workload.initial_rows.SCOTT.T"):
        load_workload(
            write(
                tmp_path,
                """
            tables:
              SCOTT.T:
                columns: [{name: ID, type: int, pk: true}]
            workload:
              initial_rows: {SCOTT.T: many}
        """,
            )
        )


def test_negative_ops_is_rejected(tmp_path):
    with pytest.raises(WorkloadFileError, match="must be >= 0"):
        load_workload(
            write(
                tmp_path,
                """
            tables:
              SCOTT.T:
                columns: [{name: ID, type: int, pk: true}]
            workload:
              ops: -5
        """,
            )
        )


def test_every_error_message_names_the_file(tmp_path):
    path = write(tmp_path, "seed: 1\n", name="my-workload.yaml")
    with pytest.raises(WorkloadFileError, match="my-workload.yaml"):
        load_workload(path)


def test_schema_errors_still_surface_from_the_loaded_schema(tmp_path):
    # yamlio parses the GRAMMAR; Schema.validate() owns referential validity, and its
    # errors must not be swallowed by the loader.
    schema, _spec, _extras = load_workload(
        write(
            tmp_path,
            """
        tables:
          SCOTT.T:
            columns: [{name: ID, type: int}]
    """,
        )
    )
    with pytest.raises(SchemaError, match="no primary-key column"):
        schema.validate()


# --- partial_updates -------------------------------------------------

def test_partial_updates_defaults_off_and_is_carried_in_extras(tmp_path):
    _, _, extras = load_workload(write(tmp_path, SCAFFOLD_YAML))
    assert extras["partial_updates"] is False
    _, _, extras = load_workload(write(tmp_path, SCAFFOLD_YAML + "partial_updates: true\n"))
    assert extras["partial_updates"] is True


def test_partial_updates_must_be_a_boolean(tmp_path):
    with pytest.raises(WorkloadFileError, match="partial_updates must be true or false"):
        load_workload(write(tmp_path, SCAFFOLD_YAML + "partial_updates: yes please\n"))


def test_partial_after_keeps_the_key_and_the_changed_columns_only(tmp_path):
    # The trail sink's projection: what partial-column logging puts on the wire. The engine's
    # updates change a random subset of the mutable units, so the present-set varies per op.
    from livetest.ggtrail.runner import partial_after

    schema, spec, _ = load_workload(write(tmp_path, SCAFFOLD_YAML + "partial_updates: true\n"))
    engine = WorkloadEngine(schema, spec)
    updates = [op for op in engine.ops() if op.kind == "update"]
    assert updates, "the scaffold mix produces updates"
    widths = set()
    for op in updates:
        table = schema.table(op.table)
        after = partial_after(schema, op)
        assert set(n for n in table.pk_names) <= set(after), "the key is always present"
        for name, value in after.items():
            col = next(c for c in table.columns if c.name == name)
            assert col.pk or op.before[name] != op.after[name], f"{name} did not change"
        for c in table.columns:
            if not c.pk and op.before[c.name] != op.after[c.name]:
                assert c.name in after, f"changed column {c.name} was dropped"
        widths.add(len(after))
    assert len(widths) > 1, "the present-set varies across updates"
