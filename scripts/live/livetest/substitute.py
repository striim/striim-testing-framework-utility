from __future__ import annotations
import re

_TOKEN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

class SubstitutionError(Exception):
    pass

def missing_tokens(text: str, tokens: dict[str, str]) -> list[str]:
    names = {m.group(1) for m in _TOKEN.finditer(text)}
    return sorted(n for n in names if n not in tokens)

def render(text: str, tokens: dict[str, str]) -> str:
    missing = missing_tokens(text, tokens)
    if missing:
        raise SubstitutionError(f"missing token values: {', '.join(missing)}")
    return _TOKEN.sub(lambda m: str(tokens[m.group(1)]), text)
