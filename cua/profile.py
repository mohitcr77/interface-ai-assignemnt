"""App profiles: per-app policy + known runtime conditions.

A profile is the unit of reuse across tenants running the same vendor product:
a tenant variant is `base profile + overrides` (see REPORT.md, section 4).
`load_profile(name, overrides=...)` shallow-merges override keys onto the base.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .policy import ALL_ACTIONS, Policy
from .schema import Condition

PROFILE_DIR = Path(__file__).resolve().parent.parent / "profiles"


@dataclass
class Profile:
    name: str
    description: str
    entry_url: str
    policy: Policy
    conditions: list[Condition]


def load_profile(name: str, overrides: Optional[dict[str, Any]] = None,
                 allow_irreversible: bool = False) -> Profile:
    raw = json.loads((PROFILE_DIR / f"{name}.json").read_text())
    if overrides:
        raw.update(overrides)
    policy = Policy(
        allowed_hosts=raw["allowed_hosts"],
        allowed_actions=set(raw.get("allowed_actions") or ALL_ACTIONS),
        irreversible_patterns=raw.get("irreversible_patterns", []),
        safe_submit_patterns=raw.get("safe_submit_patterns", []),
        allow_irreversible=allow_irreversible,
    )
    return Profile(
        name=raw["name"],
        description=raw.get("description", ""),
        entry_url=raw["entry_url"],
        policy=policy,
        conditions=[Condition(**c) for c in raw.get("conditions", [])],
    )
