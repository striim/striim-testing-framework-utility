"""Prove a restored route transports a fresh marker before assertions."""
from livetest import lifecycle as lc
from livetest.recovery import RecoveryError
from livetest.resultschema import build_assertion_result


def run(state, spec, *, client, app, admins, tokens, ident, read_files, owned,
        source_dir, cycle, record):
    phase = f"recovery_{cycle}"
    watch = lc.Watch("sentinel", "post-recovery insert present, delete absent",
                     lc.Deadline(spec.completion_s))
    reason = "error"
    try:
        refusal = lc.check_witness_refs(spec, tokens, owned)
        if refusal:
            raise RecoveryError(refusal)
        ids = getattr(state, "sentinel_ids", None)
        if ids is None:
            ids = state.sentinel_ids = {}
        sentinel = lc.Sentinel(spec, admins, tokens, ident, phase, ids,
            lc._source_sql_reader(source_dir), record=getattr(state, "record_inputs", None),
            read_files=read_files)
        reason = sentinel.run(watch, lc._terminal_of(client, [app]))
    except Exception as e:
        reason = f"error:{e}"
    witness = watch.finish(reason, f"fresh {phase} marker observed present then absent")
    record(build_assertion_result(type="recovery", status="passed" if reason == "satisfied" else "failed",
        spec={"sentinel": True, "cycle": cycle}, target=app, db=spec.sentinel["observe"].get("db"),
        detail="ok" if reason == "satisfied" else f"post-recovery-sentinel-missed: {reason}",
        expected={"kind": "rows", "rows": [{"reason": "satisfied"}]},
        actual={"kind": "rows", "rows": [witness]}))
    if reason != "satisfied":
        raise RecoveryError(f"post-recovery-sentinel-missed: {reason}")
    lc.publish_sentinels(state, tokens, ident)
    return witness
