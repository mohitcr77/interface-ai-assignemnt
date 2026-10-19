"""Safety policy: allowlist enforcement and risk classification."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .schema import Risk

ALL_ACTIONS = {"navigate", "click", "type", "select", "press", "extract"}


@dataclass
class Decision:
    allow: bool
    risk: Risk
    reason: str = ""


@dataclass
class Policy:
    """Explicit, configurable guardrails (loaded from the app profile).

    * allowed_hosts: netloc allowlist (host[:port]). Anything else is blocked,
      both for agent-proposed navigation and for browser requests (network-level).
    * allowed_actions: action types the agent/replay may perform.
    * irreversible_patterns: control names that commit state. Only evaluated for
      submit-style controls (buttons), not plain navigation links.
    * allow_irreversible: off by default. Irreversible steps are BLOCKED unless an
      operator explicitly enables them for a run (`--allow-irreversible`).
    """
    allowed_hosts: list[str]
    allowed_actions: set[str] = field(default_factory=lambda: set(ALL_ACTIONS))
    irreversible_patterns: list[str] = field(default_factory=list)
    safe_submit_patterns: list[str] = field(default_factory=list)
    allow_irreversible: bool = False

    def url_allowed(self, url: str) -> bool:
        if url.startswith(("about:", "data:", "blob:")):
            return True
        p = urlparse(url)
        return p.netloc in self.allowed_hosts

    def classify(self, action: str, name: str = "", tag: str = "", input_type: str = "") -> Risk:
        if action == "extract":
            return Risk.READ
        if action == "click":
            is_submit = tag == "button" or input_type in ("submit", "button", "image")
            if is_submit:
                if any(re.search(p, name, re.I) for p in self.safe_submit_patterns):
                    return Risk.REVERSIBLE
                if any(re.search(p, name, re.I) for p in self.irreversible_patterns):
                    return Risk.IRREVERSIBLE
                # Unknown submit control: be conservative.
                return Risk.IRREVERSIBLE
        return Risk.REVERSIBLE

    def check(self, action: str, risk: Risk, url: str = "", target_url: str = "") -> Decision:
        if action not in self.allowed_actions:
            return Decision(False, risk, f"action '{action}' not in allowed_actions")
        if target_url and not self.url_allowed(target_url):
            return Decision(False, risk, f"host not allowlisted: {urlparse(target_url).netloc}")
        if url and not self.url_allowed(url):
            return Decision(False, risk, f"current page outside allowlist: {urlparse(url).netloc}")
        if risk == Risk.IRREVERSIBLE and not self.allow_irreversible:
            return Decision(False, risk, "irreversible action blocked (needs explicit operator approval)")
        return Decision(True, risk)
