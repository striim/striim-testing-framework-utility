"""Append-only audit contracts over the exact row multiset readers."""
import re

from livetest import lifecycle, canon
from livetest.sqlutil import split_sql_statements
from livetest.substitute import render


def _targets(tql):
    targets = {}
    for statement in split_sql_statements(tql):
        match = re.match(r"CREATE\s+(?:OR\s+REPLACE\s+)?TARGET\s+(\S+)\s+USING\s+(?:Global\.)?(\w+)\s*(?:VERSION\s+'[^']+'\s*)?\(", statement, re.I)
        if not match:
            continue
        # Writer properties can contain nested ColumnMap parentheses inside strings.
        start, pos, depth, quote = match.end(), match.end(), 1, False
        while pos < len(statement) and depth:
            char = statement[pos]
            if char == "'":
                if quote and pos + 1 < len(statement) and statement[pos + 1] == "'":
                    pos += 2
                    continue
                quote = not quote
            elif not quote:
                depth += (char == "(") - (char == ")")
            if char == "\\" and quote:
                pos += 1
            pos += 1
        props = {}
        for key, value in re.findall(r"(\w+)\s*:\s*('(?:''|\\.|[^'])*'|true|false|-?[0-9]+)", statement[start:pos-1], re.I):
            key = key.lower()
            if key in props:
                raise ValueError("audit-writer-not-append-only: duplicate writer property")
            props[key] = value.strip("'").lower() if value.lower() in ("true", "false", "'true'", "'false'") else value.strip("'")
        source = re.search(r"\bINPUT\s+FROM\s+(\S+)\s*$", statement[pos:], re.I)
        if depth or source is None or match.group(1) in targets:
            raise ValueError("audit-writer-not-append-only: ambiguous writer declaration")
        targets[match.group(1)] = (match.group(2), props, source.group(1))
    return targets


