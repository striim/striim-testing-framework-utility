"""BigtableAdmin and the bigtable routes against a REAL Bigtable emulator, as test_spanner_live.py
does for Spanner: the service starts, its healthcheck passes, the readiness gate passes, and each
admin call does what the hermetic tests (scripts/live/tests/test_bigtableadmin*.py) expect of it.

The `bigtable_admin` fixture brings the service up and runs its readiness gate. Table names are
derived from `test_id` and dropped at the end of each test.
"""
from __future__ import annotations

import hashlib
import time

import pytest

from inttest.dbroutes import assert_target, read_cells, setup_bigtable, teardown_bigtable
from inttest.tokens import build_tokens

pytestmark = [pytest.mark.bigtable, pytest.mark.docker, pytest.mark.integration]


@pytest.fixture
def table(bigtable_admin, test_id):
    # A Bigtable table ID is at most 50 characters; the slugged node id can be longer.
    name = "t" + hashlib.sha1(test_id.encode()).hexdigest()[:12] + "_rows"
    yield name
    bigtable_admin.drop_table(name)


def test_create_write_read_and_drop(bigtable_admin, table):
    admin = bigtable_admin
    admin.create_table(table, {"cf": None, "meta": None}, gc_rules={"cf": {"num_versions": 2}})
    assert sorted(admin.list_families(table)) == ["cf", "meta"]

    admin.write_cells(table, [{"row": "r1", "cells": {"cf:name": "al", "meta:0xff": b"\x00"}},
                              {"row": "r2", "cells": {"cf:name": "bob"}}])
    time.sleep(0.002)                                  # a later millisecond: the newer version
    admin.write_cells(table, [{"row": "r1", "cells": {"cf:name": "alice"}}])

    # Each read shows the newest version of each column; a non-UTF-8 qualifier reads as 0x<hex>.
    assert admin.read_row(table, "r1") == {"cells": {"cf:name": b"alice".hex(), "meta:0xff": "00"}}
    assert admin.read_all(table) == [
        {"row": b"r1".hex(), "cells": {"cf:name": b"alice".hex(), "meta:0xff": "00"}},
        {"row": b"r2".hex(), "cells": {"cf:name": b"bob".hex()}},
    ]

    admin.drop_row_range(table, row_key_prefix="r1")
    assert [r["row"] for r in admin.read_all(table)] == [b"r2".hex()]
    admin.drop_all_rows(table)
    assert admin.read_all(table) == []

    admin.drop_table(table)
    assert table not in admin.list_owned_tables()


def test_age_rule_is_accepted_by_the_emulator(bigtable_admin, table):
    bigtable_admin.create_table(table, {"cf": None}, gc_rules={"cf": {"age_days": 7}})
    assert bigtable_admin.list_families(table) == ["cf"]


def test_routes_seed_read_and_assert_end_to_end(bigtable_admin, table, tmp_path):
    tokens = build_tokens(tmp_path, requires=["bigtable"], parallel=False)
    seed = [{"row": "01", "cells": {"emp:name": "ann"}}]
    setup_bigtable(table, {"emp": None}, seed=seed, route="bigtable-target", tokens=tokens)
    expected = [{"row": b"01".hex(), "cells": {"emp:name": b"ann".hex()}}]
    assert read_cells("bigtable-target", table, tokens) == {"table": table, "rows": expected}
    assert_target("bigtable-target", table, expected, tokens)
    with pytest.raises(AssertionError, match="mismatch"):
        assert_target("bigtable-target", table, expected + expected, tokens)
    teardown_bigtable(table, route="bigtable-target", tokens=tokens)
    assert table not in bigtable_admin.list_owned_tables()
