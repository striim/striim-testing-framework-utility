"""Lifecycle sentinel reads on typed databases and append sinks."""
from livetest import canon, exactdata, lifecycle, nothing, strictjson
from livetest.substitute import render


def validate_ref(ref, sink, path):
    def refuse(message):
        raise ValueError(f"{path}: lifecycle: {message}")
    if not isinstance(ref, dict):
        refuse("sentinel.observe must be a mapping")
    if sink == "file":
        if set(ref) != {"path", "key", "deletion"} or not isinstance(ref.get("path"), str) or not ref["path"].startswith("${OWNED_DIR}/"):
            refuse("sentinel.observe file needs exactly path under ${OWNED_DIR}, key and deletion")
    elif sink == "kafka":
        if set(ref) - {"db", "topic", "key", "deletion", "payload"} or not {"db", "topic", "key", "deletion"} <= set(ref) or ref["db"] != "kafka":
            refuse("sentinel.observe Kafka needs db: kafka, topic, key and deletion")
        if ref.get("payload", "avro") not in ("json", "avro") or not isinstance(ref["topic"], str):
            refuse("sentinel.observe Kafka payload must be avro or json")
    else:
        if set(ref) != {"db", "table", "key"}:
            refuse(f"sentinel.observe needs exactly {sorted(lifecycle._OBSERVE_KEYS)}")
        if ref["db"] not in ("spanner-google", "spanner-postgres"):
            lifecycle._route(path, ref["db"], "sentinel.observe.db")
        if not isinstance(ref["table"], str) or "${TID}" not in ref["table"]:
            refuse("sentinel.observe.table must contain ${TID} (a run-owned table)")
    key = ref.get("key")
    if not isinstance(key, str) or not key or any(not lifecycle._IDENT.fullmatch(s) for s in key.split(".")):
        refuse(f"sentinel.observe.key must be a column name or field path, got {key!r}")
    if sink == "db" and not lifecycle._IDENT.fullmatch(key):
        refuse("sentinel.observe.key must be a plain column name for a db sink")
    if sink in ("file", "kafka"):
        deletion = ref.get("deletion")
        if not isinstance(deletion, dict) or not deletion or any(not isinstance(k, str) or not k for k in deletion):
            refuse("sentinel.observe.deletion needs a non-empty field selector")
    return dict(ref)


def rows(ref, admins, tokens, remaining, read_files=None):
    if "path" in ref:
        if read_files is None:
            raise ValueError("sentinel file reader is unavailable")
        return strictjson.events(read_files(render(ref["path"], tokens), run=lifecycle.timed_run(remaining)))
    admin = lifecycle._admin(admins, ref["db"])
    if ref["db"] == "kafka":
        return admin.select_records(render(ref["topic"], tokens), poll_budget=remaining,
            max_rows=canon.DEFAULT_MAX_ROWS, max_bytes=canon.DEFAULT_MAX_BYTES,
            strict_json=True, payload=ref.get("payload", "avro"), strict=True)
    if ref["db"].startswith("spanner-"):
        return exactdata.read_spanner(admin, render(ref["table"], tokens), None,
                                      canon.DEFAULT_MAX_ROWS, canon.DEFAULT_MAX_BYTES, remaining)
    return exactdata.read_pg(admin, lifecycle._table(render(ref["table"], tokens)), None,
                             canon.DEFAULT_MAX_ROWS, canon.DEFAULT_MAX_BYTES, remaining)


def run_sentinel(sentinel, watch, terminal):
    s, ref = sentinel.spec.sentinel, sentinel.spec.sentinel["observe"]
    writer = lifecycle.make_probe(lifecycle._admin(sentinel.admins, s["db"]))
    def count(remaining):
        if ref.get("db", "").startswith("spanner-"):
            admin = lifecycle._admin(sentinel.admins, ref["db"])
            return admin.count_key(render(ref["table"], sentinel.tokens), ref["key"], sentinel.id, remaining)
        found = [r for r in rows(ref, sentinel.admins, sentinel.tokens, remaining, sentinel.read_files)
                 if nothing.get_path(r, ref["key"])[0]
                 and str(nothing.get_path(r, ref["key"])[1]) == str(sentinel.id)]
        deletion = {k: render(v, sentinel.tokens) if isinstance(v, str) else v
                    for k, v in ref.get("deletion", {}).items()}
        if deletion and any(nothing.matches(r, deletion) for r in found):
            return 0
        return len(found)
    sentinel.count = count
    for step, key, want in (("present", "insert", lambda n: n > 0), ("absent", "delete", lambda n: n == 0)):
        try:
            template = sentinel.source_sql(s[key])
            sql = render(template, sentinel.tokens)
            writer.run(sql, watch.deadline.remaining())
            if sentinel.record:
                sentinel.record("sentinel", s[key], sql, template=template)
            watch.observe({"step": key, "id": sentinel.id})
            reason, _ = lifecycle._wait(watch, lambda left: {"step": step, "count": count(left)},
                                       lambda value: want(value["count"]), terminal)
        except Exception as e:
            watch.observe({"step": key, "error": str(e)})
            return getattr(e, "reason", f"error:sentinel {key} failed: {e}")
        if reason != "satisfied":
            return reason
    return "satisfied"