def normalize(raw, path, *, tql):
    specs = [(kind, index, spec) for kind, values in (raw.get("assert") or {}).items()
             if isinstance(values, list) for index, spec in enumerate(values)
             if isinstance(spec, dict) and "audit" in spec]
    block = raw.get("audit")
    if block is None and not specs:
        return []
    def refuse(message):
        raise ValueError(f"invalid-audit-spec: {path}: {message}")
    if not isinstance(block, dict) or set(block) != {"version"} or type(block["version"]) is not int or block["version"] != 1:
        refuse("audit needs exactly version: 1")
    if not specs or not raw.get("lifecycle") or raw.get("expect_halt"):
        refuse("audit requires an audited exact spec, lifecycle and successful application execution")
    tql = ";\n".join(split_sql_statements(tql))
    if re.search(r"\b(?:EXCEPTIONHANDLER|AUTORESUME)\b", tql, re.I):
        refuse("audit cases cannot swallow or automatically resume writer failures")
    targets, out = _targets(tql), []
    for kind, index, spec in specs:
        if kind not in ("data", "file") or "exact" not in spec or not isinstance(raw.get("exact"), dict):
            refuse("audit is supported on canonical exact data/file specs")
        exact = {} if spec["exact"] is True else spec["exact"]
        if not isinstance(exact, dict) or exact.get("order", "any") != "any":
            refuse("audit requires order: any (counted multiset)")
        audit = spec["audit"]
        if not isinstance(audit, dict) or set(audit) - {"writer", "delivery_key"} or not isinstance(audit.get("writer"), str):
            refuse("spec audit needs exactly writer")
        if audit["writer"] not in targets:
            refuse("audit writer is not declared in this app")
        adapter, props, source = targets[audit["writer"]]
        route = spec.get("target_db", spec.get("db", "kafka" if spec.get("project") == "kafka_record" else "postgres-source"))
        completion = raw["lifecycle"].get("completion", {})
        observe = raw["lifecycle"].get("sentinel", {}).get("observe", {}) if completion.get("kind") == "sentinel" else completion.get("target", completion)
        same = (observe.get("path") == spec.get("path")) if kind == "file" else (
            observe.get("db") == route and observe.get("table", observe.get("topic")) == spec.get("target"))
        if not same:
            refuse("lifecycle completion must observe the audited output")
        if "delivery_key" in audit and route not in ("spanner-google", "spanner-postgres"):
            refuse("delivery_key is only for Spanner audit tables")
        if kind == "data" and route not in (*canon.DB_ROUTES, "kafka"):
            refuse("unsupported audit route")
        if kind == "file":
            supported = adapter == "FileWriter" and props.get("filename") == spec.get("path")
        elif route == "kafka":
            supported = adapter.startswith("KafkaWriter") and (props.get("topic", props.get("topics")) == spec.get("target") and not ({"topic", "topics"} <= props.keys())) and spec.get("project") == "kafka_record"
        else:
            expected = "DatabaseWriter" if route.startswith("postgres-") else "SpannerWriter" if route == "spanner-google" else "SpannerPGDialectWriter"
            supported = adapter == expected and props.get("tables") == spec.get("target")
            stream = re.search(r"\bCREATE\s+(?:OR\s+REPLACE\s+)?STREAM\s+" + re.escape(source) + r"\s+OF\s+([^\s;]+)", tql, re.I)
            local_type = stream and re.search(r"\bCREATE\s+(?:OR\s+REPLACE\s+)?TYPE\s+" + re.escape(stream.group(1)) + r"\s*\(", tql, re.I)
            supported = supported and bool(local_type) and all(props.get(k) == "false" for k in ("executeinsertasupdate", "executeupdateasinsert"))
            supported = supported and not props.get("ignorableexceptioncode")
            if route.startswith("spanner-"):
                key = audit.get("delivery_key")
                if not isinstance(key, str) or not lifecycle._IDENT.fullmatch(key) or key not in exact.get("ignore", []):
                    refuse("Spanner audit requires delivery_key excluded explicitly with exact.ignore")
                fields = re.search(r"\bCREATE\s+(?:OR\s+REPLACE\s+)?TYPE\s+" + re.escape(stream.group(1)) + r"\s*\(([^)]*)\)", tql, re.I) if stream else None
                if fields and any(f.strip().split()[0] == key for f in fields.group(1).split(",") if f.strip()):
                    refuse("Spanner delivery_key must be generated by the sink, not supplied in the input stream")
                supported = supported and not props.get("keycolumns")
        if not supported:
            raise ValueError("audit-writer-not-append-only: use the matching append writer, a local typed database input, explicit false upsert flags and no ignored database errors")
        out.append({"kind": kind, "index": index, "writer": audit["writer"], "route": route, "spec": spec})
    return out


