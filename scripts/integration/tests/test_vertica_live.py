"""Integration test for inttest.services.ensure_up + inttest.dbroutes against a REAL Vertica
(the vertica-source/vertica-target routes). Mirrors test_services_live.py: the call that
starts int-vertica from down is the one that tears it back down; an already-running service
(a prior test, `python -m inttest.cli start vertica`, `INT_KEEP_SERVICES`) is left running.
"""
from __future__ import annotations

import pytest
from filelock import FileLock

from inttest import paths, services
from inttest.dbroutes import query_rows, run_sql_text
from inttest.tokens import build_tokens

# Brings up Docker services: `pytest -m "not docker"` deselects this file.
pytestmark = pytest.mark.docker


@pytest.mark.integration
def test_ensure_up_then_seed_and_read_back_on_each_route(tmp_path):
    lock = FileLock(str(paths.state_dir() / ".int-compose.lock"))

    with services.ensure_up_for_test("vertica", lock):
        tokens = build_tokens(tmp_path, requires=["vertica"], parallel=True)
        assert tokens["TID"], "a parallel run gets a per-test ${TID} prefix"
        for route, schema in (("vertica-source", "qasource"), ("vertica-target", "qatarget")):
            try:
                # Unqualified names: the route's search path puts them in its own schema.
                run_sql_text(route,
                             "DROP TABLE IF EXISTS ${TID}throwaway;\n"
                             "CREATE TABLE ${TID}throwaway (id INT, name VARCHAR(20));\n"
                             "INSERT INTO ${TID}throwaway (id, name) VALUES (1, 'a;b');\n", tokens)
                assert query_rows(route, "SELECT id, name FROM ${TID}throwaway", tokens) == [[1, "a;b"]]
                assert query_rows(route, "SELECT current_user(), current_schema()", tokens) == \
                    [[schema, schema]]
            finally:
                run_sql_text(route, "DROP TABLE IF EXISTS ${TID}throwaway;", tokens)
