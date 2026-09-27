# Devin Dependency Remediation

An event-driven pipeline that turns vulnerable dependencies in a fork of
[apache/superset](https://github.com/apache/superset) into reviewed, test-passing
pull requests, with [Devin](https://docs.devin.ai/api-reference/overview) doing the engineering.

```
issues in the fork (filed by hand, or by the pip-audit workflow in fork-setup/)
   │  a human adds the label `devin:remediate`   ← the event, and the approval to spend
   ▼
orchestrator (this repo, Docker), every 30s:
   │  1. intake: finds labeled issues with no live attempt
   │     claim in SQLite (one live attempt per issue)
   │     create Devin session: task + ACU budget + structured output schema
   │  2. track: reads each active Devin session
   │     asking a question → one automatic nudge → still stuck → hand to a human
   │     finished + PR → read back tests_passed / code_changes_required
   │     comment progress on the issue
   ▼
PR merged or closed (read from GitHub) → dashboard
```

All calls are outbound (to GitHub and Devin). No public URL, tunnel or webhook setup is
needed to run it.

## The problem

Scanners are good at *finding* vulnerable dependencies. Fixing them is the
expensive part: someone reads the changelog, bumps the pin, finds what the
upgrade broke, fixes the calling code, runs the tests and writes the PR. That
work is repetitive but not mechanical, so it piles up. Dependabot can open
a version bump PR. It cannot tell you whether the bump broke anything,
and it cannot fix what broke.

The scenario: a team running **Superset 4.1.4**. Auditing its pinned runtime
dependencies finds **60 known vulnerabilities across 26 packages**. Most are
routine patch upgrades. Some are not. Superset 4.1.4 caps Selenium below 4.10
(`selenium>=3.141.0, <4.10.0`) because Selenium 4.10 removed constructor
arguments that `superset/utils/webdriver.py` relies on, and
`WEBDRIVER_CONFIGURATION` is a user-facing setting. The CVE fixes need 4.14.
Upstream, this upgrade was handled as a breaking change
([apache/superset#25933](https://github.com/apache/superset/pull/25933)) and
shipped in 5.0.

This pipeline gives that work to Devin and holds it to the same standard as an
engineer: a PR that references the issue, keeps user configuration working,
and reports the tests it ran.

## Design decisions

| Decision | Why |
|---|---|
| **One issue per package, not per CVE** | `requests 2.25.0` has four CVEs fixed in four different releases (2.31.0 → 2.33.0). Four issues would produce four conflicting PRs, and targeting the first fix would leave three vulnerabilities in place. The scanner targets the highest fix version. |
| **No fix released → no Devin session** | Filed with `no-fix-available`, not `devin:remediate`. Spending ACUs on something that can't be fixed yet is waste. |
| **The label is the approval gate** | The scanner files every finding, but only packages a human approves (or pre-approves with `auto_remediate`) get `devin:remediate`. 26 findings don't turn into 26 sessions by accident; spend is a decision. |
| **User configuration is a public interface** | The prompt requires existing configs to keep working or fail with a clear message, and asks for tests where none exist. That is what made the real Selenium upgrade hard. |
| **Structured output as the verification contract** | Every session must return `tests_passed`, `code_changes_required`, `new_version`, `pr_url` and a summary (JSON schema in `app/prompt.py`). The dashboard reports verified outcomes, not just "a PR exists". |
| **Budget enforced by Devin** | `max_acu_limit` per session. A runaway session costs at most the budget. |
| **The trigger is a label, detected by polling** | Adding `devin:remediate` is the event. The orchestrator checks for it every 30s instead of receiving a webhook, so it needs no inbound network access: it runs behind a corporate firewall, and anyone can run it with `docker compose up`. A 30s pickup is irrelevant next to a session that runs for many minutes. `POST /webhooks/github` is still there for instant pickup where GitHub can reach the service. |
| **Idempotency in the database** | A partial unique index allows one live attempt per issue, so a label seen on every poll (or a redelivered webhook) never starts a second session. |
| **The label doubles as the retry button** | When an attempt ends with a human needed, a failure, or a rejected PR, the orchestrator removes the label and says so on the issue. Adding it back starts the next attempt, capped at `MAX_ATTEMPTS`. |
| **State lives in SQLite, not in memory** | Each tick reads active rows from the database, so a container restart loses nothing. |
| **Unattended by design** | A session that asks a question gets one automatic nudge ("pick the conservative option and note it in the PR"). If it still waits, it goes to a human with a comment on the issue. It never blocks silently. |
| **Merged is the only success** | "PR opened" is progress. The headline number is merged fixes, read from GitHub (or from Devin's reported `pr_state`). |

## Run it

Requires Docker.

```bash
cp .env.example .env        # DEVIN_MOCK=true by default: no API key, no spend
docker compose up --build
open http://localhost:8000
```

### Simulate the whole workflow (no GitHub, no Devin spend)

With `DEVIN_MOCK=true`, the script sends GitHub-shaped "issue labeled" events straight to the
local webhook endpoint, so no accounts are needed:

```bash
./scripts/simulate.sh all
```

This sends five issues plus a duplicate delivery:

| Issue | What the simulated Devin does | Expected result |
|---|---|---|
| #1 selenium 3.141.0 → 4.14.0 | migrates the webdriver code, tests pass | PR open, "needed code changes". Merge it: `./scripts/simulate.sh merge https://github.com/you/superset/pull/9001` |
| #2 jinja2 3.1.4 → 3.1.6 | patch upgrade, tests pass | PR open, version pin only |
| #3 `[needs-input]` | asks a question once | auto-nudged, recovers, PR open |
| #4 `[stuck]` | keeps asking | needs a human, commented on the issue |
| #5 `[no-pr]` | finishes without a PR | needs a human, with Devin's reason |
| #1 again | duplicate webhook | ignored |

`POST /api/reconcile` runs a tick immediately instead of waiting 30s.

### Run it for real

1. **Fork** `apache/superset`, and create a `release-4.1.4` branch at the 4.1.4 release
   (commit `5eb5d607`) without cloning anything:
   ```bash
   gh api repos/<you>/superset/git/refs \
     -f ref=refs/heads/release-4.1.4 -f sha=5eb5d6077e4b0b0fa77f91398ac1d59de395b4a6
   ```
   Then on GitHub: Settings → General → **Default branch** → `release-4.1.4` (Devin's PRs target
   it), and Settings → General → Features → tick **Issues**.
2. **Issues**: create a label `devin:remediate`, and file the issues you want fixed. The ones used
   in this project are in the fork; the format is `**Package:**`, `**Fixed in:**`, and the CVEs.
   Optionally, add `fork-setup/devin-dependency-scan.yml` to the fork's `.github/workflows/` and run
   it from the Actions tab to file every finding automatically (as `triage`, unlabeled).
3. **Devin**: connect GitHub in the Devin web app with access to the fork. Create a service user
   API key (starts with `cog_`) and read your org id from `GET https://api.devin.ai/v3/self`.
4. **GitHub token**: a fine-grained token for the fork only, with **Issues: Read and write** and
   **Pull requests: Read**.
5. **`.env`**:
   ```
   DEVIN_MOCK=false
   DEVIN_API_KEY=cog_...
   DEVIN_ORG_ID=...
   GITHUB_REPO=<you>/superset
   GITHUB_TOKEN=github_pat_...
   DEVIN_MAX_ACU_LIMIT=20
   ```
6. `docker compose up --build`. The log says `polling <you>/superset every 30s for label 'devin:remediate'`.
7. **Add the `devin:remediate` label to an issue.** Within 30 seconds it appears on the dashboard,
   a Devin session starts, and the issue gets a comment with the session link.

`DEVIN_MAX_ACU_LIMIT=20` leaves room for Superset's environment setup and for upgrades that need
code changes.

## Observability

- **Dashboard** at `/`: merged fixes, PRs awaiting review, sessions that need a human, share
  finished without help, share of PRs with passing tests, median time to PR, cost per merged
  fix, ACUs used, and fixes that needed code changes beyond the version bump. Refreshes every 10s.
- **On the issue itself**: comments when Devin starts, when the PR opens (with test result and
  cost), and when a human is needed (with the reason and the session link).
- **JSON**: `/api/metrics`, `/api/remediations`, `/api/events` for piping into existing tooling.

"Engineer-hours saved" uses a stated assumption (`HOURS_SAVED_PER_FIX`, default 2h) and the
dashboard says so. Replace it with the customer's own number.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The tests run against the mock Devin and a fake GitHub. They cover polling intake (each labeled
issue starts exactly once), the happy path through merge read from GitHub, label removal and
retry by re-labelling, bounded attempts, closed PRs, the nudge-and-recover path, a session with no
PR, budget exhaustion, Devin API errors, the GitHub client against GitHub-shaped responses
(pagination, PRs in the issues list, URL-encoded labels), and webhook signature checks.

## Layout

```
app/main.py           polling loop startup, dashboard, JSON API, optional webhook
app/orchestrator.py   intake → claim → launch → reconcile state machine, metrics
app/devin.py          Devin API v3 client (retries 429/5xx) and the mock
app/prompt.py         issue parsing, task prompt, structured output schema
app/db.py             SQLite schema, idempotency index, event log
app/github.py         labeled issues, label removal, PR state, issue comments
fork-setup/           optional scanner workflow for the Superset fork
scripts/simulate.sh   GitHub-shaped events for demos without accounts
```

## Taking this to a customer

- **Deploy it as a service.** Run the container next to their CI with a persistent volume, and
  switch on the webhook for instant pickup if GitHub (or GitHub Enterprise) can reach it.
- **Their scanner, not pip-audit.** Snyk, Dependabot alerts, CodeQL or Wiz all reduce to an
  issue with a package and a target version. The orchestrator doesn't change.
- **Their queue for escalations.** Route `needs_human` to Jira or Slack instead of an issue comment.
- **Repo setup first.** Configure the repo in Devin (dependencies, test command) so sessions
  don't spend ACUs on setup. For a repo the size of Superset, this is the biggest cost lever.
- **Widen once trust is earned.** The pattern is the same for lint debt, deprecated API
  migrations and flaky tests. Start where success can be verified by tests.
- **Report to the budget owner.** Cost per merged fix next to the team's loaded hourly cost
  is the number that keeps the program funded.