def guard_pg(admin, table_sql, *, query=None, run=None, ledger=None, tokens=None, route=None):
    probe = lifecycle.PgProbe(admin) if query is None else None
    query = query or probe.query
    run = run or probe.run
    match = lifecycle._TABLE.fullmatch(table_sql.replace('"', ''))
    function = f"{match.group(1)}.{tokens['TID']}audit_guard" if tokens and match else None
    trigger = f"{tokens['TID']}audit_guard" if tokens else ""
    signature = f"{function}()" if function else ""
    count = query("SELECT (SELECT count(*) FROM pg_index WHERE indrelid = %s::regclass AND (indisunique OR indisexclusion)) "
                  "+ (SELECT count(*) FROM pg_trigger WHERE tgrelid = %s::regclass AND NOT tgisinternal "
                  "AND NOT (tgname = %s AND tgfoid = to_regprocedure(%s) AND tgenabled = 'A' AND tgtype = 58)) "
                  "+ (SELECT count(*) FROM pg_rewrite WHERE ev_class = %s::regclass AND ev_type <> '1')",
                  (table_sql, table_sql, trigger, signature or None, table_sql), 10)[0][0]
    if count:
        raise ValueError("audit-target-not-append-only: Postgres audit tables must have no unique/exclusion indexes, foreign user triggers or rewrite rules")
    if ledger is None or not function or not lifecycle._TABLE.fullmatch(function):
        raise ValueError("audit-target-not-owned: audit guard needs the current ledger")
    found = ledger.find("pg-function", function, route, attempt=ledger.ident.attempt, states=("confirmed",))
    definition = query("SELECT pg_get_functiondef(to_regprocedure(%s))", (signature,), 10)[0][0]
    if found:
        entry = found[0]
        if definition is None or found[0].get("definitionSha256") != canon._sha_text(definition.encode()):
            raise ValueError("audit-target-not-append-only: owned audit function changed")
    else:
        if definition is not None:
            raise ValueError("audit-target-not-owned: audit guard function already exists")
        entry = ledger.add("pg-function", function, route)
        schema, name = function.split(".")
        try:
            run(f'CREATE FUNCTION "{schema}"."{name}"() RETURNS trigger LANGUAGE plpgsql AS '
                "$$ BEGIN RAISE EXCEPTION 'audit-target-not-append-only'; END; $$", 10)
        finally:
            definition = query("SELECT pg_get_functiondef(to_regprocedure(%s))", (signature,), 10)[0][0]
            if definition is not None:
                entry["definitionSha256"] = canon._sha_text(definition.encode())
                ledger._set(entry, "confirmed")
            else:
                ledger._drop_entry(entry)
    exists = query("SELECT count(*) FROM pg_trigger WHERE tgrelid=%s::regclass AND tgname=%s "
                   "AND tgfoid=to_regprocedure(%s) AND tgenabled = 'A' AND tgtype=58",
                   (table_sql, trigger, signature), 10)[0][0]
    if not exists:
        if table_sql in entry.get("guardTables", []):
            raise ValueError("audit-target-not-append-only: owned audit trigger was removed or changed")
        run(f'CREATE TRIGGER "{trigger}" BEFORE UPDATE OR DELETE OR TRUNCATE ON {table_sql} '
            f'FOR EACH STATEMENT EXECUTE FUNCTION "{match.group(1)}"."{trigger}"()', 10)
        run(f'ALTER TABLE {table_sql} ENABLE ALWAYS TRIGGER "{trigger}"', 10)
        exists = query("SELECT count(*) FROM pg_trigger WHERE tgrelid=%s::regclass AND tgname=%s "
                       "AND tgfoid=to_regprocedure(%s) AND tgenabled = 'A' AND tgtype=58",
                       (table_sql, trigger, signature), 10)[0][0]
    if exists != 1:
        raise ValueError("audit-target-not-append-only: append guard was not confirmed")
    if table_sql not in entry.get("guardTables", []):
        entry.setdefault("guardTables", []).append(table_sql)
        ledger.persist()


def prepare(specs, admins, tokens, owned, *, timeout=120):
    for entry in specs:
        spec, route = entry["spec"], entry["route"]
        if entry["kind"] == "file":
            path = render(spec["path"], tokens)
            if not owned("owned-file", None, path):
                raise ValueError("audit-target-not-owned: file is outside this attempt's directory")
            continue
        admin = lifecycle._admin(admins, route)
        target = render(spec["target"], tokens)
        if route == "kafka":
            if not owned("topic", route, target):
                raise ValueError("audit-target-not-owned: Kafka topic was not acquired by this attempt")
            admin.check_audit_topic(target)
        else:
            kind = "pg-table" if route.startswith("postgres-") else "spanner-table"
            name = target.lower() if kind == "pg-table" else target
            if not owned(kind, route, name):
                raise ValueError("audit-target-not-owned: table was not acquired by this attempt")
            if kind == "pg-table":
                guard_pg(admin, lifecycle._table(target), ledger=getattr(owned, "__self__", None),
                         tokens=tokens, route=route)
            else:
                admin.check_audit_table(target, spec["audit"]["delivery_key"])
