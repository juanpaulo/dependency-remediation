"""The orchestration logic, kept separate from HTTP so it can be tested directly.

Lifecycle of one remediation:

Every POLL_INTERVAL_S the loop runs two steps, both outbound calls only:

  1. intake_once()     the trigger: open issues labeled TRIGGER_LABEL that have no
                       live attempt are claimed and get a Devin session.
                       (POST /webhooks/github does the same instantly, if GitHub can reach you.)
  2. reconcile_once()  every active session is read from Devin and mapped below.

    issue labeled ──> claim() ──> launch() ──> RUNNING
                                     │            ├─ waiting_for_user ─> auto-nudge (MAX_NUDGES times)
                                     │            │                       └─ still waiting ─> NEEDS_HUMAN
                                     │            ├─ finished + PR ─────> PR_OPEN ─> MERGED / CLOSED (read from GitHub)
                                     │            ├─ finished, no PR ───> NEEDS_HUMAN
                                     │            ├─ budget / error ────> FAILED (or PR_OPEN if a PR exists)
                                     │            └─ over SESSION_TIMEOUT_MIN ─> NEEDS_HUMAN
                                     └─ API error ─> FAILED

When an attempt ends in NEEDS_HUMAN, FAILED or CLOSED, the trigger label is removed
from the issue. Adding it back is how a human asks for a retry (up to MAX_ATTEMPTS).
Every transition writes an event and, where a human would care, comments on the issue.
"""
import asyncio
import logging
import statistics
import time

from . import config, db, devin, github, prompt

log = logging.getLogger("orchestrator")

BUDGET_DETAILS = {
    "usage_limit_exceeded", "out_of_credits", "out_of_quota", "no_quota_allocation",
    "payment_declined", "org_usage_limit_exceeded", "user_usage_limit_exceeded",
    "total_session_limit_exceeded", "contract_expired",
}
WAITING_DETAILS = {"waiting_for_user", "waiting_for_approval"}
ENDED_STATUSES = {"exit", "suspended"}


# ---------------------------------------------------------------- trigger

async def intake_once() -> int:
    """The trigger. Start a Devin session for every labeled issue without a live attempt."""
    if not github.enabled():
        return 0
    s = config.settings
    started = 0
    for issue in await github.labeled_issues():
        latest = db.latest_for_issue(s.github_repo, issue["number"])
        if latest and latest["status"] not in db.RETRYABLE:
            continue  # running, awaiting review, or merged
        if latest and latest["attempt"] >= s.max_attempts:
            continue  # bounded, even if the label could not be removed
        rid, existing = claim(issue)
        if existing:
            continue
        await launch(rid, issue)
        started += 1
    return started


def claim(issue: dict) -> tuple[int | None, dict | None]:
    """Record the issue before doing anything slow, so a second trigger is ignored.
    Returns (remediation_id, None) or (None, existing_row) if already live."""
    s = config.settings
    parsed = prompt.parse_issue(issue["title"], issue.get("body") or "")
    try:
        rid = db.claim(
            repo=s.github_repo,
            issue_number=issue["number"],
            issue_title=issue["title"],
            issue_url=issue["html_url"],
            **parsed,
        )
        return rid, None
    except db.AlreadyActive as e:
        return None, e.existing


async def launch(rid: int, issue: dict) -> None:
    """Create the Devin session for a claimed remediation. Runs in the background."""
    s = config.settings
    row = db.get(rid)
    parsed = {k: row[k] for k in ("vuln_id", "package", "fixed_version")}
    tags = ["devin-remediation", f"issue-{issue['number']}"]
    if row["vuln_id"]:
        tags.extend(v.strip() for v in row["vuln_id"].split(",")[:5])
    try:
        session = await devin.client().create_session(
            prompt=prompt.build_prompt(s.github_repo, issue, parsed, s.max_acu_limit),
            title=f"Remediate #{issue['number']}: {issue['title']}"[:200],
            tags=tags,
            repos=[s.github_repo] if s.github_repo else [],
            structured_output_schema=prompt.STRUCTURED_OUTPUT_SCHEMA,
        )
    except Exception as e:
        log.exception("create_session failed for remediation %s", rid)
        db.update(rid, status=db.FAILED, reason=f"could not start Devin session: {e}", finished_at=time.time())
        db.event(rid, "failed", str(e)[:300])
        await github.comment(issue["number"], f"Could not start a Devin session for this issue.\n\n`{str(e)[:300]}`")
        await github.remove_label(issue["number"])
        return

    now = time.time()
    db.update(
        rid,
        status=db.RUNNING,
        devin_session_id=session["session_id"],
        devin_url=session.get("url"),
        devin_status=session.get("status"),
        devin_status_detail=session.get("status_detail"),
        session_started_at=now,
    )
    db.event(rid, "session_started", session.get("url") or session["session_id"])
    await github.comment(
        issue["number"],
        f"Devin is working on this (attempt {row['attempt']}).\n\n"
        f"- Session: {session.get('url')}\n"
        f"- Budget: {s.max_acu_limit} ACUs\n"
        f"- Done means: a PR that says `Fixes #{issue['number']}` with the tests passing.",
    )


