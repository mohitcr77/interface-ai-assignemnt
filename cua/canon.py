"""URL canonicalization and template helpers."""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlparse

_NUMERIC = re.compile(r"^\d+$")
_HEXID = re.compile(r"^[0-9a-f]{8,}(-[0-9a-f]{4,})*$", re.I)


def _is_id(v: str) -> bool:
    return bool(_NUMERIC.match(v) or _HEXID.match(v))


def canonicalize_url(url: str) -> str:
    """`http://h/parabank/activity.htm?id=12345` -> `/parabank/activity.htm?id=:id`.

    Concrete ids become `:id` so one artifact generalizes over records (and, with the
    host dropped, over tenants hosting the same app under different domains).
    """
    p = urlparse(url)
    path = "/".join(":id" if _is_id(seg) else seg for seg in p.path.split("/"))
    if p.query:
        q = "&".join(f"{k}={':id' if _is_id(v) else v}" for k, v in parse_qsl(p.query, keep_blank_values=True))
        return f"{path}?{q}"
    return path


def url_matches(pattern: str, url: str) -> bool:
    return canonicalize_url(url) == pattern


def substitute(template: str, params: dict[str, str]) -> str:
    return re.sub(r"\{\{(\w+)\}\}", lambda m: str(params.get(m.group(1), m.group(0))), template)


def templates_in(template: str) -> set[str]:
    return set(re.findall(r"\{\{(\w+)\}\}", template))
