# Design Report: Computer-Use Automation System (CUA)

## 1. Architecture

### Core Design Philosophy
Financial institution back-offices rely on decades-old core banking, loan servicing, and teller applications. These interfaces rarely offer APIs, feature complex legacy layouts (deeply nested tables, framesets, zero semantic test IDs), and change slowly. 

Our architecture is built on the principle: **Discover with an LLM once; compile into a typed, parameterized capability; execute deterministically without an LLM in production.**

```
               +------------------------------------------------+
               |              DISCOVERY PHASE (LLM)             |
               |                                                |
Goal/Task ---> |  Agent Loop (Claude Haiku 4.5)                 |
               |       |  Observe UI via Surface Abstraction    |
               |       |  Rank & Verify Multi-Locators          |
               |       v                                        |
               |  Compiler (Template values, Extract Schemas)   |
               +------------------------------------------------+
                                       |
                                       v
               +------------------------------------------------+
               |          CAPABILITY ARTIFACT (.json)           |
               |  - Typed Inputs / Outputs                      |
               |  - Ordered Steps with Ranked Locators          |
               |  - Post-step Checkpoints & Condition Detectors |
               +------------------------------------------------+
                                       |
                                       v
               +------------------------------------------------+
               |          PRODUCTION REPLAY (ZERO LLM)          |
               |                                                |
Agent Call --> |  Replay Engine                                 |
(Typed Args)   |       |  Resolve Candidates (Rank 0 -> 9)      |
               |       |  Detect Business Outcomes vs Errors    |
               |       |  Verify Checkpoints                    |
               |       v                                        |
               |  ReplayResult (Success / Outcome / Failure)    |
               +------------------------------------------------+
                                       | (on stuck / auth loss)
                                       v
               +------------------------------------------------+
               |         HUMAN-IN-THE-LOOP ESCALATION           |
               |  - Single Session Ownership Lock               |
               |  - Transfer Same Live Browser Context          |
               |  - Record Human Actions -> Return Control      |
               +------------------------------------------------+
```

### Key Decisions & Trade-Offs

1. **Separation of Perception from Execution (`Surface` Seam)**:
   - *Decision*: The Agent, Compiler, and Replay Engine communicate exclusively with a `Surface` protocol (`observe()`, `build_target()`, `resolve()`, `act()`).
   - *Trade-off*: Adds an adapter layer over raw Playwright APIs, but decouples locator generation and action mechanics from the underlying platform. Adapting to desktop automation (e.g. Windows UI Automation / macOS Accessibility APIs) requires only a new `Surface` adapter, leaving the artifact schema, replay engine, and agent loop untouched.

2. **In-Situ Verification during Candidate Construction**:
   - *Decision*: Rather than guessing CSS selectors, `Surface.build_target()` generates multiple candidate strategies (semantic role/name, label, relative row-cell, stable attribute, and DOM path) and immediately tests each candidate against the live page before the step action is executed.
   - *Trade-off*: Increases discovery step latency by ~50–100ms, but guarantees that every locator stored in the capability was empirically verified to resolve uniquely to the target element on the actual page.

3. **Synchronous Single-Process Execution**:
   - *Decision*: Implemented as a clean, synchronous library and CLI rather than distributed workers or message queues.
   - *Trade-off*: Avoids premature distributed systems complexity (Redis, Celery, Kubernetes) while making debugging, tracing, and local test verification trivial.

---

## 2. Artifact Schema

The artifact schema (`Capability` in `cua/schema.py`) defines an agent-invocable contract rather than a raw browser action trace.

### Structural Anatomy
```json
{
  "schema_version": "1.0",
  "name": "get_account_details",
  "version": 1,
  "description": "Log in to the bank back office and read account details...",
  "status": "approved",
  "confidence": { "runs": 3, "successes": 3, "score": 1.0 },
  "inputs": [
    { "name": "username", "type": "string", "required": true },
    { "name": "password", "type": "string", "required": true, "secret": true },
    { "name": "account_id", "type": "integer", "required": true, "pattern": "\\d{3,12}" }
  ],
  "outputs": [
    { "name": "account_type", "type": "string" },
    { "name": "balance", "type": "number" }
  ],
  "conditions": [ ... ],
  "steps": [ ... ],
  "success": { "url_pattern": "/parabank/activity.htm?id=:id", "text_visible": "Account Details" }
}
```

