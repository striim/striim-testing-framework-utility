"""Fail before provisioning when a writer leaves the qualified emulator paths."""
from __future__ import annotations

import re

# Only literal properties from the documented writer contract are understood here.
# In particular, connection profiles and cloud-storage staging are not qualified.
_PROPERTIES = set("""allowquotednewlines batchpolicy billingprojectid cddlaction columndelimiter
connectionretrypolicy datalocation encoding excludedtables ignorableexceptioncode includeinsertid
mode nullmarker optimizedmerge parallelthreads privateserviceconnectendpoint projectid
quotecharacter serviceaccountkey standardsql streamingconfiguration streamingupload tables
uploadpolicy maxuploadingthreads queuesize _h_maxtaskqueuesize""".split())
_TOKEN = re.compile(r"\s+|--[^\n]*|//[^\n]*|/\*.*?\*/|'(?:\\.|''|[^'\\])*'|\"(?:\\.|\"\"|[^\"\\])*\"|[A-Za-z_][A-Za-z_0-9]*|[^\s]", re.S)


def validate_writer(manifest, text, bindings=None):
    if "bigquery" not in getattr(manifest, "requires", ()):
        return
    tokens = [m.group() for m in _TOKEN.finditer(text)
              if not m.group().isspace() and not m.group().startswith(("--", "//", "/*"))]

    def refuse(reason):
        raise ValueError(f"BigQuery emulator lane: {reason}")

    if (any(a.get("type") == "alter_recompile" for a in getattr(manifest, "action_specs", ()))
            or any(a.lower() == "alter" and b.lower() == "target"
                   for a, b in zip(tokens, tokens[1:]))):
        refuse("writer property changes are unqualified (ALTER TARGET / alter_recompile)")

    for index, token in enumerate(tokens):
        if token.lower() != "bigquerywriter":
            continue
        prefix = tokens[max(0, index-3):index]
        if not ([t.lower() for t in prefix[-1:]] == ["using"]
                or [t.lower() for t in prefix] == ["using", "global", "."]):
            refuse("BigQueryWriter must use a literal USING property block")
        i, props = index + 1, {}
        if tokens[i:i+1] != ["("]:
            refuse("BigQueryWriter requires literal properties")
        i += 1
        while tokens[i:i+1] != [")"]:
            if i + 2 >= len(tokens) or tokens[i+1] != ":":
                refuse("cannot validate BigQueryWriter properties")
            key, value = tokens[i].lower(), tokens[i+2]
            if key not in _PROPERTIES or key in props:
                refuse(f"unsupported or duplicate BigQueryWriter property: {key}")
            if not (value[:1] in ("'", '"') or re.fullmatch(r"[A-Za-z_0-9]+", value)):
                refuse(f"property {key} must be a literal")
            props[key] = value[1:-1] if value[:1] in ("'", '"') else value
            i += 3
            if tokens[i:i+1] == [","]:
                i += 1
            elif tokens[i:i+1] != [")"]:
                refuse("cannot validate BigQueryWriter property expression")
        streaming = props.get("streamingupload", "false").lower()
        mode = props.get("mode", "APPENDONLY").upper()
        if streaming not in ("true", "false") or mode not in ("APPENDONLY", "MERGE"):
            refuse("StreamingUpload and Mode must be explicit qualified literal values")
        if streaming == "true":
            config = {}
            for pair in props.get("streamingconfiguration", "").split(","):
                parts = pair.split("=")
                if len(parts) != 2 or parts[0].strip().lower() in config:
                    refuse("streaming requires explicit UseLegacyStreamingAPI=true")
                config[parts[0].strip().lower()] = parts[1].strip().lower()
            if config.get("uselegacystreamingapi") != "true":
                refuse("Storage Write is unqualified; set UseLegacyStreamingAPI=true")
            if mode == "MERGE":
                refuse("streaming MERGE is unqualified; use batch MERGE")
        if mode == "MERGE" and not (manifest.lifecycle and manifest.lifecycle.uses_bigquery_cdc()):
            refuse("MERGE requires an owned CDC dataset (sentinel observes bigquery-target)")

        if bindings is not None:
            from livetest.bigqueryadmin import local_http
            endpoint = props.get("privateserviceconnectendpoint")
            if not endpoint or endpoint != bindings.get("BIGQUERY_ENDPOINT"):
                refuse("privateserviceconnectendpoint must equal BIGQUERY_ENDPOINT")
            local_http(endpoint)
            if props.get("serviceaccountkey") != "UploadedFiles/fake-bigquery-key.json":
                refuse("serviceaccountkey must use UploadedFiles/fake-bigquery-key.json")
            project = bindings.get("BIGQUERY_PROJECT")
            if not project or props.get("projectid") != project:
                refuse("projectid must equal BIGQUERY_PROJECT")
            if "billingprojectid" in props and props["billingprojectid"] != project:
                refuse("billingprojectid must equal BIGQUERY_PROJECT")
