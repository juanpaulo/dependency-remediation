"""End-to-end tests against the mock Devin. No network, no spend."""
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app import config, db, devin, orchestrator, prompt

REPO = "paulo/superset"
BODY = (
    "**Package:** `requests` (currently 2.25.0)\n**Fixed in:** 2.33.0\n\n"
    "- **CVE-2023-32681** (fixed in 2.31.0)\n- **CVE-2024-35195** (fixed in 2.32.0)"
)


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVIN_MOCK", "true")
    monkeypatch.setenv("GITHUB_REPO", REPO)
    monkeypatch.setenv("GITHUB_TOKEN", "")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("DISABLE_POLLER", "1")
    monkeypatch.setenv("MAX_NUDGES", "1")
    config.settings = config.Settings.from_env()
    devin.reset_client()
    from app import main
    with TestClient(main.app) as c:
        yield c


def labeled(number, title, label="devin:remediate", body=BODY):
    return {
        "action": "labeled",
        "label": {"name": label},
        "issue": {"number": number, "state": "open", "title": title, "body": body,
                  "html_url": f"https://github.com/{REPO}/issues/{number}"},
    }


def post(c, event, payload, headers=None):
    return c.post("/webhooks/github", json=payload,
                  headers={"X-GitHub-Event": event, **(headers or {})})


async def poll(n=1):
    for _ in range(n):
        await orchestrator.reconcile_once()


def test_parse_issue_collects_all_cves():
    p = prompt.parse_issue("[Security] requests 2.25.0", BODY)
    assert p == {"vuln_id": "CVE-2023-32681, CVE-2024-35195", "package": "requests", "fixed_version": "2.33.0"}


def test_prompt_targets_highest_fix_and_demands_tests():
    p = prompt.build_prompt(REPO, labeled(7, "t")["issue"], prompt.parse_issue("t", BODY), 10)
    assert "2.33.0" in p and "Fixes #7" in p and "pytest" in p and "10 ACUs" in p


@pytest.mark.asyncio
async def test_happy_path_to_merge(app_client):
    r = post(app_client, "issues", labeled(1, "[Security] requests"))
    assert r.json()["accepted"] is True
    row = db.list_all()[0]
    assert row["status"] == db.RUNNING and row["devin_session_id"].startswith("devin-mock")

    await poll(3)
    row = db.get(row["id"])
    assert row["status"] == db.PR_OPEN
    assert row["pr_url"].endswith("/pull/9001")
    assert row["tests_passed"] == 1 and row["acus"] > 0

    # Reviewer merges on GitHub -> webhook
    r = post(app_client, "pull_request",
             {"action": "closed", "pull_request": {"merged": True, "html_url": row["pr_url"], "body": ""}})
    assert r.json()["status"] == db.MERGED

    m = app_client.get("/api/metrics").json()
    assert m["merged"] == 1 and m["autonomy_rate"] == 1.0 and m["tests_pass_rate"] == 1.0
    assert m["cost_per_merged_fix_usd"] > 0


@pytest.mark.asyncio
async def test_merge_detected_from_devin_pr_state_without_webhook(app_client):
    post(app_client, "issues", labeled(1, "[Security] requests"))
    await poll(3)
    row = db.list_all()[0]
    devin.client().merge(row["devin_session_id"])
    await poll(1)
    assert db.get(row["id"])["status"] == db.MERGED


def test_duplicate_delivery_is_ignored(app_client):
    assert post(app_client, "issues", labeled(1, "x")).json()["accepted"]
    r = post(app_client, "issues", labeled(1, "x")).json()
    assert r["ignored"] == "already in progress"
    assert len(db.list_all()) == 1


def test_other_labels_and_events_are_ignored(app_client):
    assert "ignored" in post(app_client, "issues", labeled(1, "x", label="bug")).json()
    assert "ignored" in post(app_client, "issues", {"action": "opened", "issue": {}}).json()
    assert post(app_client, "ping", {"zen": "hi"}).json()["pong"]
    assert db.list_all() == []


@pytest.mark.asyncio
async def test_waiting_session_is_nudged_and_recovers(app_client):
    post(app_client, "issues", labeled(2, "[Security] urllib3 [needs-input]"))
    await poll(2)
    row = db.list_all()[0]
    assert row["nudges"] == 1 and row["status"] == db.RUNNING
    await poll(2)
    assert db.get(row["id"])["status"] == db.PR_OPEN


@pytest.mark.asyncio
async def test_stuck_session_goes_to_human_then_retry_allowed(app_client):
    post(app_client, "issues", labeled(3, "[Security] jinja2 [stuck]"))
    await poll(4)
    row = db.list_all()[0]
    assert row["status"] == db.NEEDS_HUMAN and "input" in row["reason"]

    # Re-applying the label starts attempt 2
    r = post(app_client, "issues", labeled(3, "[Security] jinja2 [stuck]")).json()
    assert r["accepted"]
    assert db.list_all()[0]["attempt"] == 2


@pytest.mark.asyncio
async def test_session_without_pr_needs_human(app_client):
    post(app_client, "issues", labeled(4, "[Security] pyyaml [no-pr]"))
    await poll(3)
    row = db.list_all()[0]
    assert row["status"] == db.NEEDS_HUMAN and "reproduce" in row["reason"]


@pytest.mark.asyncio
async def test_budget_exhausted_without_pr_fails(app_client):
    post(app_client, "issues", labeled(5, "x"))
    row = db.list_all()[0]
    new = await orchestrator.apply_session_state(
        row, {"status": "exit", "status_detail": "usage_limit_exceeded", "acus_consumed": 10, "pull_requests": []})
    assert new == db.FAILED


def test_devin_api_error_marks_failed(app_client, monkeypatch):
    async def boom(**kw):
        raise devin.DevinError("HTTP 403 forbidden")
    monkeypatch.setattr(devin.client(), "create_session", boom)
    post(app_client, "issues", labeled(6, "x"))
    row = db.list_all()[0]
    assert row["status"] == db.FAILED and "403" in row["reason"]


def test_signature_required_when_secret_set(app_client, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "s3cret")
    config.settings = config.Settings.from_env()
    payload = labeled(8, "x")
    assert post(app_client, "issues", payload).status_code == 401
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"s3cret", raw, hashlib.sha256).hexdigest()
    r = app_client.post("/webhooks/github", content=raw,
                        headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": sig,
                                 "Content-Type": "application/json"})
    assert r.status_code == 200 and r.json()["accepted"]


@pytest.mark.asyncio
async def test_dashboard_renders_with_every_status(app_client):
    for n, t in [(1, "a"), (2, "b [needs-input]"), (3, "c [stuck]"), (4, "d [no-pr]")]:
        post(app_client, "issues", labeled(n, t))
    await poll(4)
    html = app_client.get("/").text
    assert "Awaiting review" in html and "Needs a human" in html and "#9001" in html
