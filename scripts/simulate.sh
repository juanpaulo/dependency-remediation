#!/usr/bin/env bash
# Sends GitHub-shaped webhooks to the orchestrator so you can watch the whole
# pipeline without a public URL. Signs requests if GITHUB_WEBHOOK_SECRET is set.
#
#   ./scripts/simulate.sh                    # one realistic issue
#   ./scripts/simulate.sh all                # happy path + every failure path (use with DEVIN_MOCK=true)
#   ./scripts/simulate.sh merge <pr_url>     # pretend a reviewer merged a PR
set -euo pipefail

URL="${ORCHESTRATOR_URL:-http://localhost:8000}"
REPO="${GITHUB_REPO:-you/superset}"
SECRET="${GITHUB_WEBHOOK_SECRET:-}"

send() {  # $1 = event, $2 = json
  local sig=()
  if [[ -n "$SECRET" ]]; then
    sig=(-H "X-Hub-Signature-256: sha256=$(printf '%s' "$2" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $NF}')")
  fi
  curl -sS -X POST "$URL/webhooks/github" -H "Content-Type: application/json" \
       -H "X-GitHub-Event: $1" "${sig[@]}" -d "$2"
  echo
}

SELENIUM='**Package:** `selenium` (currently 3.141.0)\n**Fixed in:** 4.14.0\n\n**Vulnerabilities:**\n- **CVE-2023-5590** (fixed in 4.14.0)\n- **CVE-2022-28108** (fixed in 4.0.0)'
JINJA='**Package:** `jinja2` (currently 3.1.4)\n**Fixed in:** 3.1.6\n\n**Vulnerabilities:**\n- **CVE-2024-56201** (fixed in 3.1.5)\n- **CVE-2024-56326** (fixed in 3.1.5)\n- **CVE-2025-27516** (fixed in 3.1.6)'
GENERIC='**Package:** `example` (currently 1.0.0)\n**Fixed in:** 2.0.0\n\n- **CVE-2099-0001** (fixed in 2.0.0)'

issue() {  # $1 = number, $2 = title, $3 = body
  send issues "{\"action\":\"labeled\",\"label\":{\"name\":\"devin:remediate\"},
    \"issue\":{\"number\":$1,\"state\":\"open\",\"title\":\"$2\",\"body\":\"$3\",
    \"html_url\":\"https://github.com/$REPO/issues/$1\"}}"
}

case "${1:-one}" in
  one)
    issue 1 "[Security] selenium 3.141.0: 2 known vulnerabilities, upgrade to 4.14.0" "$SELENIUM" ;;
  all)
    issue 1 "[Security] selenium 3.141.0: 2 known vulnerabilities, upgrade to 4.14.0" "$SELENIUM"
    issue 2 "[Security] jinja2 3.1.4: 3 known vulnerabilities, upgrade to 3.1.6" "$JINJA"
    issue 3 "[Security] example-a [needs-input]" "$GENERIC"
    issue 4 "[Security] example-b [stuck]" "$GENERIC"
    issue 5 "[Security] example-c [no-pr]" "$GENERIC"
    echo "duplicate delivery of #1 (should be ignored):"
    issue 1 "[Security] selenium 3.141.0: 2 known vulnerabilities, upgrade to 4.14.0" "$SELENIUM" ;;
  merge)
    send pull_request "{\"action\":\"closed\",\"pull_request\":{\"merged\":true,\"html_url\":\"$2\",\"body\":\"\"}}" ;;
  *)
    echo "usage: $0 [one|all|merge <pr_url>]"; exit 1 ;;
esac

echo "Dashboard: $URL/   (POST $URL/api/reconcile to poll Devin immediately)"