# ---------------------------------------------------------------- reconcile

def _pr_from(session: dict) -> tuple[str | None, str | None]:
    prs = session.get("pull_requests") or []
    if prs:
        last = prs[-1]
        return last.get("pr_url"), last.get("pr_state")
    out = session.get("structured_output") or {}
    return out.get("pr_url") or None, None


async def apply_session_state(row: dict, session: dict) -> str:
    """Map one Devin session snapshot onto our state machine. Returns the new status."""
    s = config.settings
    rid, issue_no, now = row["id"], row["issue_number"], time.time()
    status, detail = session.get("status"), session.get("status_detail")
    out = session.get("structured_output") or {}
    pr_url, pr_state = _pr_from(session)

    updates = {
        "devin_status": status,
        "devin_status_detail": detail,
        "acus": float(session.get("acus_consumed") or 0),
    }
    if out:
        if "tests_passed" in out:
            updates["tests_passed"] = int(bool(out["tests_passed"]))
        if "code_changes_required" in out:
            updates["code_changes_required"] = int(bool(out["code_changes_required"]))
        if out.get("summary"):
            updates["summary"] = out["summary"]
    if pr_url and not row["pr_url"]:
        updates["pr_url"] = pr_url
        db.event(rid, "pr_detected", pr_url)
    if pr_state:
        updates["pr_state"] = pr_state

    current = row["status"]
    new = current
    have_pr = bool(pr_url or row["pr_url"])

    # A PR that is already open only needs watching for merge/close.
    if pr_state == "merged":
        new = db.MERGED
    elif pr_state == "closed":
        new = db.CLOSED
    elif current == db.PR_OPEN:
        new = db.PR_OPEN
    elif status == "error" or detail in BUDGET_DETAILS:
        new = db.PR_OPEN if have_pr else db.FAILED
        updates["reason"] = f"Devin stopped: {detail or status}"
    elif detail in WAITING_DETAILS and have_pr:
        new = db.PR_OPEN  # finished its work and is idling for review
    elif detail in WAITING_DETAILS:
        if row["nudges"] < s.max_nudges:
            await devin.client().send_message(row["devin_session_id"], prompt.NUDGE_MESSAGE)
            updates["nudges"] = row["nudges"] + 1
            db.event(rid, "auto_nudged", f"Devin asked for input; nudge {row['nudges'] + 1}/{s.max_nudges}")
            new = db.RUNNING
        else:
            new = db.NEEDS_HUMAN
            updates["reason"] = "Devin is still asking for input after the automatic nudge"
    elif status in ENDED_STATUSES or detail == "finished":
        if have_pr:
            new = db.PR_OPEN
        else:
            new = db.NEEDS_HUMAN
            updates["reason"] = out.get("blocked_reason") or "session ended without opening a PR"
    elif row["session_started_at"] and now - row["session_started_at"] > s.session_timeout_min * 60:
        new = db.NEEDS_HUMAN
        updates["reason"] = f"no result after {s.session_timeout_min} minutes"
    else:
        new = db.RUNNING

    if new != current:
        updates["status"] = new
        if new == db.PR_OPEN and not row["pr_opened_at"]:
            updates["pr_opened_at"] = now
        if new in (db.MERGED, db.CLOSED, db.NEEDS_HUMAN, db.FAILED):
            updates["finished_at"] = now
        db.event(rid, new, updates.get("reason") or updates.get("pr_url") or row.get("pr_url") or "")

    db.update(rid, **updates)

    if new != current:
        await _announce(row, new, {**row, **updates})
    return new


async def _announce(before: dict, new: str, row: dict) -> None:
    s = config.settings
    cost = row["acus"] * s.acu_price_usd
    if new == db.PR_OPEN:
        tests = {1: "passed", 0: "FAILED"}.get(row.get("tests_passed"), "not reported")
        code = {1: "yes", 0: "no, version pin only"}.get(row.get("code_changes_required"), "not reported")
        await github.comment(
            row["issue_number"],
            f"Devin opened a PR: {row['pr_url']}\n\n"
            f"- Tests: **{tests}**\n"
            f"- Calling code changed: {code}\n"
            f"- Cost: {row['acus']:.1f} ACUs (about ${cost:.2f})\n\n"
            f"{row.get('summary') or ''}",
        )
    elif new == db.NEEDS_HUMAN:
        await github.comment(
            row["issue_number"],
            f"Devin needs a human here: {row.get('reason')}.\n\n"
            f"Session: {row.get('devin_url')}\n"
            f"I removed the `{s.trigger_label}` label. Add it again to retry.",
        )
        await github.remove_label(row["issue_number"])
    elif new == db.FAILED:
        await github.comment(
            row["issue_number"],
            f"Remediation failed: {row.get('reason')}.\n\nAdd the `{s.trigger_label}` label again to retry.",
        )
        await github.remove_label(row["issue_number"])
    elif new == db.CLOSED:
        await github.comment(
            row["issue_number"],
            f"The PR was closed without merging. Add the `{s.trigger_label}` label again to retry.",
        )
        await github.remove_label(row["issue_number"])


