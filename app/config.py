"""All runtime configuration, read from environment variables.

`settings` is re-assignable so tests can swap in a fresh instance.
"""
import os
from dataclasses import dataclass


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # Devin
    devin_api_key: str
    devin_org_id: str
    devin_base_url: str
    devin_mock: bool               # true = simulated Devin, no API calls, no spend
    max_acu_limit: int             # hard budget per session, enforced by Devin
    acu_price_usd: float           # for cost reporting only
    structured_output_required: bool

    # GitHub
    github_token: str              # used to comment on issues; optional
    github_repo: str               # "owner/superset" — the fork Devin works in
    webhook_secret: str
    trigger_label: str

    # Orchestration
    poll_interval_s: int
    max_nudges: int                # auto-replies before a blocked session goes to a human
    session_timeout_min: int
    max_attempts: int              # per issue, counting retries after re-labelling

    # Reporting
    hours_saved_per_fix: float     # assumption shown on the dashboard, label says so

    db_path: str

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        return cls(
            devin_api_key=e("DEVIN_API_KEY", ""),
            devin_org_id=e("DEVIN_ORG_ID", ""),
            devin_base_url=e("DEVIN_BASE_URL", "https://api.devin.ai/v3"),
            devin_mock=_bool("DEVIN_MOCK", False),
            max_acu_limit=int(e("DEVIN_MAX_ACU_LIMIT", "10")),
            acu_price_usd=float(e("DEVIN_ACU_PRICE_USD", "2.25")),
            structured_output_required=_bool("DEVIN_STRUCTURED_OUTPUT_REQUIRED", True),
            github_token=e("GITHUB_TOKEN", ""),
            github_repo=e("GITHUB_REPO", ""),
            webhook_secret=e("GITHUB_WEBHOOK_SECRET", ""),
            trigger_label=e("TRIGGER_LABEL", "devin:remediate"),
            poll_interval_s=int(e("POLL_INTERVAL_S", "30")),
            max_nudges=int(e("MAX_NUDGES", "1")),
            session_timeout_min=int(e("SESSION_TIMEOUT_MIN", "90")),
            max_attempts=int(e("MAX_ATTEMPTS", "3")),
            hours_saved_per_fix=float(e("HOURS_SAVED_PER_FIX", "2")),
            db_path=e("DB_PATH", "data/remediation.db"),
        )

    def validate(self) -> list[str]:
        problems = []
        if not self.devin_mock:
            if not self.devin_api_key:
                problems.append("DEVIN_API_KEY is not set (or set DEVIN_MOCK=true)")
            if not self.devin_org_id:
                problems.append("DEVIN_ORG_ID is not set (or set DEVIN_MOCK=true)")
        if not self.github_repo:
            problems.append("GITHUB_REPO is not set, e.g. your-user/superset")
        if not self.github_token:
            problems.append("GITHUB_TOKEN is not set: labeled issues will not be picked up "
                            "(only simulated webhooks from scripts/simulate.sh will work)")
        return problems


settings = Settings.from_env()
