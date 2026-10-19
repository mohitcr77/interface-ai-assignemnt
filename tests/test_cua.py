"""Unit tests for core CUA modules (schema, policy, redactor, canon, store)."""
import pytest
from cua.schema import Capability, Risk, ParamType, InputParam, OutputSpec, Step, Checkpoint, Target, Locator
from cua.policy import Policy
from cua.redact import Redactor
from cua.canon import canonicalize_url, url_matches, substitute
from cua import store


def test_redactor_secrets_and_pii():
    r = Redactor(["super_secret_pw", "api_token_xyz"])
    text = (
        "Login with password super_secret_pw and token api_token_xyz. "
        "User SSN is 123-45-6789, email is test.user@example.com, "
        "phone is 555-123-4567, acct is 12345678."
    )
    scrubbed = r.scrub(text)
    assert "super_secret_pw" not in scrubbed
    assert "api_token_xyz" not in scrubbed
    assert "[SSN]" in scrubbed
    assert "[EMAIL]" in scrubbed
    assert "[PHONE]" in scrubbed
    assert "****5678" in scrubbed


def test_policy_allowlist():
    p = Policy(
        allowed_hosts=["localhost:8080", "bank.internal"],
        allowed_actions={"navigate", "click", "type", "extract"},
        irreversible_patterns=["transfer", "delete"],
        safe_submit_patterns=["^log ?in$"]
    )
    assert p.url_allowed("http://localhost:8080/index.htm")
    assert not p.url_allowed("http://malicious.com/phish")
    assert not p.url_allowed("http://localhost:9000/api")

    # Safe vs irreversible action checks
    d_nav = p.check("navigate", Risk.REVERSIBLE, target_url="http://localhost:8080/home")
    assert d_nav.allow

    d_bad_host = p.check("navigate", Risk.REVERSIBLE, target_url="http://evil.com/leak")
    assert not d_bad_host.allow

    # Irreversible blocked unless explicitly enabled
    d_irrev = p.check("click", Risk.IRREVERSIBLE, url="http://localhost:8080/pay")
    assert not d_irrev.allow
    assert "irreversible action blocked" in d_irrev.reason

    p.allow_irreversible = True
    d_irrev_ok = p.check("click", Risk.IRREVERSIBLE, url="http://localhost:8080/pay")
    assert d_irrev_ok.allow


def test_url_canonicalization():
    assert canonicalize_url("http://localhost:8080/parabank/activity.htm?id=12345") == "/parabank/activity.htm?id=:id"
    assert canonicalize_url("http://bank.internal/app/users/987654/view") == "/app/users/:id/view"
    assert url_matches("/parabank/activity.htm?id=:id", "http://anyhost:8080/parabank/activity.htm?id=99999")


def test_substitute():
    res = substitute("Look up member {{account_id}} for {{username}}", {"account_id": "12345", "username": "alice"})
    assert res == "Look up member 12345 for alice"


def test_artifact_load_and_catalog():
    caps = store.list_artifacts()
    assert len(caps) >= 1
    cap = caps[0]
    assert cap.name == "get_account_details"
    cat = store.catalog(approved_only=False)
    assert any(c["name"] == "get_account_details" for c in cat)