async def reconcile_once() -> int:
    """Poll every active session once. Restart-safe: state lives in SQLite, not in memory."""
    rows = db.list_active()
    for row in rows:
        try:
            session = await devin.client().get_session(row["devin_session_id"])
            await apply_session_state(row, session)
            fresh = db.get(row["id"])
            if fresh["status"] == db.PR_OPEN and fresh["pr_url"]:
                state = await github.pr_state(fresh["pr_url"])
                if state in ("merged", "closed"):
                    await finish_pr(fresh, merged=state == "merged", pr_url=fresh["pr_url"])
        except Exception:
            log.exception("reconcile failed for remediation %s", row["id"])
    return len(rows)


async def tick() -> None:
    try:
        await intake_once()
    except Exception:
        log.exception("intake failed")
    await reconcile_once()


async def reconcile_forever(stop: asyncio.Event) -> None:
    while not stop.is_set():
        await tick()
        try:
            await asyncio.wait_for(stop.wait(), timeout=config.settings.poll_interval_s)
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------- PR events

async def handle_pr_closed(pr: dict) -> dict:
    """GitHub pull_request.closed. Match on PR URL first, then 'Fixes #N' in the body."""
    import re

    row = db.find_by_pr_url(pr.get("html_url", ""))
    if not row:
        m = re.search(r"(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?)\s+#(\d+)", pr.get("body") or "", re.I)
        if m:
            row = db.latest_for_issue(config.settings.github_repo, int(m.group(1)))
    if not row:
        return {"matched": False}
    new = await finish_pr(row, merged=bool(pr.get("merged")), pr_url=pr.get("html_url", ""))
    return {"matched": True, "remediation_id": row["id"], "status": new}


async def finish_pr(row: dict, merged: bool, pr_url: str) -> str:
    """A human merged or closed Devin's PR. Seen by polling GitHub, or by the webhook."""
    new = db.MERGED if merged else db.CLOSED
    if row["status"] != new:
        updates = {"status": new, "pr_state": "merged" if merged else "closed",
                   "pr_url": row["pr_url"] or pr_url, "finished_at": time.time()}
        db.update(row["id"], **updates)
        db.event(row["id"], new, pr_url)
        await _announce(row, new, {**row, **updates})
    return new


# ---------------------------------------------------------------- metrics

def metrics() -> dict:
    s = config.settings
    rows = db.list_all()
    by = lambda st: [r for r in rows if r["status"] == st]
    merged, pr_open = by(db.MERGED), by(db.PR_OPEN)
    needs_human, failed, closed = by(db.NEEDS_HUMAN), by(db.FAILED), by(db.CLOSED)
    in_flight = by(db.RUNNING) + by(db.QUEUED)

    produced_pr = merged + pr_open + closed
    decided = produced_pr + needs_human + failed
    total_acus = sum(r["acus"] for r in rows)

    def minutes(pairs):
        vals = [(b - a) / 60 for a, b in pairs if a and b]
        return round(statistics.median(vals), 1) if vals else None

    tested = [r for r in produced_pr if r["tests_passed"] is not None]
    return {
        "total": len(rows),
        "in_flight": len(in_flight),
        "pr_open": len(pr_open),
        "merged": len(merged),
        "needs_human": len(needs_human),
        "failed": len(failed),
        "closed": len(closed),
        # Of the attempts that reached an outcome, how many produced a PR on their own.
        "autonomy_rate": round(len(produced_pr) / len(decided), 2) if decided else None,
        "tests_pass_rate": round(sum(r["tests_passed"] for r in tested) / len(tested), 2) if tested else None,
        "code_change_count": sum(1 for r in produced_pr if r["code_changes_required"] == 1),
        "median_min_to_pr": minutes((r["created_at"], r["pr_opened_at"]) for r in produced_pr),
        "median_min_to_merge": minutes((r["created_at"], r["finished_at"]) for r in merged),
        "total_acus": round(total_acus, 1),
        "total_cost_usd": round(total_acus * s.acu_price_usd, 2),
        "cost_per_merged_fix_usd": round(total_acus * s.acu_price_usd / len(merged), 2) if merged else None,
        "est_hours_saved": round(len(merged) * s.hours_saved_per_fix, 1),
        "hours_saved_per_fix_assumption": s.hours_saved_per_fix,
    }
