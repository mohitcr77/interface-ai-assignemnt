# Computer-Use Automation System (CUA)

A reliable, safe computer-use automation engine for legacy back-office financial applications that lack APIs.

The system uses an LLM (Claude Haiku 4.5 via Anthropic API) to discover how to navigate the target application once, compiles that experience into a typed, versioned, parameterized **Capability Artifact**, and executes that capability **deterministically without an LLM in production**.

---

## Key Features

1. **Goal-Driven LLM Discovery**: Observes live UI state, reasons over compact element hierarchies (including non-semantic legacy tables & frames), acts via verified multi-candidate locators, and halts when declared outputs are extracted.
2. **Typed Capability Artifact**: Formats the workflow into an agent-callable contract complete with typed input parameters, declared output schemas, URL canonicalization, and explicit failure/condition detectors.
3. **Deterministic Replay (No LLM in Loop)**: Executes in <2 seconds with strict checkpoint verification, locator fallback chains, and sub-second transitions.
4. **Three-Tier Condition Taxonomy**:
   - **Business Outcomes**: Legitimate operational answers (e.g. `account_id_not_found`, `invalid_credentials`) returned cleanly to the caller without crashing.
   - **Recoverable Interstitials**: Bounded auto-recovery (e.g. dismiss maintenance modal, retry transient network hitch).
   - **Hard Failures**: Surfaced with complete debug context (step ID, expected vs. observed state, URL, and screenshot).
5. **Live Session Human Handoff**: When unrecoverable or policy-gated states occur, the system pauses automation, transfers ownership of the *exact same live browser session* to a human operator, records human actions, and resumes seamlessly upon operator handoff.
6. **Enterprise Safety & Data Redaction**: Host-level network allowlists, configurable action/risk policies (irreversible actions gated by default), and streaming regex + Luhn scrubbing of PII/credentials before logging.
7. **Agent-Facing Catalog & Approval Gating**: Exposes capabilities as Anthropic/OpenAI function-calling tool definitions. Capabilities require a verified stability threshold (`draft` → `approved`) before unattended production execution is permitted.

---

## Prerequisites & Setup

### Requirements
- **Python**: 3.13+
- **Package Manager**: [`uv`](https://github.com/astral-sh/uv) (recommended) or standard `pip`
- **Docker**: For running the local legacy banking target (ParaBank)
- **Anthropic API Key**: For running live discovery (`ANTHROPIC_API_KEY`)

### 1. Install Dependencies
```bash
# Clone the repository
git clone <repo-url>
cd Interface.ai

# Install Python packages and Playwright browser binaries
uv sync
uv run playwright install chromium
```

### 2. Configure Environment
Create a `.env` file in the project root:
```bash
ANTHROPIC_API_KEY=your_anthropic_api_key_here
CUA_PARAM_PASSWORD=demo
```

### 3. Launch Target Banking Application (ParaBank)
ParaBank provides a realistic legacy banking interface with server-rendered frames, tabular data layouts, and absence of modern test IDs:
```bash
docker run -d --name parabank -p 8080:8080 parasoft/parabank
```
*Wait ~15-20 seconds for the Java servlet container to initialize at `http://localhost:8080/parabank/index.htm`.*

---

## Demo Walkthrough

### Step 1: Goal-Driven Discovery (LLM in the loop)
Runs the Claude Haiku 4.5 agent to discover how to fulfill the task goal, recording verified locator candidates and compiling `artifacts/get_account_details.v1.json`:

```bash
uv run python -m cua discover \
  --task tasks/get_account_details.json \
  --param username=john \
  --param account_id=13344 \
  --out evidence/discovery
```

### Step 2: Deterministic Replay (Zero LLM calls)
Replays the generated artifact with different parameters, verifying that it extracts the real account type and balance deterministically in <2 seconds:

```bash
uv run python -m cua replay get_account_details \
  --allow-draft \
  --param username=john \
  --param account_id=12456 \
  --out evidence/replay-success
```

### Step 3: Run Stability & Approval Gating
Run multi-run stability testing to verify determinism and promote the capability from `draft` to `approved`:

```bash
# Replay 3 times and calculate confidence score
uv run python -m cua stability get_account_details --runs 3 --param username=john --param account_id=12456

# Approve the capability for unattended execution (requires >= 80% pass rate)
uv run python -m cua approve get_account_details

# Inspect agent-facing tool catalog
uv run python -m cua catalog
```

### Step 4: Agent Invocation
Invoke the capability as an external AI agent using typed JSON inputs:

```bash
uv run python -m cua invoke get_account_details --args '{"username": "john", "account_id": 12456}'
```

---

## Verifying Error Handling & Human Handoff Scenarios

Run the end-to-end scenario test suite to generate and verify all operational paths:

```bash
bash scripts/run_scenarios.sh
```

This runs and records structured logs and screenshots for:
1. **Happy Path**: Successful retrieval of account type and balance.
2. **Business Outcome (`account_id_not_found`)**: Querying non-existent account `99999` cleanly reports business code `account_id_not_found` rather than crashing.
3. **Business Outcome (`invalid_credentials`)**: Supplying an incorrect password cleanly reports `invalid_credentials`.
4. **Input Validation (`invalid_input`)**: Passing invalid format `account_id=abc` is rejected prior to execution.
5. **Recoverable Condition (`unexpected_dialog`)**: Injects an unexpected maintenance modal banner; the replay engine detects it, dismisses it, and completes the step.
6. **Hard Failure Unattended (`session_expired`)**: Simulates cookie invalidation mid-flow; system reports `session_expired` failure and halts.
7. **Human-in-the-Loop Handoff (`session_handoff`)**: Simulates cookie invalidation with an attached operator; automation transfers the live browser session to the operator, records the operator re-authenticating, transfers control back, and completes successfully.

---

## Running Automated Tests

```bash
uv run pytest -v
```

---

## Repository Structure

```text
├── artifacts/              # Compiled, versioned Capability Artifacts (JSON)
├── cua/                    # Core automation package
│   ├── agent.py            # LLM discovery loop & artifact compiler
│   ├── canon.py            # Canonical URL pattern matching & parameter templating
│   ├── cli.py              # Unified command line interface
│   ├── escalation.py       # Session ownership state & human handoff protocols
│   ├── evidence.py         # Structured JSONL run logger & screenshot manager
│   ├── policy.py           # Safety guardrails, domain allowlists, and risk classification
│   ├── profile.py          # Application profile specifications
│   ├── redact.py           # PII and credential redaction engine
│   ├── replay.py           # Deterministic replay engine
│   ├── schema.py           # Pydantic data models for capabilities, steps, and results
│   ├── store.py            # Artifact storage, catalog generator, and stability scoring
│   └── surface.py          # Surface abstraction & Playwright browser adapter
├── evidence/               # Run logs, event streams (JSONL), and screenshots
├── profiles/               # Target application profiles (ParaBank)
├── scripts/                # Helper automation scripts
├── tasks/                  # Task definitions for discovery
├── tests/                  # Pytest unit and integration tests
├── pyproject.toml          # Python project specification and dependencies
├── README.md               # Setup and usage guide
└── REPORT.md               # Design write-up and architecture documentation
```
