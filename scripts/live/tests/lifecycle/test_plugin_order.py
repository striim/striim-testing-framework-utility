"""The lifecycle hooks sit in the lifecycle order inside the synced plugin
(text-order check on the transformed source; the executed order is test_plugin_exec)."""
from __future__ import annotations

import ast
import dataclasses
import inspect
import textwrap

from livetest import lifecycle, plugin
from livetest.manifest import VALID_MANIFEST_KEYS
from livetest.manifest import TestManifest as _Manifest


def _positions(src, needles):
    out = []
    for n in needles:
        assert src.count(n) >= 1, f"hook text missing: {n!r}"
        out.append(src.index(n))
    return out


def test_hook_markers_full_order():
    runtest = inspect.getsource(plugin.LiveItem._runtest)
    order = [
        "ident = _slt_runident.derive(m.name, os.environ)",             # H1b identity
        "ledger = _slt_ownership.Ledger.open(ident)",                   # H1 ledger, persisted before the try
        "self._slt_lc = _slt_lifecycle.State.for_manifest(m)",          # H1 state, before the try
        "_report, _clear_progress = _make_progress(self.config)",       # the case body's try
        "ledger.reset(client=client, admins=admins, ctx=ctx",           # H6 reset own recorded objects
        "ledger.acquire_namespace(client)",                             # H6 namespace acquisition
        "ledger.run_ddl_files(m.ddl_files",                             # H6 DDL: acquire, run, confirm
        "ledger.allocate_owned_dir(ctx)",                               # H7 owned directory, before the baseline
        "_slt_lifecycle.record_baseline(self._slt_lc, m.lifecycle",     # H6b after the seed
        "ledger.claim_exact_file(ctx",                                  # H7 exact claims
        "_deploy_tql_once_retrying(",                                   # deploy
        "_slt_lifecycle.smoke_and_ready(",                              # H3 readiness
        "_post_seed = [(db, f, a)",                                     # ordered changes
        "_slt_lifecycle.complete(",                                     # H4 completion + stability
        "assert_data(",                                                 # assertions
        "cleanup = _slt_ownership.run_cleanup(",                        # H8 cleanup in the finally
        "raise _slt_ownership.CleanupError(",                           # H8 cleanup as result
    ]
    pos = _positions(runtest, order)
    assert pos == sorted(pos), list(zip(order, pos))
    function = ast.parse(textwrap.dedent(runtest)).body[0]
    # The case body's guard owns cleanup; earlier eligibility guards have no cleanup block.
    body_tries = [node for node in function.body if isinstance(node, ast.Try)
                  and any(isinstance(call, ast.Call)
                          and ast.unparse(call.func) == "_slt_ownership.run_cleanup"
                          for statement in node.finalbody for call in ast.walk(statement))]
    [body_try] = body_tries
    assert body_try.finalbody
    for marker in order[1:3]:     # ledger and lifecycle state precede the guarded case body
        line = runtest[:runtest.index(marker)].count("\n") + 1
        assert line < body_try.lineno, marker
    report = inspect.getsource(plugin.pytest_runtest_makereport)
    assert report.index("_slt_collect_report(item.config, item, rep)") < report.index("_slt_evidence.finalize_from_report(item, rep)")
    assert "declare_if_live" in inspect.getsource(plugin.pytest_collection_finish)       # H0


def test_manifest_accepts_lifecycle_key(tmp_path):
    assert "lifecycle" in VALID_MANIFEST_KEYS
    assert "lifecycle" in {f.name for f in dataclasses.fields(_Manifest)}
    (tmp_path / "app.tql").write_text("CREATE APPLICATION ${APP};\n")
    (tmp_path / "test.yaml").write_text(
        "name: lc\npurpose: p\ntql: app.tql\nrequires: [postgres]\nassert: {smoke: true}\n"
        "lifecycle:\n  version: 1\n  mode: cdc\n  sink: db\n"
        "  readiness: {kind: source-progress, db: postgres-source}\n"
        "  completion: {kind: row-count, db: postgres-target, table: \"${PG_TARGET_SCHEMA}.${TID}tgt\", expect: 1}\n")
    from livetest.manifest import load_manifest
    assert isinstance(load_manifest(tmp_path / "test.yaml").lifecycle, lifecycle.LifecycleSpec)


def test_no_whole_schema_reset_or_parent_wipe_or_fallback_sweep_remains():
    runtest = inspect.getsource(plugin.LiveItem._runtest)
    for gone in ("reset_schemas(", 'drop_test_tables("")', "teardown_ddl_tables(",
                 "clear_server_dir(", "clear_op_checkpoints(", "teardown_file_outputs("):
        assert gone not in runtest, f"runtest still calls {gone}"
    # Review of 3.1 (F1): the only prefix reset left is MySQL's, by this run's ${TID} (the ledger does not
    # own MySQL), and the only server-file clear is the block-less case's <path>* teardown after the ledger.
    assert runtest.count("reset_test_objects(") == 1 and "_mysql.reset_test_objects(tid)" in runtest
    assert runtest.count("clear_server_files(") == 1
    unowned = runtest.index("teardown_unowned(")
    assert runtest.index("_slt_ownership.run_cleanup(") < unowned
    assert runtest.index("if m.lifecycle is None and _slt_keep_reason is None:") < unowned
    assert unowned < runtest.index("clear_server_files(") < unowned + 300
    # the only namespace teardown left is the OP-poison recovery of this run's own namespace (exact name)
    assert runtest.count("client.teardown_namespace(ns)") == 1
    assert runtest.index("_sp.restart_app_nodes(client)") < runtest.index("client.teardown_namespace(ns)")


def test_final_outcome_computed_after_cleanup_before_raise():
    runtest = inspect.getsource(plugin.LiveItem._runtest)
    finally_at = runtest.index("        finally:\n", runtest.index("            succeeded = True\n"))
    cleanup_at = runtest.index("cleanup = _slt_ownership.run_cleanup(")
    record_at = runtest.index("self._slt_cleanup = cleanup.record()")
    release_at = runtest.index("_in_use.__exit__(None, None, None)\n                finally:")
    raise_at = runtest.index("raise _slt_ownership.CleanupError(")
    assert finally_at < cleanup_at < record_at < release_at < raise_at
    guard = runtest[runtest.rindex("if ", 0, raise_at):raise_at]
    assert "_slt_sys.exc_info()[0] is None" in guard and 'cleanup.status == "failed"' in guard
    from livetest import evidence
    assert "_slt_cleanup" in inspect.getsource(evidence.case_envelope)          # the report hook reads it
