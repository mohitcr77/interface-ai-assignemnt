"""Surface abstraction + Playwright (web) implementation.

The seam between "how we perceive / act on a surface" and "the recorded flow":

    Surface.observe()            -> what the agent sees (elements with refs)
    Surface.build_target(ref)    -> ranked, *verified* locator candidates for a ref
    Surface.resolve(target)      -> a live handle (or NotFound/Ambiguous)
    Surface.click/type/...(h)    -> act on a handle

The agent loop, artifact schema and replay engine only talk to this interface.
A legacy-web (frames / tables) surface is already handled here (we search every
frame, and use row/column relative locators). A desktop surface would implement
the same methods on top of UIA / AX APIs (new `strategy` values, same Target).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol
from urllib.parse import urlparse

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from .policy import Policy
from .schema import Locator, Target


class LocatorNotFound(Exception):
    pass


class LocatorAmbiguous(Exception):
    pass


@dataclass
class Element:
    ref: str
    tag: str
    role: str
    name: str
    text: str
    type: str = ""
    href: str = ""
    value: str = ""
    row_key: str = ""
    col: int = -1
    header: str = ""
    disabled: bool = False
    interactive: bool = False
    attrs: dict[str, str] = field(default_factory=dict)
    path: str = ""


@dataclass
class Observation:
    url: str
    title: str
    elements: list[Element]
    text: str

    def get(self, ref: str) -> Optional[Element]:
        return next((e for e in self.elements if e.ref == ref), None)

    def render(self) -> str:
        lines = [f"URL: {self.url}", f"TITLE: {self.title}", "ELEMENTS:"]
        for e in self.elements:
            if e.interactive:
                extra = ""
                if e.href:
                    extra += f" href={e.href}"
                if e.type:
                    extra += f" type={e.type}"
                if e.value:
                    extra += f" value={e.value!r}"
                if e.disabled:
                    extra += " (disabled)"
                lines.append(f'[{e.ref}] {e.role or e.tag} "{e.name}"{extra}')
            else:
                ctx = ""
                if e.row_key and e.col >= 0:
                    ctx = f' (row "{e.row_key}", col {e.col}' + (f', header "{e.header}")' if e.header else ")")
                lines.append(f'[{e.ref}] {e.role or e.tag} "{e.text}"{ctx}')
        lines.append("PAGE TEXT (truncated):")
        lines.append(self.text)
        return "\n".join(lines)


class Surface(Protocol):
    """Contract every surface (web, legacy web, desktop) implements."""

    def goto(self, url: str) -> None: ...
    def url(self) -> str: ...
    def body_text(self) -> str: ...
    def observe(self) -> Observation: ...
    def build_target(self, el: Element, description: str) -> Target: ...
    def resolve(self, target: Target, params: dict[str, str], timeout_ms: int) -> "Resolved": ...
    def screenshot(self, path: str) -> None: ...
    def close(self) -> None: ...


@dataclass
class Resolved:
    handle: Any
    candidate_index: int
    description: str
    fallback_used: bool


# --------------------------------------------------------------------------- JS

_SNAPSHOT_JS = r"""
(prefix) => {
  window.__cua_els = [];
  const out = [];
  const vis = el => {
    const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const role = el => {
    const r = el.getAttribute('role'); if (r) return r;
    const t = el.tagName.toLowerCase(); const ty = (el.getAttribute('type') || 'text').toLowerCase();
    if (t === 'a' && el.hasAttribute('href')) return 'link';
    if (t === 'button') return 'button';
    if (t === 'input') {
      if (['submit', 'button', 'reset', 'image'].includes(ty)) return 'button';
      if (ty === 'checkbox') return 'checkbox'; if (ty === 'radio') return 'radio';
      if (ty === 'password' || ty === 'hidden') return '';
      return 'textbox';
    }
    if (t === 'textarea') return 'textbox'; if (t === 'select') return 'combobox';
    if (/^h[1-6]$/.test(t)) return 'heading';
    if (t === 'td') return 'cell'; if (t === 'th') return 'columnheader';
    return '';
  };
  const name = el => {
    const al = el.getAttribute('aria-label'); if (al) return clean(al);
    if (el.labels && el.labels.length) return clean([...el.labels].map(l => l.innerText).join(' '));
    const t = el.tagName.toLowerCase(); const ty = (el.getAttribute('type') || '').toLowerCase();
    if (t === 'input' && ['submit', 'button', 'reset'].includes(ty)) return clean(el.value);
    if (t === 'input' && ty === 'image') return clean(el.alt || el.title);
    if (t === 'input' || t === 'textarea' || t === 'select') {
      // legacy pages: a caption in the previous sibling / cell, no <label>
      const ph = el.getAttribute('placeholder'); if (ph) return clean(ph);
      let prev = el.parentElement && el.parentElement.previousElementSibling;
      if (prev && clean(prev.innerText).length < 40) return clean(prev.innerText);
      const cell = el.closest('td'); if (cell && cell.previousElementSibling && clean(cell.previousElementSibling.innerText).length < 40)
        return clean(cell.previousElementSibling.innerText);
      return clean(el.getAttribute('name') || el.getAttribute('title'));
    }
    return clean(el.innerText || el.getAttribute('title'));
  };
  const cssPath = el => {
    const parts = [];
    while (el && el.nodeType === 1 && el.tagName.toLowerCase() !== 'html') {
      let i = 1, s = el; while ((s = s.previousElementSibling)) if (s.tagName === el.tagName) i++;
      parts.unshift(el.tagName.toLowerCase() + ':nth-of-type(' + i + ')'); el = el.parentElement;
    }
    return 'html > ' + parts.join(' > ');
  };
  const interactive = 'a[href], button, input:not([type=hidden]), select, textarea, [role=button], [role=link], [onclick]';
  const readable = 'td, th, h1, h2, h3, h4, label, p, span, li, b, div';
  const seen = new Set();
  const add = (el, isInt) => {
    if (seen.has(el) || !vis(el)) return; seen.add(el);
    const t = el.tagName.toLowerCase(); const ty = (el.getAttribute('type') || '').toLowerCase();
    const idx = window.__cua_els.length; window.__cua_els.push(el);
    let rowKey = '', col = -1, header = '';
    const cell = el.closest('td, th');
    if (cell && cell.parentElement && cell.parentElement.tagName === 'TR') {
      const tr = cell.parentElement; col = [...tr.children].indexOf(cell);
      rowKey = clean(tr.children[0] ? tr.children[0].innerText : '');
      const tbl = tr.closest('table'); const hr = tbl && tbl.querySelector('tr');
      if (hr && hr.children[col]) header = clean(hr.children[col].innerText);
    }
    out.push({
      ref: prefix + 'e' + idx, tag: t, role: role(el), name: name(el),
      text: clean(el.innerText || el.textContent).slice(0, 100), type: ty,
      href: el.getAttribute('href') || '',
      value: (ty === 'password') ? '' : clean(el.value || '').slice(0, 60),
      rowKey: rowKey.slice(0, 60), col, header: header.slice(0, 40),
      disabled: !!el.disabled, interactive: isInt,
      attrs: { id: el.id || '', name: el.getAttribute('name') || '', placeholder: el.getAttribute('placeholder') || '',
               value: (ty === 'submit' || ty === 'button') ? (el.value || '') : '' },
      path: cssPath(el),
    });
  };
  document.querySelectorAll(interactive).forEach(el => add(el, true));
  document.querySelectorAll(readable).forEach(el => {
    if (seen.has(el)) return;
    // leaf-ish text holders only: avoid dumping whole containers
    const hasBlockChild = [...el.children].some(c => /^(div|table|ul|p|form|tr|td)$/i.test(c.tagName));
    const own = clean(el.innerText);
    if (!own || own.length > 100 || (hasBlockChild && el.tagName.toLowerCase() !== 'td')) return;
    if (el.closest('a, button')) return;
    add(el, false);
  });
  return out;
}
"""

_HUMAN_JS = r"""
() => {
  if (window.__cua_human_installed) return; window.__cua_human_installed = true;
  const desc = el => {
    const t = el.tagName.toLowerCase();
    return t + (el.id ? '#' + el.id : '') + (el.getAttribute('name') ? '[name=' + el.getAttribute('name') + ']' : '') +
           ' "' + ((el.innerText || el.value || '').replace(/\s+/g, ' ').trim().slice(0, 40)) + '"';
  };
  document.addEventListener('click', e => window.__cua_human && window.__cua_human({k: 'click', target: desc(e.target)}), true);
  document.addEventListener('change', e => {
    const el = e.target; const secret = (el.type || '').toLowerCase() === 'password';
    window.__cua_human && window.__cua_human({k: 'input', target: desc(el), value: secret ? '[REDACTED]' : String(el.value || '').slice(0, 80)});
  }, true);
}
"""


def _q(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _subst(obj: Any, params: dict[str, str]) -> Any:
    if isinstance(obj, str):
        return re.sub(r"\{\{(\w+)\}\}", lambda m: str(params.get(m.group(1), m.group(0))), obj)
    if isinstance(obj, dict):
        return {k: _subst(v, params) for k, v in obj.items()}
    return obj


class WebSurface:
    """Playwright-backed surface. Sync API, single page, same-origin frames supported."""

    def __init__(self, policy: Policy, headless: bool = True, slow_mo: int = 0) -> None:
        self.policy = policy
        self._pw = sync_playwright().start()
        self._browser: Browser = self._pw.chromium.launch(headless=headless, slow_mo=slow_mo)
        self._ctx: BrowserContext = self._browser.new_context(viewport={"width": 1280, "height": 900})
        self.blocked_requests: list[str] = []
        self._ctx.route("**/*", self._route)
        self.page: Page = self._ctx.new_page()
        self.page.set_default_timeout(8000)
        self.human_actions: list[dict[str, Any]] = []
        self._human_on = False

    # -- network-level allowlist: nothing outside the allowed hosts is ever fetched
    def _route(self, route: Any, request: Any) -> None:
        if self.policy.url_allowed(request.url):
            route.continue_()
        else:
            self.blocked_requests.append(request.url)
            route.abort("blockedbyclient")

    # -- basic state
    def goto(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded")
        self.settle()

    def url(self) -> str:
        return self.page.url

    def title(self) -> str:
        try:
            return self.page.title()
        except Exception:
            return ""

    def body_text(self) -> str:
        parts = []
        for fr in self.page.frames:
            try:
                parts.append(fr.inner_text("body", timeout=1500))
            except Exception:
                pass
        return "\n".join(parts)

    def settle(self, timeout_ms: int = 3000) -> None:
        """Best-effort wait for navigation + XHR to quiet down (legacy apps load via XHR)."""
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            self.page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            pass

    def screenshot(self, path: str) -> None:
        try:
            self.page.screenshot(path=path, full_page=False)
        except Exception:
            pass

    # -- perception
    def observe(self, max_elements: int = 140) -> Observation:
        elements: list[Element] = []
        for i, fr in enumerate(self.page.frames):
            prefix = "" if fr == self.page.main_frame else f"f{i}"
            try:
                raw = fr.evaluate(_SNAPSHOT_JS, prefix)
            except Exception:
                continue
            for r in raw:
                elements.append(Element(
                    ref=r["ref"], tag=r["tag"], role=r["role"], name=r["name"], text=r["text"],
                    type=r["type"], href=r["href"], value=r["value"], row_key=r["rowKey"],
                    col=r["col"], header=r["header"], disabled=r["disabled"],
                    interactive=r["interactive"], attrs=r["attrs"], path=r["path"]))
        elements = elements[:max_elements]
        text = re.sub(r"\n\s*\n+", "\n", self.body_text())[:1200]
        return Observation(self.url(), self.title(), elements, text)

    def _frame_and_index(self, ref: str) -> tuple[Any, int]:
        m = re.fullmatch(r"(f\d+)?e(\d+)", ref)
        if not m:
            raise LocatorNotFound(f"bad ref {ref}")
        fr = self.page.main_frame if not m.group(1) else self.page.frames[int(m.group(1)[1:])]
        return fr, int(m.group(2))

    def handle_for_ref(self, ref: str) -> Any:
        fr, idx = self._frame_and_index(ref)
        h = fr.evaluate_handle("(i) => window.__cua_els && window.__cua_els[i]", idx).as_element()
        if h is None:
            raise LocatorNotFound(f"stale ref {ref}")
        return h

    # -- locator construction / resolution
    def _playwright_locator(self, frame: Any, loc: Locator, params: dict[str, str]) -> Any:
        a = _subst(loc.args, params)
        s = loc.strategy
        if s == "role":
            return frame.get_by_role(a["role"], name=a.get("name"), exact=a.get("exact", True))
        if s == "label":
            return frame.get_by_label(a["text"], exact=True)
        if s == "text":
            return frame.get_by_text(a["text"], exact=a.get("exact", True))
        if s == "css":
            return frame.locator(a["selector"])
        if s == "xpath":
            return frame.locator("xpath=" + a["expr"])
        if s == "row_cell":
            return frame.locator(
                f'tr:has(> :nth-child(1):text-is("{_q(a["row_text"])}")) > :nth-child({int(a["column"]) + 1})')
        raise ValueError(f"unknown strategy {s}")

    def _matches(self, loc: Locator, params: dict[str, str]) -> list[Any]:
        found = []
        for fr in self.page.frames:
            try:
                pl = self._playwright_locator(fr, loc, params).filter(visible=True)
                n = pl.count()
            except Exception:
                continue
            found.extend(pl.nth(i) for i in range(n))
        return found

    def build_target(self, el: Element, description: str) -> Target:
        """Rank candidate locators for `el` and keep only those that uniquely re-resolve to it."""
        cands: list[Locator] = []
        a = el.attrs
        stable_id = a.get("id") and not re.search(r"\d{4,}|[0-9a-f]{8}", a["id"])
        if el.interactive and el.role and el.name and el.role in ("link", "button", "textbox", "combobox", "checkbox", "radio"):
            cands.append(Locator(strategy="role", rank=0, args={"role": el.role, "name": el.name, "exact": True},
                                 note="ARIA role + accessible name: semantic, survives markup changes"))
        if el.tag == "input" and el.type in ("", "text", "email", "tel", "number", "search") and el.name:
            cands.append(Locator(strategy="label", rank=1, args={"text": el.name},
                                 note="associated label text"))
        if el.row_key and el.col >= 0 and not el.interactive:
            cands.append(Locator(strategy="row_cell", rank=1, args={"row_text": el.row_key, "column": el.col},
                                 note="cell relative to its row label: stable for tables without ids"))
        if el.interactive and el.tag in ("a", "button") and el.text and len(el.text) < 60:
            cands.append(Locator(strategy="text", rank=2, args={"text": el.text, "exact": True},
                                 note="visible text"))
        if stable_id:
            cands.append(Locator(strategy="css", rank=3, args={"selector": f"#{a['id']}"}, note="element id"))
        if a.get("name") and el.tag in ("input", "select", "textarea", "button"):
            cands.append(Locator(strategy="css", rank=3, args={"selector": f'{el.tag}[name="{_q(a["name"])}"]'},
                                 note="name attribute (form field contract; stable on legacy forms)"))
        if el.tag == "input" and el.type in ("submit", "button") and a.get("value"):
            cands.append(Locator(strategy="css", rank=3,
                                 args={"selector": f'input[type="{el.type}"][value="{_q(a["value"])}"]'},
                                 note="submit control by value"))
        if el.tag == "a" and el.href:
            cands.append(Locator(strategy="css", rank=4, args={"selector": f'a[href="{_q(el.href)}"]'},
                                 note="href (breaks if routes change)"))
        if el.path:
            cands.append(Locator(strategy="css", rank=9, args={"selector": el.path},
                                 note="structural path: brittle, last resort"))
        # verification: candidate must resolve uniquely
        target_handle = self.handle_for_ref(el.ref)
        verified: list[Locator] = []
        for c in cands:
            ms = self._matches(c, {})
            if len(ms) == 1:
                try:
                    same = ms[0].element_handle().evaluate("(a, b) => a === b", target_handle)
                except Exception:
                    same = False
                if same:
                    verified.append(c)
        if not verified:
            raise LocatorNotFound(f"no unique locator for {el.ref} ({description})")
        return Target(description=description, candidates=sorted(verified, key=lambda c: c.rank))

    def resolve(self, target: Target, params: dict[str, str], timeout_ms: int = 8000) -> Resolved:
        """Poll candidates in rank order until exactly one visible match; handles late XHR content."""
        deadline = time.time() + timeout_ms / 1000
        ambiguous = False
        while True:
            for i, c in enumerate(sorted(target.candidates, key=lambda c: c.rank)):
                ms = self._matches(c, params)
                if len(ms) == 1:
                    return Resolved(ms[0], i, f"{c.strategy}:{_subst(c.args, params)}", i > 0)
                if len(ms) > 1:
                    ambiguous = True
            if time.time() > deadline:
                if ambiguous:
                    raise LocatorAmbiguous(target.description)
                raise LocatorNotFound(target.description)
            time.sleep(0.2)

    # -- actions on a handle (Locator or ElementHandle)
    def click(self, h: Any) -> None:
        h.click()
        self.settle()

    def type(self, h: Any, text: str, submit: bool = False) -> None:
        h.fill(text)
        if submit:
            h.press("Enter")
            self.settle()

    def select(self, h: Any, value: str) -> None:
        try:
            h.select_option(label=value)
        except Exception:
            h.select_option(value=value)

    def press(self, key: str) -> None:
        self.page.keyboard.press(key)
        self.settle()

    def read(self, h: Any) -> str:
        return h.inner_text().strip()

    # -- human takeover support (same live page; actions are recorded)
    def start_human_recording(self) -> None:
        self.human_actions = []
        if not self._human_on:
            self.page.expose_binding("__cua_human", lambda _s, rec: self.human_actions.append(rec))
            self.page.on("framenavigated", lambda fr: self._human_nav(fr))
            self._human_on = True
        self._human_active = True
        for fr in self.page.frames:
            try:
                fr.evaluate(_HUMAN_JS)
            except Exception:
                pass

    def _human_nav(self, fr: Any) -> None:
        if getattr(self, "_human_active", False) and fr == self.page.main_frame:
            self.human_actions.append({"k": "navigate", "url": fr.url})
            try:
                fr.evaluate(_HUMAN_JS)
            except Exception:
                pass

    def stop_human_recording(self) -> list[dict[str, Any]]:
        self._human_active = False
        return list(self.human_actions)

    def close(self) -> None:
        try:
            self._ctx.close()
            self._browser.close()
        finally:
            self._pw.stop()
