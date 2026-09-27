REPO="juanpaulo/superset"

gh label create "devin:remediate" --repo "$REPO" --color 5319e7 \
  --description "Devin will open a fix PR" 2>/dev/null || echo "label devin:remediate already exists"
gh label create "security" --repo "$REPO" --color d73a4a 2>/dev/null || true

mkissue() {  # $1 title; body on stdin
  if gh issue list --repo "$REPO" --state all --label security --limit 500 --json title --jq '.[].title' | grep -qxF "$1"; then
    cat >/dev/null
    echo "exists:  $1"
  else
    gh issue create --repo "$REPO" --title "$1" --body-file - --label security >/dev/null && echo "created: $1"
  fi
}

mkissue "[Security] jinja2 3.1.4: 3 known vulnerabilities, upgrade to 3.1.6" <<'EOF'
**Package:** `jinja2` (currently 3.1.4)
**Fixed in:** 3.1.6

**Vulnerabilities:**
- **CVE-2024-56201** (fixed in 3.1.5)
- **CVE-2024-56326** (fixed in 3.1.5): sandbox bypass
- **CVE-2025-27516** (fixed in 3.1.6): sandbox breakout through the attr filter

Superset renders user-written SQL templates in Jinja's SandboxedEnvironment
(`superset/jinja_context.py`), so sandbox escapes are directly relevant.

**Acceptance criteria:**
- `jinja2` is at 3.1.6 or later everywhere it is pinned
- Relevant unit tests run and pass
- PR references this issue
EOF

mkissue "[Security] requests 2.32.2: 2 known vulnerabilities, upgrade to 2.33.0" <<'EOF'
**Package:** `requests` (currently 2.32.2)
**Fixed in:** 2.33.0

**Vulnerabilities:**
- **CVE-2024-47081** (fixed in 2.32.4): URL parsing issue may leak .netrc credentials to third parties.
- **CVE-2026-25645** (fixed in 2.33.0): extract_zipped_paths() uses a predictable temporary filename.

**Acceptance criteria:**
- `requests` is at 2.33.0 or later everywhere it is pinned
- Relevant unit tests run and pass
- PR references this issue
EOF

mkissue "[Security] mako 1.3.5: 1 known vulnerability, upgrade to 1.3.12" <<'EOF'
**Package:** `mako` (currently 1.3.5)
**Fixed in:** 1.3.12

**Vulnerabilities:**
- **CVE-2026-44307** (fixed in 1.3.12): backslash path traversal bypasses the directory traversal check in Template (Windows).

**Acceptance criteria:**
- `mako` is at 1.3.12 or later everywhere it is pinned
- Relevant unit tests run and pass
- PR references this issue
EOF

mkissue "[Security] pyjwt 2.8.0: 5 known vulnerabilities, upgrade to 2.13.0" <<'EOF'
**Package:** `pyjwt` (currently 2.8.0)
**Fixed in:** 2.13.0

**Vulnerabilities:**
- **CVE-2026-32597** (fixed in 2.12.0): the crit (Critical) header parameter is not validated.
- **CVE-2026-48522** (fixed in 2.13.0): PyJWKClient passes its uri directly to urlopen().
- **CVE-2026-48524** (fixed in 2.13.0): issue surfacing when a JWKS fetch fails.
- **CVE-2026-48525** (fixed in 2.13.0): resource usage issue depending on request body limits.
- **CVE-2026-48526** (fixed in 2.13.0): issue when a verifier mixes symmetric and asymmetric algorithms.

Out of scope: CVE-2025-45768 (disputed by the maintainers, no fixed release).

**Acceptance criteria:**
- `pyjwt` is at 2.13.0 or later everywhere it is pinned
- Check Superset's JWT usage (superset/utils/oauth2.py, superset/async_events/async_query_manager.py) for behavior changes between 2.8 and 2.13
- Relevant unit tests run and pass
- PR references this issue
EOF

mkissue "[Security] selenium 3.141.0: 2 known vulnerabilities, upgrade to 4.14.0" <<'EOF'
**Package:** `selenium` (currently 3.141.0)
**Fixed in:** 4.14.0

**Vulnerabilities:**
- **CVE-2023-5590** (fixed in 4.14.0): NULL pointer dereference in Selenium prior to 4.14.0.
- **CVE-2022-28108** (fixed in 4.0.0): Selenium Server (Grid) before 4 allows CSRF via non-JSON content types.

**Acceptance criteria:**
- `selenium` is at 4.14.0 or later everywhere it is pinned or constrained (note pyproject.toml caps it below 4.10.0)
- Calling code updated for any breaking or deprecated behavior
- Existing WEBDRIVER_CONFIGURATION settings keep working
- Relevant unit tests run and pass
- PR references this issue
EOF
