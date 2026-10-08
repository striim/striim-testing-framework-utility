# `slt-canon/1` pinned fixtures

- `orders.csv` (CRLF) and `orders_dup.csv` (LF, row 1 repeated in another spelling): db-route goldens
  with typed columns, `<null>` and an empty undeclared cell.
- `events.csv` (file-route golden with `<null>`, `<absent>` and an embedded newline) and `events.json`
  (the matching actual events, read with `parse_float=Decimal`), compared under `order: sequence`.
- `reference-cells.json`: the canonical cells of each fixture, **written by hand** from the C8.2 text
  (`docs/internals/design/c8-exact-data.md`), not produced by `livetest.canon`.
- `digests.json`: the output of the reference script below over `reference-cells.json`. It implements
  only the C8.2 byte framing, independent of the code under test; `test_pinned_fixture_digests` checks
  that `livetest.canon` canonicalizes the fixture files to the same digests.

Regenerate (only when C8.2 itself changes) from this directory:

```python
import hashlib, json

def cj(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

def sha(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()

def digest(rows, declaration):
    order = declaration["order"]
    lines = [json.dumps(r, ensure_ascii=False, separators=(",", ":")).encode("utf-8") for r in rows]
    if order == "any":
        lines.sort()
    head = b"slt-canon/1\n" + b"order=" + order.encode() + b"\n" + b"decl=" + sha(cj(declaration)).encode() + b"\n"
    return sha(head + b"".join(line + b"\n" for line in lines))

cases = json.load(open("reference-cells.json", encoding="utf-8"))
out = {name: {"declarationSha256": sha(cj(c["declaration"])), "sha256": digest(c["rows"], c["declaration"])}
       for name, c in cases.items()}
open("digests.json", "w").write(json.dumps(out, indent=2, sort_keys=True) + "\n")
```
