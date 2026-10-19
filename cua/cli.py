"""Command line: discover | replay | stability | approve | catalog | invoke.

Secret inputs are never accepted on the command line (shell history): set
CUA_PARAM_<NAME> in the environment (or .env) instead.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from . import store
from .agent import DEFAULT_MODEL, Agent, TaskSpec
from .escalation import ControlState, Escalator, InterventionRequest, NoOperator, Operator, Resolution, \
    ScriptedOperator, WebOperator
from .evidence import RunLog, new_run_id
from .profile import load_profile
from .redact import Redactor
from .replay import Faults, Replayer
from .schema import Capability
from .surface import WebSurface

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "evidence"


def _collect_params(cap_inputs: list[Any], cli_params: list[str]) -> dict[str, str]:
    params: dict[str, str] = {}
    for kv in cli_params or []:
        k, _, v = kv.partition("=")
        params[k] = v
    for p in cap_inputs:
        if p.secret:
            if p.name in params:
                sys.exit(f"secret input '{p.name}' must come from env CUA_PARAM_{p.name.upper()}, not the CLI")
            v = os.environ.get(f"CUA_PARAM_{p.name.upper()}")
            if v:
                params[p.name] = v
    return params


def _human_demo_script(params: dict[str, str]) -> Any:
    """Mock human operator for headless demos: re-authenticates in the SAME live page."""
    def script(surface: WebSurface, req: InterventionRequest) -> Resolution:
        page = surface.page
        if page.locator("form[name=login]").count():
            page.fill("input[name=username]", params.get("username", ""))
            page.fill("input[name=password]", params.get("password", ""))
            page.click("input[value='Log In']")
            surface.settle()
            return Resolution("retry_step", "human re-authenticated the live session")
        return Resolution("abort", "human could not resolve")
    return script


def _operator(kind: str, params: dict[str, str]) -> Operator:
    if kind == "web":
        return WebOperator()
    if kind == "scripted":
        return ScriptedOperator(_human_demo_script(params))
    return NoOperator()


def _setup(profile_name: str, secrets: list[str], run_prefix: str, out: str | None, headed: bool,
           allow_irreversible: bool = False):
    profile = load_profile(profile_name, allow_irreversible=allow_irreversible)
    run_id = new_run_id(run_prefix)
    redactor = Redactor(secrets)
    log = RunLog(Path(out) if out else EVIDENCE / run_id, run_id, redactor)
    surface = WebSurface(profile.policy, headless=not headed)
    return profile, run_id, redactor, log, surface


def cmd_discover(a: argparse.Namespace) -> int:
    task = TaskSpec.load(a.task)
    params = _collect_params(task.inputs, a.param)
    secrets = [params[p.name] for p in task.inputs if p.secret and p.name in params]
    profile, run_id, redactor, log, surface = _setup(task.profile, secrets, "discovery", a.out, a.headed)
    control = ControlState()
    escalator = Escalator(_operator(a.operator, params), control, log) if a.operator != "none" else None
    try:
        agent = Agent(surface, profile, task, params, log, redactor, escalator, model=a.model,
                      max_steps=a.max_steps)
        res = agent.run()
        print(json.dumps({"success": res.success, "stop_reason": res.stop_reason, "summary": res.summary,
                          "steps": res.steps_taken, "usage": agent.usage, "run_dir": str(log.dir)}, indent=2))
        if res.capability:
            path = store.save(res.capability)
            log.write_json("artifact.json", json.loads(res.capability.model_dump_json()))
            print(f"artifact saved: {path}")
        return 0 if res.success else 1
    finally:
        log.close()
        surface.close()


def cmd_replay(a: argparse.Namespace, cap: Capability | None = None) -> int:
    cap = cap or (store.load(a.capability) if a.capability.endswith(".json") else store.find(a.capability))
    params = _collect_params(cap.inputs, a.param)
    secrets = [params[p.name] for p in cap.inputs if p.secret and p.name in params]
    profile, run_id, redactor, log, surface = _setup(cap.app.profile, secrets, "replay", a.out, a.headed,
                                                     allow_irreversible=a.allow_irreversible)
    control = ControlState()
    escalator = Escalator(_operator(a.operator, params), control, log) if a.operator != "none" else None
    try:
        rp = Replayer(surface, profile.policy, log, redactor, escalator, control, Faults.parse(a.inject or []))
        res = rp.run(cap, params, allow_draft=a.allow_draft)
        log.write_json("result.json", json.loads(res.model_dump_json()))
        print(res.model_dump_json(indent=2))
        return {"success": 0, "business_outcome": 2}.get(res.status, 1)
    finally:
        log.close()
        surface.close()


def cmd_stability(a: argparse.Namespace) -> int:
    cap = store.find(a.capability)
    results = []
    for i in range(a.runs):
        ns = argparse.Namespace(**{**vars(a), "allow_draft": True, "operator": "none", "inject": [],
                                   "allow_irreversible": False, "out": str(EVIDENCE / f"stability-{cap.name}-{i + 1}")})
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cmd_replay(ns, cap)
        results.append(code == 0)
        print(f"run {i + 1}/{a.runs}: {'ok' if code == 0 else 'FAIL'}")
    cap = store.record_stability(cap, results)
    store.save(cap)
    print(f"stability: {sum(results)}/{len(results)}  confidence score={cap.confidence.score}")
    return 0 if all(results) else 1


def cmd_approve(a: argparse.Namespace) -> int:
    cap = store.approve(store.find(a.capability), a.min_score, a.min_runs)
    store.save(cap)
    print(f"{cap.name} v{cap.version} -> {cap.status.value} (confidence {cap.confidence.score})")
    return 0


def cmd_catalog(a: argparse.Namespace) -> int:
    print(json.dumps(store.catalog(approved_only=not a.all), indent=2))
    return 0


def cmd_invoke(a: argparse.Namespace) -> int:
    """What a calling agent does: pick a tool by name, pass typed args (secrets come from the vault/env)."""
    spec_args = json.loads(a.args)
    cap = store.find(a.name)
    ns = argparse.Namespace(capability=a.name, param=[f"{k}={v}" for k, v in spec_args.items()],
                            inject=[], operator=a.operator, allow_draft=False, headed=False,
                            allow_irreversible=False, out=a.out)
    return cmd_replay(ns, cap)


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser(prog="cua", description="Computer-use automation: discover once, replay deterministically")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="LLM-driven run that records a capability artifact")
    d.add_argument("--task", required=True)
    d.add_argument("--param", action="append", default=[], help="name=value (non-secret inputs)")
    d.add_argument("--model", default=DEFAULT_MODEL)
    d.add_argument("--max-steps", type=int, default=25)
    d.add_argument("--operator", choices=["none", "web", "scripted"], default="none")
    d.add_argument("--headed", action="store_true")
    d.add_argument("--out")
    d.set_defaults(fn=cmd_discover)

    r = sub.add_parser("replay", help="deterministic replay of a saved artifact (no LLM)")
    r.add_argument("capability", help="capability name or artifact path")
    r.add_argument("--param", action="append", default=[])
    r.add_argument("--inject", action="append", default=[], help="dialog@<step> | session@<step>")
    r.add_argument("--operator", choices=["none", "web", "scripted"], default="none")
    r.add_argument("--allow-draft", action="store_true", help="run an unapproved (draft) artifact")
    r.add_argument("--allow-irreversible", action="store_true")
    r.add_argument("--headed", action="store_true")
    r.add_argument("--out")
    r.set_defaults(fn=cmd_replay)

    s = sub.add_parser("stability", help="replay N times, record confidence score")
    s.add_argument("capability")
    s.add_argument("--runs", type=int, default=5)
    s.add_argument("--param", action="append", default=[])
    s.add_argument("--headed", action="store_true")
    s.set_defaults(fn=cmd_stability)

    ap_ = sub.add_parser("approve", help="draft -> approved (requires stability evidence)")
    ap_.add_argument("capability")
    ap_.add_argument("--min-score", type=float, default=0.8)
    ap_.add_argument("--min-runs", type=int, default=3)
    ap_.set_defaults(fn=cmd_approve)

    c = sub.add_parser("catalog", help="print approved capabilities as function-calling tool specs")
    c.add_argument("--all", action="store_true", help="include drafts")
    c.set_defaults(fn=cmd_catalog)

    i = sub.add_parser("invoke", help="invoke an approved capability by name with typed JSON args")
    i.add_argument("name")
    i.add_argument("--args", default="{}")
    i.add_argument("--operator", choices=["none", "web", "scripted"], default="none")
    i.add_argument("--out")
    i.set_defaults(fn=cmd_invoke)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
