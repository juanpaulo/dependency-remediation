"""Turns a GitHub issue into a Devin task, and defines what Devin must report back.

The structured output schema is the verification contract: instead of trusting
"a PR exists", the orchestrator reads back whether tests actually ran and
passed, and whether the upgrade needed code changes beyond the version pin.
"""
import re

VULN_RE = re.compile(r"\b(CVE-\d{4}-\d{4,}|GHSA-[\w]{4}-[\w]{4}-[\w]{4}|PYSEC-\d{4}-\d+)\b")
PACKAGE_RE = re.compile(r"\*\*Package:\*\*\s*`?([A-Za-z0-9_.\-]+)`?")
FIXED_RE = re.compile(r"\*\*Fixed in:\*\*\s*`?([0-9][\w.\-]*)`?")


def parse_issue(title: str, body: str) -> dict:
    text = f"{title}\n{body or ''}"
    ids = list(dict.fromkeys(VULN_RE.findall(text)))  # unique, in order
    cves = [i for i in ids if i.startswith("CVE-")] or ids
    pkg = PACKAGE_RE.search(body or "")
    fixed = FIXED_RE.search(body or "")
    return {
        "vuln_id": ", ".join(cves) if cves else None,
        "package": pkg.group(1) if pkg else None,
        "fixed_version": fixed.group(1) if fixed else None,
    }


STRUCTURED_OUTPUT_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "properties": {
        "pr_url": {"type": "string", "description": "URL of the pull request you opened"},
        "previous_version": {"type": "string"},
        "new_version": {"type": "string"},
        "code_changes_required": {
            "type": "boolean",
            "description": "true if anything beyond the version pin had to change",
        },
        "tests_command": {"type": "string", "description": "exact command you ran"},
        "tests_passed": {"type": "boolean"},
        "summary": {"type": "string", "description": "two sentences for a reviewer"},
        "blocked_reason": {
            "type": "string",
            "description": "empty if not blocked; otherwise what stopped you",
        },
    },
    "required": ["pr_url", "new_version", "code_changes_required", "tests_passed", "summary"],
}


def build_prompt(repo: str, issue: dict, parsed: dict, max_acu: int) -> str:
    pkg = parsed.get("package") or "the affected package"
    fixed = parsed.get("fixed_version") or "the first non-vulnerable release"
    return f"""Remediate a dependency vulnerability in the GitHub repository {repo}.

Issue #{issue['number']}: {issue['title']}
{issue['html_url']}

--- issue body ---
{issue.get('body') or '(empty)'}
--- end issue body ---

What done looks like:
1. `{pkg}` is upgraded to {fixed} or later wherever it is pinned or constrained:
   pyproject.toml (including any upper bound that blocks the fix) and the
   generated requirements/*.txt files. Keep them consistent with each other.
2. Search the codebase for usages of `{pkg}` that the upgrade could affect, and
   read the upstream changelog between the old and new versions. If anything
   breaks or is deprecated, fix the calling code. Do not stop at the version bump.
3. Treat user-facing configuration (for example settings in superset/config.py)
   as a public interface. Existing user configs must keep working, or fail with a
   clear error that says what to change. Explain the compatibility approach in the PR.
4. Run the unit tests that cover the affected code (for example
   `pytest tests/unit_tests -k <area>`), and make sure they pass. If no tests cover
   the code you changed, add focused unit tests. If the full suite is too slow,
   run the relevant subset and say which one.
5. Open a pull request against the default branch. The title starts with
   "fix(deps):". The description includes "Fixes #{issue['number']}", what changed,
   whether calling code had to change, and the exact test command and result.

Rules:
- Do not ask for clarification. If something is ambiguous, pick the most
  conservative option and explain the choice in the PR description.
- Stay within a budget of {max_acu} ACUs. If you cannot finish, stop and explain
  why in blocked_reason.
- When done, fill in the structured output.
"""


NUDGE_MESSAGE = (
    "This session runs unattended and nobody is watching it right now. Do not wait "
    "for input. Make the most conservative reasonable choice, note it in the PR "
    "description, and continue until the PR is open and the structured output is filled in. "
    "If you truly cannot continue, set blocked_reason and finish."
)
