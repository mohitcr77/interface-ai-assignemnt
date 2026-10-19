"""Redaction of secrets and regulated data before anything is logged or persisted."""
from __future__ import annotations

import re
from typing import Any

# Order matters: most specific first.
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE = re.compile(r"(?<!\d)(?:\+?1[ -.]?)?\(?\d{3}\)?[ -.]\d{3}[ -.]\d{4}(?!\d)")
_CARD = re.compile(r"(?<![\d.])(?:\d[ -]?){13,19}(?![\d.])")
# Account-number-like digit runs; skip money amounts such as $100000.00 / 1,000,000
_ACCT = re.compile(r"(?<![\d.,$])\d{5,}(?![\d]|\.\d|,\d)")


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


class Redactor:
    """Scrubs known secret values plus PII-shaped patterns.

    Known secret values (e.g. the password input) are replaced exactly. Pattern
    based masking is a best-effort backstop, not a guarantee (see REPORT.md).
    """

    def __init__(self, secrets: list[str] | None = None) -> None:
        self._secrets = sorted({s for s in (secrets or []) if s}, key=len, reverse=True)

    def add_secret(self, value: str) -> None:
        if value and value not in self._secrets:
            self._secrets.append(value)
            self._secrets.sort(key=len, reverse=True)

    def scrub(self, text: str, accounts: bool = True) -> str:
        """`accounts=False` keeps account-number-like digits (needed in the live LLM context,
        where the model must see which row is account 12345). Logs/artifacts always mask them."""
        if not text:
            return text
        for s in self._secrets:
            text = text.replace(s, "[REDACTED]")
        text = _SSN.sub("[SSN]", text)
        text = _EMAIL.sub("[EMAIL]", text)
        text = _PHONE.sub("[PHONE]", text)

        def card(m: re.Match[str]) -> str:
            digits = re.sub(r"\D", "", m.group(0))
            return "[CARD]" if 13 <= len(digits) <= 19 and _luhn(digits) else m.group(0)

        text = _CARD.sub(card, text)
        if accounts:
            text = _ACCT.sub(lambda m: "****" + m.group(0)[-4:], text)
        return text

    def scrub_obj(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.scrub(obj)
        if isinstance(obj, dict):
            return {k: self.scrub_obj(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.scrub_obj(v) for v in obj]
        return obj
