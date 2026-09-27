"""Devin API v3 client (plus a mock for demos and tests).

Endpoints used (docs.devin.ai/api-reference/v3):
  POST /organizations/{org_id}/sessions                     create session
  GET  /organizations/{org_id}/sessions/{id}                read status, PRs, ACUs, structured output
  POST /organizations/{org_id}/sessions/{id}/messages       unblock a waiting session

Session fields the orchestrator relies on:
  status          new | claimed | running | exit | error | suspended | resuming
  status_detail   working | waiting_for_user | waiting_for_approval | finished |
                  usage_limit_exceeded | out_of_credits | ... | error
  pull_requests   [{pr_url, pr_state}]
  acus_consumed   float
  structured_output  object matching prompt.STRUCTURED_OUTPUT_SCHEMA
"""
import asyncio
import logging
import time

import httpx

from . import config

log = logging.getLogger("devin")


class DevinError(Exception):
    pass


class DevinClient:
    def __init__(self):
        s = config.settings
        self.base = f"{s.devin_base_url}/organizations/{s.devin_org_id}"
        self.headers = {
            "Authorization": f"Bearer {s.devin_api_key}",
            "Content-Type": "application/json",
        }

    async def _request(self, method: str, path: str, json: dict | None = None) -> dict:
        # Retry rate limits and server errors with backoff; fail fast on 4xx.
        delay = 1.0
        for attempt in range(4):
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.request(method, self.base + path, headers=self.headers, json=json)
            except httpx.TransportError as e:
                if attempt == 3:
                    raise DevinError(f"{method} {path}: {e}") from e
            else:
                if resp.status_code < 400:
                    return resp.json()
                if resp.status_code != 429 and resp.status_code < 500:
                    raise DevinError(f"{method} {path}: HTTP {resp.status_code} {resp.text[:300]}")
                if attempt == 3:
                    raise DevinError(f"{method} {path}: HTTP {resp.status_code} after retries")
            await asyncio.sleep(delay)
            delay *= 2
        raise DevinError("unreachable")

    async def create_session(self, *, prompt, title, tags, repos, structured_output_schema) -> dict:
        s = config.settings
        body = {
            "prompt": prompt,
            "title": title,
            "tags": tags,
            "max_acu_limit": s.max_acu_limit,
            "structured_output_schema": structured_output_schema,
            "structured_output_required": s.structured_output_required,
        }
        if repos:
            body["repos"] = repos
        return await self._request("POST", "/sessions", json=body)

    async def get_session(self, session_id: str) -> dict:
        return await self._request("GET", f"/sessions/{session_id}")

    async def send_message(self, session_id: str, message: str) -> dict:
        return await self._request("POST", f"/sessions/{session_id}/messages", json={"message": message})


class MockDevin:
    """Simulated Devin for demos and tests: no network, no spend.

    Each get_session call advances the session one step. Put a marker in the
    issue title to exercise the unhappy paths:
      [needs-input]  asks a question once, recovers after the auto-nudge
      [stuck]        keeps asking; ends up with a human
      [no-pr]        finishes without opening a PR
    """

    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self._n = 0

    async def create_session(self, *, prompt, title, tags, repos, structured_output_schema) -> dict:
        self._n += 1
        sid = f"devin-mock{self._n:04d}"
        issue_tag = next((t for t in tags if t.startswith("issue-")), "issue-0")
        now = int(time.time())
        self.sessions[sid] = {
            "session_id": sid,
            "url": f"https://app.devin.ai/sessions/{sid}",
            "status": "new",
            "status_detail": None,
            "title": title,
            "tags": tags,
            "created_at": now,
            "updated_at": now,
            "acus_consumed": 0.0,
            "pull_requests": [],
            "structured_output": None,
            "_step": 0,
            "_nudged": 0,
            "_repo": (repos or ["you/superset"])[0],
            "_issue": int(issue_tag.split("-")[1]),
        }
        return self._public(sid)

    async def send_message(self, session_id: str, message: str) -> dict:
        self.sessions[session_id]["_nudged"] += 1
        return self._public(session_id)

    async def get_session(self, session_id: str) -> dict:
        s = self.sessions[session_id]
        s["_step"] += 1
        step, title = s["_step"], s["title"]
        s["acus_consumed"] = round(min(step, 4) * 0.9, 2)
        s["updated_at"] = int(time.time())

        if "[stuck]" in title and step >= 2:
            s["status"], s["status_detail"] = "running", "waiting_for_user"
        elif "[needs-input]" in title and step == 2 and s["_nudged"] == 0:
            s["status"], s["status_detail"] = "running", "waiting_for_user"
        elif step < 3:
            s["status"], s["status_detail"] = "running", "working"
        elif "[no-pr]" in title:
            s["status"], s["status_detail"] = "exit", "finished"
            s["structured_output"] = {"blocked_reason": "could not reproduce the vulnerable pin"}
        else:
            pr = f"https://github.com/{s['_repo']}/pull/{9000 + s['_issue']}"
            if not s["pull_requests"]:
                s["pull_requests"] = [{"pr_url": pr, "pr_state": "open"}]
            s["status"], s["status_detail"] = "exit", "finished"
            if "selenium" in title.lower():
                s["structured_output"] = {
                    "pr_url": pr,
                    "previous_version": "3.141.0",
                    "new_version": "4.14.0",
                    "code_changes_required": True,
                    "tests_command": "pytest tests/unit_tests/utils/test_webdriver.py",
                    "tests_passed": True,
                    "summary": "Selenium 4.10+ removed firefox_profile and service_log_path from the driver "
                               "constructor, so WebDriverSelenium.create now builds a Service object and sets "
                               "the profile on Options. Old WEBDRIVER_CONFIGURATION keys are translated with a "
                               "deprecation warning; added unit tests for both config shapes.",
                    "blocked_reason": "",
                }
            else:
                s["structured_output"] = {
                    "pr_url": pr,
                    "previous_version": "3.1.4",
                    "new_version": "3.1.6",
                    "code_changes_required": False,
                    "tests_command": "pytest tests/unit_tests/jinja_context_test.py",
                    "tests_passed": True,
                    "summary": "Patch upgrade to the fixed release; no calling code changes needed. Unit tests pass.",
                    "blocked_reason": "",
                }
        return self._public(session_id)

    def merge(self, session_id: str):
        """Test helper: pretend a reviewer merged the PR."""
        for pr in self.sessions[session_id]["pull_requests"]:
            pr["pr_state"] = "merged"

    def _public(self, sid):
        return {k: v for k, v in self.sessions[sid].items() if not k.startswith("_")}


_client = None


def client():
    global _client
    if _client is None:
        _client = MockDevin() if config.settings.devin_mock else DevinClient()
    return _client


def reset_client():
    global _client
    _client = None
