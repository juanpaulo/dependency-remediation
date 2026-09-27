"""The GitHub client against GitHub-shaped HTTP responses (no network)."""
import httpx
import pytest

from app import config, github

REPO = "paulo/superset"


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("GITHUB_REPO", REPO)
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    config.settings = config.Settings.from_env()
    calls = []
    routes = {}

    def handler(request: httpx.Request):
        calls.append(request)
        key = (request.method, request.url.path, request.url.params.get("page"))
        status, body = routes.get(key) or routes.get((request.method, request.url.path, None)) or (404, {})
        return httpx.Response(status, json=body)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return routes, calls


def issue(n, pr=False):
    d = {"number": n, "title": f"issue {n}", "body": "", "html_url": f"https://github.com/{REPO}/issues/{n}"}
    if pr:
        d["pull_request"] = {"url": "..."}
    return d


async def test_labeled_issues_filters_prs_and_paginates(api):
    routes, calls = api
    path = f"/repos/{REPO}/issues"
    routes[("GET", path, "1")] = (200, [issue(i) for i in range(1, 100)] + [issue(100, pr=True)])
    routes[("GET", path, "2")] = (200, [issue(101), issue(102, pr=True)])
    got = await github.labeled_issues()
    assert [i["number"] for i in got] == list(range(1, 100)) + [101]
    first = calls[0]
    assert first.url.params["labels"] == "devin:remediate" and first.url.params["state"] == "open"
    assert first.headers["Authorization"] == "Bearer t"


async def test_labeled_issues_raises_on_http_error(api):
    routes, _ = api
    routes[("GET", f"/repos/{REPO}/issues", None)] = (401, {"message": "Bad credentials"})
    with pytest.raises(github.GitHubError, match="401"):
        await github.labeled_issues()


async def test_remove_label_encodes_colon(api):
    routes, calls = api
    routes[("DELETE", f"/repos/{REPO}/issues/7/labels/devin:remediate", None)] = (200, [])
    await github.remove_label(7)
    assert calls[0].method == "DELETE"
    assert calls[0].url.raw_path.decode().endswith("/issues/7/labels/devin%3Aremediate")


@pytest.mark.parametrize("pr,expected", [
    ({"merged": True, "state": "closed"}, "merged"),
    ({"merged": False, "state": "closed"}, "closed"),
    ({"merged": False, "state": "open"}, "open"),
])
async def test_pr_state(api, pr, expected):
    routes, _ = api
    routes[("GET", "/repos/paulo/superset/pulls/12", None)] = (200, pr)
    assert await github.pr_state("https://github.com/paulo/superset/pull/12") == expected


async def test_pr_state_unknown_on_error(api):
    assert await github.pr_state("https://github.com/paulo/superset/pull/99") is None
