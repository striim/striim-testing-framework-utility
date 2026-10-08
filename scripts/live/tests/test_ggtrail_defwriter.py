from __future__ import annotations
from pathlib import Path
from livetest.ggtrail.defwriter import ColumnSpec, TableSchema, write_def_file, ASCII_V


def test_writes_parseable_single_table_def(tmp_path):
    schema = TableSchema(name="SCOTT.WIDGETS", columns=[
        ColumnSpec(name="ID", gg_type=ASCII_V, ext_length=10, is_key=True),
        ColumnSpec(name="NAME", gg_type=ASCII_V, ext_length=50),
    ])
    out = tmp_path / "widgets.def"
    write_def_file(out, [schema])
    text = out.read_text()
    assert "Definition for table SCOTT.WIDGETS" in text
    assert "Columns: 2" in text
    assert "End of definition" in text
    # Column line: NAME<ws>datatype<ws>extlen<ws>fetchoffset<ws>scale<ws>level<ws>null<ws>...
    id_line = next(l for l in text.splitlines() if l.startswith("ID "))
    fields = id_line.split()
    assert fields[0] == "ID"
    assert fields[1] == "64"     # gg_type
    assert fields[2] == "10"     # ext_length
    assert fields[5] == "0"      # NULL_POS=5 -> nullable flag (0 = not null since is_key)


def test_writes_all_tables_into_one_dictionary(tmp_path):
    # The multi-table TrailFileWriter (spec §12.1) writes ONE schema.def for every
    # table it knows about -- write_def_file already takes a list.
    tables = [
        TableSchema("SCOTT.CUSTOMERS", [ColumnSpec("ID", ASCII_V, 10, is_key=True)]),
        TableSchema("SCOTT.ORDERS", [ColumnSpec("ID", ASCII_V, 10, is_key=True),
                                     ColumnSpec("CUST_ID", ASCII_V, 10)]),
    ]
    out = tmp_path / "schema.def"
    write_def_file(out, tables)
    text = out.read_text()
    assert "Definition for table SCOTT.CUSTOMERS" in text
    assert "Definition for table SCOTT.ORDERS" in text
    assert text.count("End of definition") == 2
