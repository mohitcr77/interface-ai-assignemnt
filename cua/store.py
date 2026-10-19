"""Store, catalog (agent-facing tool surface), approval gate and stability scoring."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from .schema import Approval, Capability, Confidence, ParamType

ARTIFACT_DIR = Path(__file__).resolve().parent.parent / "artifacts"

_JSON_TYPES = {ParamType.STRING: "string", ParamType.INTEGER: "integer", ParamType.NUMBER: "number"}


def save(cap: Capability, directory: Path = ARTIFACT_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / f"{cap.name}.v{cap.version}.json"
    p.write_text(cap.model_dump_json(indent=2))
    return p


def load(path: str | Path) -> Capability:
    return Capability.model_validate_json(Path(path).read_text())


def list_artifacts(directory: Path = ARTIFACT_DIR) -> list[Capability]:
    return [load(p) for p in sorted(directory.glob("*.json"))]


def find(name: str, directory: Path = ARTIFACT_DIR) -> Capability:
    cands = [c for c in list_artifacts(directory) if c.name == name]
    if not cands:
        raise KeyError(f"no capability named '{name}'")
    return max(cands, key=lambda c: c.version)


def tool_spec(cap: Capability) -> dict[str, Any]:
    """Anthropic/OpenAI-style function-calling spec derived from the artifact contract.

    The calling agent discovers what the capability needs (typed inputs, secrets excluded
    from args and sourced from the vault/env), what it returns, and which business outcomes
    it can answer with.
    """
    props = {
        p.name: {"type": _JSON_TYPES[p.type], "description": p.description,
                 **({"pattern": p.pattern} if p.pattern else {})}
        for p in cap.inputs if not p.secret
    }
    outcomes = [c.id for c in cap.conditions if c.kind.value == "business_outcome"]
    desc = (f"{cap.description} Returns: " + ", ".join(f"{o.name} ({o.type.value})" for o in cap.outputs) +
            (f". Possible business outcomes: {', '.join(outcomes)}." if outcomes else "."))
    return {
        "name": cap.name,
        "description": desc,
        "input_schema": {"type": "object", "properties": props,
                         "required": [p.name for p in cap.inputs if p.required and not p.secret]},
        "x-capability": {"version": cap.version, "status": cap.status.value,
                         "confidence": cap.confidence.score,
                         "secret_inputs": [p.name for p in cap.inputs if p.secret]},
    }


def catalog(directory: Path = ARTIFACT_DIR, approved_only: bool = True) -> list[dict[str, Any]]:
    return [tool_spec(c) for c in list_artifacts(directory)
            if c.status == Approval.APPROVED or not approved_only]


def approve(cap: Capability, min_score: float = 0.8, min_runs: int = 3) -> Capability:
    """Draft -> approved gate: only artifacts that replay reliably may run unattended."""
    c = cap.confidence
    if c.runs < min_runs or c.score < min_score:
        raise ValueError(f"cannot approve: need >= {min_runs} stability runs at >= {min_score:.0%} "
                         f"(have {c.successes}/{c.runs} = {c.score:.0%}). Run `stability` first.")
    cap.status = Approval.APPROVED
    return cap


def record_stability(cap: Capability, outcomes: list[bool]) -> Capability:
    runs = cap.confidence.runs + len(outcomes)
    succ = cap.confidence.successes + sum(outcomes)
    cap.confidence = Confidence(runs=runs, successes=succ, score=round(succ / runs, 3) if runs else 0.0,
                                last_evaluated=time.strftime("%Y-%m-%dT%H:%M:%S"))
    return cap