### Key Schema Design Choices
- **Decoupled Step Contract**: Each step declares an `action` (`navigate`, `click`, `type`, `select`, `extract`), an explicit `risk` classification (`read`, `reversible`, `irreversible`), and an optional post-step `expect` checkpoint.
- **Ranked Target Candidates**: Targets contain an ordered array of locators (`rank` 0 to 9) with explicit notes explaining their durability (e.g., `role` with rank 0 vs. structural `css` with rank 9).
- **Templated Parameterization**: Concrete values supplied during discovery are abstracted into `{{param}}` templates. Targets identified via runtime parameter values (e.g. clicking account row `13344`) are automatically parameterized, and all instance-specific selectors are pruned so replay can navigate to any valid account.
- **Secret Isolation**: Input parameters marked `secret: true` are scrubbed from serialized examples and prohibited from command-line arguments. The compiler performs a defense-in-depth scan to guarantee no secret value exists in the emitted JSON artifact.

---

## 3. Determinism & Error Handling

Replay must succeed unattended thousands of times without invoking an LLM.

### Deterministic Replay Mechanics
1. **Rank-Ordered Locator Resolution**:
   - The replay engine iterates through candidates by rank:
     - *Rank 0*: ARIA Role + Accessible Name (`role:link, name:{{account_id}}`)
     - *Rank 1*: Form Label or Table Row Context (`row_cell: row='Balance:', col=1`)
     - *Rank 2*: Visible Button/Link Text
     - *Rank 3*: Stable Form `name` or `id` attributes
     - *Rank 9*: Structural DOM Path (emergency fallback)
   - If a primary locator fails (e.g. due to minor markup drift), a lower-ranked candidate is used, the step is logged as `recovered`, and a drift signal is emitted.
2. **Explicit Post-Step Checkpoints**:
   - Rather than arbitrary `sleep` statements, every state-transition step asserts a `Checkpoint` consisting of a canonical URL pattern (e.g. `/parabank/activity.htm?id=:id`) and visible page text before the next step commences.
3. **Data Extraction Coercion**:
   - Extracted text is polled until non-empty (accounting for asynchronous XHR rendering in legacy pages) and coerced into typed primitives (`currency` -> `float`, `integer` -> `int`).

### Runtime Error Taxonomy
A core failure mode of automation is conflating business outcomes with system crashes. Our engine cleanly separates three classes of conditions:

| Category | Definition | System Response | Example |
| :--- | :--- | :--- | :--- |
| **Business Outcome** | A legitimate operational state that the caller needs to handle. | Execution halts cleanly; returns structured `Outcome(code, message)` with HTTP/exit status 2. | `account_id_not_found`, `invalid_credentials` |
| **Recoverable Condition** | Transient interstitial or delay that can be self-healed. | Applies bound recovery (`dismiss`, `wait_retry`, `reload`), increments retry count, and resumes step. | Maintenance modal popup, slow XHR load |
| **Hard Failure** | System or application invariant violation. | Captures full failure context (step ID, expected, observed, URL, screenshot); triggers escalation if operator is configured. | `session_expired`, `checkpoint_failed`, `locator_not_found` |

---

## 4. Heterogeneity & Multi-Tenant Reuse

### Surface Abstraction (Web, Legacy Web, Desktop)
The platform is designed around the reality that core banking applications encompass modern single-page apps, legacy frameset/table-based portals, and terminal/desktop thick clients.

- **Legacy Web Surfaces**:
  - Legacy applications frequently embed forms inside `<iframe>` or `<frameset>` tags without proper labels or semantic IDs.
  - The `WebSurface` implementation automatically scans all active page frames during `observe()` and locator resolution.
  - For data tables lacking identifiers, the `row_cell` strategy anchors lookups relative to visible row headers (`tr:has(td:text-is("Balance:")) > td:nth-child(2)`).
- **Extending to Desktop Thick Clients**:
  - A desktop surface adapter (e.g., `WindowsUiaSurface` using `pywinauto` or UI Automation COM interfaces) implements the identical `Surface` protocol.
  - Strategy types like `role` map directly to UI Automation Control Types (`Button`, `Edit`, `ListItem`), and `row_cell` maps to Grid/Table pattern providers. The capability schema and replay engine require no changes.

### Multi-Tenant Generalization & Parameterized Overrides
In enterprise banking, hundreds of financial institutions run the same vendor core (e.g., FIS, Fiserv, Jack Henry) with different branding, custom CSS, modified sub-domains, and localized routes.

1. **URL Canonicalization**:
   - All URLs are canonicalized at recording time: `http://tenant1.bankcore.com/app/user/987654` becomes `/app/user/:id`.
   - Hostnames and concrete entity IDs are stripped, allowing the capability to run across any tenant's deployment base.
2. **Profile Hierarchy with Overrides**:
   - Base vendor applications are defined in a profile (`profiles/parabank.json`).
   - Tenant-specific variations are expressed via shallow-merged overrides:
     ```python
     tenant_profile = load_profile("parabank", overrides={
         "allowed_hosts": ["tenantB.internal.bank:8443"],
         "entry_url": "https://tenantB.internal.bank:8443/custom_login.htm"
     })
     ```
