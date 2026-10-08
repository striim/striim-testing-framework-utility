from __future__ import annotations
import re

def schema_for(testid: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", testid.lower()).strip("_")
    return f"slt_{slug}"
