"""Discovery: an LLM-driven observe -> decide -> act loop, then compilation to a Capability.

The model is only used here. It sees a compact element list (refs) of the live page,
chooses ONE tool call per turn, and we execute it on the real surface. Every action is
captured together with *verified* locator candidates (built before the click, while the
element is still on the page). `compile_capability` then turns the recording into the
typed artifact: concrete values equal to an input become `{{param}}` templates, and
instance-specific locators are dropped for parameterized targets.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .canon import canonicalize_url, substitute
from .escalation import Escalator, InterventionRequest
from .evidence import RunLog
from .policy import Policy
from .profile import Profile
from .redact import Redactor
from .schema import (AppRef, Capability, Checkpoint, Condition, ConditionKind, Detector, InputParam, Locator,
                     OutputSpec, ParamType, Provenance, Risk, Step, Target, ValueSpec)
from .surface import Element, LocatorNotFound, Observation, WebSurface

DEFAULT_MODEL = os.environ.get("CUA_MODEL", "claude-haiku-4-5")

SYSTEM = """You operate a legacy bank back-office web application through a text view of the page.
Each turn you get the current page as a list of elements with refs like [e12], then you must call EXACTLY ONE tool.

Rules:
- Achieve the goal in as FEW steps as possible; do not wander or explore unrelated pages.
- Inputs are referenced by name. When you need to type an input value, pass the placeholder, e.g. text="{{username}}".
  Never type a literal value for an input, and never invent values. Secret inputs are only available as placeholders.
- Use `extract` on the element that displays a requested output (the value cell, not its label). Declare each output once.
- This is a READ-ONLY task unless the goal says otherwise: never press buttons that submit or change data
  (transfers, new accounts, payments, loans, profile updates).
- If the application shows an error the goal cannot get past (e.g. credentials rejected), call `finish` with success=false
  and describe it. If you are genuinely stuck, call `ask_human`.