3. **Drift Detection**:
   - When a tenant updates their vendor patch level, steps that fall back from Rank 0 to Rank 3+ candidates record `locator_fallback` warnings in the event stream, allowing engineering to proactively re-align primary selectors before failures occur.

---

## 5. Escalation & Human Handoff

When automation encounters an ambiguous, stuck, or session-invalidating state, it must escalate without losing the live work state.

### Control State Model
At any point in time, session ownership belongs to exactly one entity: `AUTOMATION` or `HUMAN`.
```python
class Owner(str, Enum):
    AUTOMATION = "automation"
    HUMAN = "human"
```
Before executing any action, the automation engine enforces `control.require_automation()`. If human intervention is underway, automated commands cannot race or interfere with operator input.

### Handoff Protocol
1. **Detection**:
   - Hard failures with `escalate: true` (e.g. `session_expired`) or repeated loop detection (agent taking the identical action 3x without progress) construct an `InterventionRequest`.
   - The request bundles the capability name, run ID, failed step ID, plain-English reason, target URL, live screenshot, and scrubbed DOM text excerpt.
2. **Session Transfer**:
   - Control ownership transitions to `HUMAN`.
   - The engine enables DOM-level event listeners (`__cua_human`) on the **same live Playwright browser session** (preserving cookies, active DOM, and server-side session handles).
3. **Operator Resolution**:
   - *Headless / CI*: A `ScriptedOperator` executes programmatic remedial actions (such as logging back in).
   - *Production / Interactive*: A `WebOperator` serves a lightweight intervention console displaying the screenshot, context, and action options (`retry_step`, `skip_step`, `finish`, `abort`).
4. **Resumption**:
   - The operator performs the necessary action in the live browser and clicks a resolution action.
   - Captured human interactions are appended to the structured evidence log.
   - Ownership transfers back to `AUTOMATION`, and the replay engine resumes execution from the designated step.

---

## 6. Safety & Guardrails

Automating banking back-offices carries significant regulatory and financial risk. The system enforces strict multi-layered safeguards:

### 1. Network & Navigation Allowlists
- Configured at the profile level (`allowed_hosts`).
- Enforced at both the agent planning layer and at the browser network routing layer (`page.route("**/*")`). Any request or navigation targeting an unapproved host is immediately aborted (`blockedbyclient`).

### 2. Action Classification & Irreversible Action Gating
Every action is categorized into a risk tier:
- `READ`: Data extraction only (`extract`).
- `REVERSIBLE`: Page navigation, typing without submission, dropdown selection.
- `IRREVERSIBLE`: State-modifying actions (fund transfers, account creation, record deletion).

By default, any step classified as `IRREVERSIBLE` is blocked by policy unless the run explicitly provides `--allow-irreversible` or a human operator authorizes the step during live escalation.

### 3. PII & Secret Redaction
- Regulated customer data (Social Security Numbers, Credit Card numbers matching Luhn checksums, Phone Numbers, Email addresses, and Account Numbers) are scrubbed via streaming regex and pattern replacement before writing to logs, traces, or terminal outputs.
- Passwords and authentication tokens are tagged `secret: true` and replaced with placeholder strings (`[REDACTED]`), preventing credentials from being saved into artifacts or sent to the LLM.

---

## 7. Cuts & Next Steps

### Deliberate Cuts (What We Left Out)
1. **Full WebRTC / VNC Operator Console**:
   - Rather than building a heavyweight streaming video operator workspace, we implemented the live session transfer seam using Playwright's attached browser context paired with a lightweight HTTP control plane. The state transfer and interaction capture protocols are fully functional and ready to connect to a centralized VNC/CDP streaming daemon.
2. **Multi-Tenant Database Infrastructure**:
   - We implemented tenant customization via file-based profile overlays rather than spinning up a multi-tenant PostgreSQL schema or microservice fleet, keeping the core abstractions lean and testable.
3. **Pixel-Level Computer Vision Control**:
   - We biased toward semantic ARIA trees and DOM structures over raw screenshot coordinate clicking, as coordinate clicks are notoriously brittle to window scaling, OS DPI settings, and font rendering variations.

### Next Steps (What We'd Build Next)
1. **Assisted Replay Fallback (Bounded LLM Recovery)**:
   - If an approved capability encounters an unresolvable locator on step $N$, invoke the LLM for a *single bounded step* to locate the migrated control, verify the post-step checkpoint, and record the repair as a draft patch for human review.
2. **Visual Anchor Strategies for Canvas/Desktop Surfaces**:
   - Incorporate relative coordinate anchoring based on OCR text matches (e.g. "click 40px to the right of text 'Account Number'") for non-standard canvas or desktop thick clients.
3. **Automated Migration Pull Requests**:
   - When replay logs detect that a step consistently relies on Rank 2+ fallback locators due to application updates, automatically emit a suggested artifact version upgrade with updated primary selectors.
