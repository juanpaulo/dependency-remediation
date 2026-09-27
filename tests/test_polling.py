"""The polling trigger, against a fake GitHub and the mock Devin. No network, no spend."""
import pytest
from fastapi.testclient import TestClient

from app import config, db, devin, github, orchestrator

REPO = "paulo/superset"
BODY = "**Package:** `jinja2` (currently 3.1.4)\n**Fixed in:** 3.1.6\n\n- **CVE-2025-27516** (fixed in 3.1.6)"


class FakeGitHub:
    def __init__(self):
        self.issues: dict[int, dict] = {}
        self.labeled: set[int] = set()
        self.comments: list[tuple[int, str]] = []
        self.removed: list[int] = []
        self.pr_states: dict[str, str] = {}
        self.removal_works = True

    def add(self, number, title, labeled=True):
        self.issues[number] = {"number": number, "title": title, "body": BODY, "state": "open",
                               "html_url": f"https://github.com/{REPO}/issues/{number}"}
        if labeled:
            self.labeled.add(number)

    async def labeled_issues(self):
        return [self.issues[n] for n in sorted(self.labeled)]

    async def remove_label(self, number):
        self.removed.append(number)
        if self.removal_works:
            self.labeled.discard(number)

    async def pr_state(self, url):
        return self.pr_states.get(url, "open")

    async def comment(self, number, body):
        self.comments.append((number, body))


@pytest.fixture
def gh(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVIN_MOCK", "true")
    monkeypatch.setenv("GITHUB_REPO", REPO)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("DISABLE_POLLER", "1")
    monkeypatch.setenv("MAX_ATTEMPTS", "3")
    config.settings = config.Settings.from_env()
    devin.reset_client()
    fake = FakeGitHub()
    for name in ("labeled_issues", "remove_label", "pr_state", "comment"):
        monkeypatch.setattr(github, name, getattr(fake, name))
    from app import main
    with TestClient(main.app):
        yield fake


async def ticks(n):
    for _ in range(n):
        await orchestrator.tick()


async def test_labeled_issue_is_picked_up_once(gh):
    gh.add(1, "[Security] jinja2 3.1.4")
    gh.add(2, "[Security] requests 2.32.2", labeled=False)
    await ticks(1)
    rows = db.list_all()
    assert len(rows) == 1 and rows[0]["issue_number"] == 1 and rows[0]["status"] == db.RUNNING
    assert any("Devin is working on this" in body for _, body in gh.comments)

    await ticks(1)  # still labeled, already running: no second session
    assert len(db.list_all()) == 1 and len(devin.client().sessions) == 1


async def test_full_path_to_merge_read_from_github(gh):
    gh.add(1, "[Security] jinja2 3.1.4")
    await ticks(4)
    row = db.list_all()[0]
    assert row["status"] == db.PR_OPEN and row["pr_url"]

    gh.pr_states[row["pr_url"]] = "merged"
    await ticks(1)
    assert db.get(row["id"])["status"] == db.MERGED
    assert orchestrator.metrics()["merged"] == 1


async def test_needs_human_removes_label_and_relabel_retries(gh):
    gh.add(3, "[Security] example [stuck]")
    await ticks(5)
    row = db.list_all()[0]
    assert row["status"] == db.NEEDS_HUMAN and 3 in gh.removed
    assert any("Add it again to retry" in body for _, body in gh.comments)

    await ticks(1)  # label is gone: nothing new starts
    assert len(db.list_all()) == 1

    gh.labeled.add(3)  # a human re-applies the label
    await ticks(1)
    latest = db.list_all()[0]
    assert latest["attempt"] == 2 and latest["status"] == db.RUNNING


async def test_closed_pr_removes_label_without_auto_retry(gh):
    gh.add(1, "[Security] jinja2 3.1.4")
    await ticks(4)
    row = db.list_all()[0]
    gh.pr_states[row["pr_url"]] = "closed"
    await ticks(2)
    assert db.get(row["id"])["status"] == db.CLOSED
    assert 1 in gh.removed and len(db.list_all()) == 1


async def test_attempts_are_bounded_if_label_cannot_be_removed(gh):
    gh.removal_works = False  # e.g. token lacks permission
    gh.add(4, "[Security] example [no-pr]")
    await ticks(20)
    assert len(db.list_all()) == 3  # MAX_ATTEMPTS, not an endless loop


async def test_no_token_means_no_polling(gh, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "")
    config.settings = config.Settings.from_env()
    gh.add(1, "[Security] jinja2 3.1.4")
    await ticks(2)
    assert db.list_all() == []
