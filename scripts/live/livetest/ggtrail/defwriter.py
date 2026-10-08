from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path

# GG type codes actually seen in TestData/data/sample.def and confirmed against
# GGTypeMapper.java:45-70 / ColumnType.java:6-17. SINT64 is characterized (Evidence §D:
# span = [u16 null-ind][8-byte BE signed int], colLen 10, value scaled by 10^-scale) and
# IS wired into record.py's column encoder. DATETIME is exported for forward use but its
# ASCII wire format is still unpinned -- record.py raises NotImplementedError for it
# (see Evidence appendix §F and the spec's D3: declare date columns as ASCII_V for now).
ASCII_F = 0     # fixed-length CHAR
ASCII_V = 64    # variable-length VARCHAR2/CHAR/CLOB/BLOB/LONG/ROWID/FLOAT
SINT64 = 134    # NUMBER(p,s)
DATETIME = 192  # DATE/TIMESTAMP


@dataclass
class ColumnSpec:
    name: str
    gg_type: int
    ext_length: int
    scale: int = 0
    is_key: bool = False
    sub_type: int = 0


@dataclass
class TableSchema:
    name: str
    columns: list[ColumnSpec] = field(default_factory=list)


def _column_line(col: ColumnSpec, fetch_offset: int) -> str:
    # Field order matches SourceDefinitions.java's numeric-array indices (0-based,
    # SourceDefinitions.java:75-82): 0=DataType,1=ExtLength,2=FetchOffset,3=Scale,
    # 4=Level,5=Null,6=BumpIfOdd,7=InternalLength,8=BinaryLength,9=TableLength,
    # 10=MSD,11=LSD,12=HighPrec,13=LowPrec,14=ElementaryItem,15=Occurs,
    # 16=KeyColumn,17=SubDataType. Only indices 0,3,5,7,16,17(,18,19) are ever read
    # by the parser (Evidence §F) -- the rest are placeholders GGTrailParser ignores.
    null_flag = 0 if col.is_key else 1
    internal_len = col.ext_length
    return (
        f"{col.name:<16}{col.gg_type:>6}{col.ext_length:>7}{fetch_offset:>9}"
        f"{col.scale:>3}{0:>3}{null_flag:>2}{0:>2}{internal_len:>7}{internal_len:>7}"
        f"{0:>7}{0:>2}{0:>2}{0:>2}{0:>2}{1:>2}{0:>5}{(1 if col.is_key else 0):>2}{col.sub_type:>2}"
    )


def write_def_file(path: Path, tables: list[TableSchema], database_type: str = "ORACLE") -> None:
    lines = [
        "*+- Defgen version 2.0, Encoding UTF-8",
        "*",
        f"Database type: {database_type}",
        "Character set ID: UTF-8",
        "*",
    ]
    for table in tables:
        record_length = sum(c.ext_length for c in table.columns)
        lines.append(f"Definition for table {table.name}")
        lines.append(f"Record length: {record_length}")
        lines.append("Syskey: 0")
        lines.append(f"Columns: {len(table.columns)}")
        fetch_offset = 0
        for col in table.columns:
            lines.append(_column_line(col, fetch_offset))
            fetch_offset += col.ext_length
        lines.append("End of definition")
        lines.append("*")
    path.write_text("\n".join(lines) + "\n")
