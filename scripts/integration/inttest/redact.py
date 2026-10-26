"""Masks known secrets in failure text. Mirrors livetest.evidence's redaction: each engine imports alone."""
from __future__ import annotations

import os
import re

SECRET_MIN_LEN = 8
REDACTED = "[redacted]"
_TOKEN_MIN_LEN = 3
_TOKEN_CHARS = r"A-Za-z0-9_.\-/@"
_ASSIGNMENT = r"(?P<pre>(?i:[A-Za-z0-9_.\-]*(?:PASS|PWD|SECRET|TOKEN|KEY|CREDENTIAL|AUTH)[A-Za-z0-9_.\-]*)[\"']?\s*[=:]\s*[\"']?)"
_SECRET_NAME = re.compile(r"(?i)(PASS|PASSWORD|SECRET|TOKEN|KEY|CREDENTIAL|AUTH|COMPANY_NAME|CLUSTER_NAME)")
_CREDENTIAL_KEY = re.compile(r"(?i)(PASS|SECRET|KEY|TOKEN|COMPANY_NAME|CLUSTER_NAME)")
_USERINFO = re.compile(r"(?P<s>[A-Za-z][A-Za-z0-9+.-]*://)[^/@\s]+@")
PUBLIC_NAMES = frozenset({"striim"})
_LICENCE_NAME = re.compile(r"(?i)(?:[A-Za-z0-9_]*_)?(?:COMPANY|CLUSTER)_NAME")


def _public_licence_name(key: str, value) -> bool:
    return bool(_LICENCE_NAME.fullmatch(key)) and str(value).strip().lower() in PUBLIC_NAMES


def credentials_in(mapping) -> set[str]:
    """Non-empty string values under credential-like keys (a `*_port` is not one)."""
    return {value for key, value in (mapping or {}).items()
            if _CREDENTIAL_KEY.search(str(key)) and not str(key).lower().endswith("_port")
            and isinstance(value, str) and value and not _public_licence_name(str(key), value)}


def known_secrets(env=None, extra=()) -> list[str]:
    """Secret-named environment values plus `extra`, longest first."""
    env = os.environ if env is None else env
    values = {str(v) for k, v in env.items()
              if _SECRET_NAME.search(k) and v and not _public_licence_name(k, v)}
    values |= {str(v) for v in extra if v}
    return sorted(values, key=lambda v: (-len(v), v))


def _secret_pattern(secret: str):
    return re.compile(rf"(?<![{_TOKEN_CHARS}]){re.escape(secret)}(?![{_TOKEN_CHARS}])")


def _assignment_pattern(secret: str):
    return re.compile(_ASSIGNMENT + re.escape(secret) + rf"(?![{_TOKEN_CHARS}])")


def redact_text(text, secrets):
    """A long secret everywhere; a short one only as the whole text, an assigned credential
    (password=ab) or a whole token -- never inside an identifier. URL userinfo always."""
    if not isinstance(text, str):
        return text
    text = _USERINFO.sub(r"\g<s>", text)
    for secret in secrets:
        if not secret or secret not in text:
            continue
        if len(secret) >= SECRET_MIN_LEN:
            text = text.replace(secret, REDACTED)
        elif text == secret:
            text = REDACTED
        else:
            text = _assignment_pattern(secret).sub(lambda m: m.group("pre") + REDACTED, text)
            if len(secret) >= _TOKEN_MIN_LEN:
                text = _secret_pattern(secret).sub(REDACTED, text)
    return text