- Call `finish(success=true)` only after every requested output has been extracted."""

TOOLS = [
    {"name": "navigate", "description": "Open a URL (must be on the allowlisted host).",
     "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
    {"name": "click", "description": "Click an element by ref.",
     "input_schema": {"type": "object", "properties": {"ref": {"type": "string"}}, "required": ["ref"]}},
    {"name": "type", "description": "Type text into an input by ref. Use {{input_name}} placeholders for inputs. "
                                     "Set submit=true to press Enter afterwards.",
     "input_schema": {"type": "object", "properties": {"ref": {"type": "string"}, "text": {"type": "string"},
                                                       "submit": {"type": "boolean"}}, "required": ["ref", "text"]}},
    {"name": "select", "description": "Choose an option (by visible label) in a dropdown by ref.",
     "input_schema": {"type": "object", "properties": {"ref": {"type": "string"}, "value": {"type": "string"}},
                      "required": ["ref", "value"]}},
    {"name": "press", "description": "Press a keyboard key (e.g. Enter).",
     "input_schema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}},
    {"name": "extract", "description": "Read the text of an element into a declared output.",
     "input_schema": {"type": "object", "properties": {"output": {"type": "string"}, "ref": {"type": "string"}},
                      "required": ["output", "ref"]}},
    {"name": "finish", "description": "End the run.",
     "input_schema": {"type": "object", "properties": {"success": {"type": "boolean"}, "summary": {"type": "string"}},
                      "required": ["success", "summary"]}},
    {"name": "ask_human", "description": "Request human intervention when stuck or blocked.",
     "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
]


@dataclass
class TaskSpec:
    name: str
    goal: str
    description: str
    profile: str
    inputs: list[InputParam]
    outputs: list[OutputSpec]

    @classmethod
    def load(cls, path: str) -> "TaskSpec":
        raw = json.loads(open(path).read())
        return cls(raw["name"], raw["goal"], raw.get("description", raw["goal"]), raw["profile"],
                   [InputParam(**i) for i in raw["inputs"]], [OutputSpec(**o) for o in raw["outputs"]])


@dataclass
class Recorded:
    step: Step
    pre_url: str


@dataclass
class DiscoveryResult:
    success: bool
    summary: str
    capability: Optional[Capability]
    steps_taken: int
    stop_reason: str


class Agent:
    def __init__(self, surface: WebSurface, profile: Profile, task: TaskSpec, params: dict[str, str],
                 log: RunLog, redactor: Redactor, escalator: Optional[Escalator] = None,
                 model: str = DEFAULT_MODEL, max_steps: int = 25, timeout_s: int = 240,
                 client: Any = None) -> None:
        self.s, self.profile, self.task, self.params = surface, profile, task, params
        self.policy: Policy = profile.policy
        self.log, self.redactor, self.escalator = log, redactor, escalator
        self.model, self.max_steps, self.timeout_s = model, max_steps, timeout_s
        if client is None:
            import anthropic
            client = anthropic.Anthropic()
        self.client = client
        self.recorded: list[Recorded] = []
        self.history: list[str] = []
        self.extracted: dict[str, str] = {}
        self.usage = {"input_tokens": 0, "output_tokens": 0}

    # ------------------------------------------------------------------ prompt
    def _task_block(self) -> str:
        ins = []
        for p in self.task.inputs:
            val = "<secret - use the placeholder only>" if p.secret else f'value "{self.params.get(p.name, "")}"'
            ins.append(f'- {{{{{p.name}}}}} ({p.type.value}) {p.description}: {val}')
        outs = [f"- {o.name} ({o.type.value}): {o.description}" for o in self.task.outputs]
        done = ", ".join(self.extracted) or "none"
        todo = ", ".join(o.name for o in self.task.outputs if o.name not in self.extracted) or "none (call finish)"
        return (f"GOAL: {self.task.goal}\nINPUTS:\n" + "\n".join(ins) + "\nOUTPUTS TO EXTRACT:\n" + "\n".join(outs)
                + f"\nALREADY EXTRACTED: {done}\nSTILL TO EXTRACT: {todo}")

    def _llm_view(self, obs: Observation) -> str:
        return self.redactor.scrub(obs.render(), accounts=False)

    def _decide(self, obs: Observation) -> tuple[str, dict[str, Any], str]:
        hist = "\n".join(self.history[-12:]) or "(none yet)"
        user = f"{self._task_block()}\n\nSTEPS SO FAR:\n{hist}\n\nCURRENT PAGE:\n{self._llm_view(obs)}"
        resp = self.client.messages.create(
            model=self.model, max_tokens=700, system=SYSTEM, tools=TOOLS,
            tool_choice={"type": "any"}, messages=[{"role": "user", "content": user}])
        self.usage["input_tokens"] += resp.usage.input_tokens
        self.usage["output_tokens"] += resp.usage.output_tokens
        thought = " ".join(b.text for b in resp.content if b.type == "text").strip()
        for b in resp.content:
            if b.type == "tool_use":
                return b.name, dict(b.input), thought
        return "ask_human", {"reason": "model returned no tool call"}, thought

    # ------------------------------------------------------------------ loop
    def run(self) -> DiscoveryResult:
        t0 = time.time()
        entry = self.profile.entry_url
        self.log.event("discovery_start", goal=self.task.goal, model=self.model, entry=entry,
                       max_steps=self.max_steps)
        self.s.goto(entry)
        self.recorded.append(Recorded(Step(id="s1", intent="Open the application entry page", action="navigate",
                                           risk=Risk.REVERSIBLE, value=ValueSpec(template=entry)), ""))
        last_sig, repeats, errors = "", 0, 0
        steps = 0
        pending: Optional[Recorded] = self.recorded[0]
        result: Optional[DiscoveryResult] = None
        while result is None:
            obs = self.s.observe()
            self._finalize_expect(pending, obs)
            pending = None
            if steps >= self.max_steps:
                result = DiscoveryResult(False, "step budget exhausted", None, steps, "max_steps")
                break
            if time.time() - t0 > self.timeout_s:
                result = DiscoveryResult(False, "timeout", None, steps, "timeout")
                break
            shot = self.log.screenshot_path(f"turn{steps + 1}")
            self.s.screenshot(str(shot))
            name, args, thought = self._decide(obs)
            steps += 1
            self.log.event("agent_decision", turn=steps, tool=name, args=args, reasoning=thought, url=obs.url,
                           usage=dict(self.usage))
            sig = f"{name}:{json.dumps(args, sort_keys=True)}:{obs.url}"
            repeats = repeats + 1 if sig == last_sig else 0
            last_sig = sig
            if repeats >= 2:   # same action on same page 3x => stuck
                res = self._stuck(obs, "repeating the same action without progress", steps)
                if res is not None:
                    result = res
                    break
                repeats = 0
                continue
            try:
                if name == "finish":
                    ok = bool(args.get("success"))
                    missing = [o.name for o in self.task.outputs if o.name not in self.extracted]
                    if ok and missing:
                        self.history.append(f"{steps}. finish REJECTED: outputs not yet extracted: {missing}")
                        continue
                    result = DiscoveryResult(ok, args.get("summary", ""), None, steps,
                                             "finished" if ok else "agent_reported_failure")
                    self._last_obs = obs
                    break
                if name == "ask_human":
                    res = self._stuck(obs, args.get("reason", "agent asked for help"), steps)
                    if res is not None:
                        result = res
                        break
                    continue
                pending = self._execute(name, args, obs, steps)
                errors = 0
            except _Blocked as b:
                errors += 1
                self.history.append(f"{steps}. {name} BLOCKED by policy: {b}")
                self.log.event("policy_block", turn=steps, tool=name, reason=str(b))
            except Exception as e:  # element missing, timeout, etc: report back and let the model retry
                errors += 1
                msg = f"{type(e).__name__}: {str(e).splitlines()[0][:160]}"
                self.history.append(f"{steps}. {name} FAILED: {msg}")
                self.log.event("action_error", turn=steps, tool=name, error=msg)
            if errors >= 3:
                res = self._stuck(obs, "3 consecutive failed/blocked actions", steps)
                if res is not None:
                    result = res
                    break
                errors = 0
        self.log.event("discovery_end", success=result.success, stop_reason=result.stop_reason,
                       summary=result.summary, steps=steps, usage=dict(self.usage))
        if result.success:
            final_obs = getattr(self, "_last_obs", None) or self.s.observe()
            result.capability = self.compile(final_obs)
        return result

    # ------------------------------------------------------------------ actions
    def _execute(self, name: str, args: dict[str, Any], obs: Observation, n: int) -> Recorded:
        pre_url = self.s.url()
        if name == "navigate":
            url = args["url"]
            d = self.policy.check("navigate", Risk.REVERSIBLE, target_url=url)
            if not d.allow:
                raise _Blocked(d.reason)
            self.s.goto(url)
            step = Step(id=f"s{len(self.recorded) + 1}", intent=f"Navigate to {url}", action="navigate",
                        value=ValueSpec(template=url))
        elif name == "press":
            self.s.press(args["key"])
            step = Step(id=f"s{len(self.recorded) + 1}", intent=f"Press {args['key']}", action="press",
                        value=ValueSpec(template=args["key"]))
        else:
            ref = args["ref"]
            el = obs.get(ref)
            if el is None:
                raise LocatorNotFound(f"unknown ref {ref}")
            desc = (f"{el.role or el.tag} '{el.name or el.text}'" if el.interactive
                    else f"{el.role or el.tag} in row '{el.row_key}' (col {el.col})")
            risk = self.policy.classify(name, el.name, el.tag, el.type)
            d = self.policy.check(name, risk, url=self.s.url())
            if not d.allow:
                raise _Blocked(d.reason)
            target = self.s.build_target(el, desc)          # BEFORE acting: element is still on the page
            h = self.s.handle_for_ref(ref)
            sid = f"s{len(self.recorded) + 1}"
            if name == "click":
                self.s.click(h)
                step = Step(id=sid, intent=f"Click {desc}", action="click", risk=risk, target=target)
            elif name == "type":
                text = args["text"]
                self.s.type(h, substitute(text, self.params), submit=bool(args.get("submit")))
                step = Step(id=sid, intent=f"Type into {desc}", action="type", risk=risk, target=target,
                            value=ValueSpec(template=text), submit=bool(args.get("submit")))
            elif name == "select":
                self.s.select(h, args["value"])
                step = Step(id=sid, intent=f"Select '{args['value']}' in {desc}", action="select", risk=risk,
                            target=target, value=ValueSpec(template=args["value"]))
            elif name == "extract":
                out = args["output"]
                spec = next((o for o in self.task.outputs if o.name == out), None)
                if spec is None:
                    raise ValueError(f"unknown output '{out}'")
                text = self.s.read(h)
                if not text:
                    raise ValueError("element is empty (value may still be loading)")
                self.extracted[out] = text
                transform = "currency" if spec.type == ParamType.NUMBER else (
                    "integer" if spec.type == ParamType.INTEGER else "text")
                step = Step(id=sid, intent=f"Read {out} from {desc}", action="extract", risk=Risk.READ,
                            target=target, extract_as=out, transform=transform)
            else:
                raise ValueError(f"unknown tool {name}")
        rec = Recorded(step, pre_url)
        # a retried action replaces its earlier recording instead of duplicating it
        self.recorded = [r for r in self.recorded if not (
            r.step.action == step.action and r.step.target and step.target
            and r.step.target.description == step.target.description
            and (step.action != "extract" or r.step.extract_as == step.extract_as)
            and (step.action != "type" or r.step.value == step.value))]
        self.recorded.append(rec)
        shown = {k: ("[secret]" if k == "text" and "{{" in str(v) else v) for k, v in args.items()}
        what = step.target.description if step.target else ""
        self.history.append(f"{n}. {name} {json.dumps(shown)} {what} -> ok")
        self.log.event("action_executed", turn=n, step=step.id, action=name, risk=step.risk.value,
                       locator_candidates=[c.strategy for c in step.target.candidates] if step.target else [],
                       url_after=self.s.url())
        return rec

    def _finalize_expect(self, rec: Optional[Recorded], obs: Observation) -> None:
        """Derive the post-step checkpoint from the page we landed on (canonical URL + stable heading)."""
        if rec is None or rec.step.action in ("type", "select", "extract"):
            return
        heads = [e for e in obs.elements if e.role == "heading" and 3 < len(e.text) < 50
                 and not any(ch.isdigit() for ch in e.text)]
        # prefer the page title (h1) over sidebar/section headings (h2+), which repeat across pages
        heads.sort(key=lambda e: e.tag)
        heading = heads[0].text if heads else None
        rec.step.expect = Checkpoint(url_pattern=canonicalize_url(obs.url), text_visible=heading,
                                     description=f"landed on {canonicalize_url(obs.url)}")

    def _stuck(self, obs: Observation, reason: str, steps: int) -> Optional[DiscoveryResult]:
        self.log.event("stuck_detected", reason=reason, url=obs.url, turn=steps)
        if self.escalator is None:
            return DiscoveryResult(False, f"stuck: {reason}", None, steps, "stuck")
        shot = self.log.screenshot_path("stuck")
        self.s.screenshot(str(shot))
        req = InterventionRequest(capability=self.task.name, run_id=self.log.run_id, step_id=None,
                                  step_intent="discovery", reason=reason, category="agent_stuck", url=obs.url,
                                  screenshot=str(shot),
                                  page_text_excerpt=self.redactor.scrub(obs.text[:600]),
                                  options=["retry_step", "finish", "abort"])
        res = self.escalator.escalate(self.s, req)
        if res.action == "abort":
            return DiscoveryResult(False, f"stuck: {reason}; operator aborted", None, steps, "operator_abort")
        self.history.append(f"(a human intervened: {len(res.human_actions)} action(s); page may have changed)")
        return None

    # ------------------------------------------------------------------ compile
    def compile(self, final_obs: Observation) -> Capability:
        return compile_capability(self.task, self.profile, self.params, self.recorded, final_obs,
                                  self.model, self.log.run_id, self.redactor)


class _Blocked(Exception):
    pass


# ---------------------------------------------------------------------- compilation
def _param_templates(params: dict[str, str], task: TaskSpec) -> dict[str, str]:
    """value -> param name, for values safe to template (non-trivial)."""
    return {v: k for k, v in params.items() if v and len(v) >= 3}


def _tpl_str(s: str, values: dict[str, str], whole_only: bool) -> tuple[str, set[str]]:
    used: set[str] = set()
    for v, k in sorted(values.items(), key=lambda kv: -len(kv[0])):
        if s == v:
            return "{{" + k + "}}", {k}
        if not whole_only and v in s and v.isdigit():
            s = s.replace(v, "{{" + k + "}}")
            used.add(k)
    return s, used


def _tpl_locator(loc: Locator, values: dict[str, str]) -> tuple[Locator, set[str]]:
    used: set[str] = set()
    args: dict[str, Any] = {}
    for k, v in loc.args.items():
        if isinstance(v, str):
            nv, u = _tpl_str(v, values, whole_only=k not in ("selector", "expr"))
            args[k] = nv
            used |= u
        else:
            args[k] = v
    return loc.model_copy(update={"args": args}), used


def compile_capability(task: TaskSpec, profile: Profile, params: dict[str, str], recorded: list[Recorded],
                       final_obs: Observation, model: str, run_id: str, redactor: Redactor) -> Capability:
    values = _param_templates(params, task)
    conditions: list[Condition] = [c.model_copy(deep=True) for c in profile.conditions]
    steps: list[Step] = []
    for rec in recorded:
        st = rec.step.model_copy(deep=True)
        st.intent = _tpl_str(st.intent, values, whole_only=False)[0]
        if st.value and st.action in ("type", "select"):
            st.value = ValueSpec(template=_tpl_str(st.value.template, values, whole_only=True)[0])
        if st.target:
            cands, used_any = [], set()
            for c in st.target.candidates:
                tc, used = _tpl_locator(c, values)
                cands.append((tc, bool(used)))
                used_any |= used
            if used_any:
                # Parameterized target: anything instance-specific (structural paths, concrete text) would
                # silently select the *recorded* record on a different input. Keep only templated candidates.
                kept = [tc for tc, templated in cands if templated]
                st.target = Target(description=st.target.description, candidates=kept)
                for p in used_any:
                    cid = f"{p}_not_found"
                    if not any(c.id == cid for c in conditions):
                        conditions.append(Condition(
                            id=cid, kind=ConditionKind.BUSINESS_OUTCOME, detect=Detector(),
                            description=f"No record matched input '{p}' (legitimate answer, not a failure)."))
                    st.if_missing = cid
            else:
                st.target = Target(description=st.target.description, candidates=[tc for tc, _ in cands])
        if st.expect and st.expect.text_visible:
            st.expect.text_visible = _tpl_str(st.expect.text_visible, values, whole_only=False)[0]
        steps.append(st)
    for i, st in enumerate(steps, 1):
        st.id = f"s{i}"
    success = Checkpoint(url_pattern=None, description="all declared outputs extracted on a stable page")
    for st in reversed(steps):
        if st.expect:
            success = Checkpoint(url_pattern=st.expect.url_pattern, text_visible=st.expect.text_visible,
                                 description="final page reached")
            break
    inputs = [p.model_copy(update={"example": None if p.secret else p.example}) for p in task.inputs]
    cap = Capability(
        name=task.name, description=task.description, goal=task.goal,
        app=AppRef(profile=profile.name, entry_url=profile.entry_url,
                   allowed_hosts=list(profile.policy.allowed_hosts)),
        inputs=inputs, outputs=task.outputs, conditions=conditions, steps=steps, success=success,
        provenance=Provenance(model=model, discovery_run_id=run_id, agent_steps=len(recorded) - 1))
    # Defence in depth: refuse to emit an artifact that contains a secret value.
    blob = cap.model_dump_json()
    for p in task.inputs:
        if p.secret and params.get(p.name) and params[p.name] in blob:
            raise RuntimeError(f"secret input '{p.name}' leaked into artifact; refusing to save")
    return cap
