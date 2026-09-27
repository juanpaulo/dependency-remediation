"""HTTP surface: dashboard and JSON API.

The trigger itself is the polling loop started here (see orchestrator.intake_once).
POST /webhooks/github is an optional instant trigger for deployments GitHub can
reach, and is what scripts/simulate.sh uses locally.
"""
import asyncio
import hashlib
import hmac
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from . import config, db, orchestrator

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    for problem in config.settings.validate():
        log.warning("config: %s", problem)
    db.init()
    stop = asyncio.Event()
    task = None
    if os.environ.get("DISABLE_POLLER") != "1":
        task = asyncio.create_task(orchestrator.reconcile_forever(stop))
    s = config.settings
    mode = "MOCK Devin (no spend)" if s.devin_mock else "live Devin API"
    watching = f"polling {s.github_repo} every {s.poll_interval_s}s for label {s.trigger_label!r}" \
        if s.github_token and s.github_repo else "not polling GitHub (no GITHUB_TOKEN)"
    log.info("ready: %s, %s", mode, watching)
    yield
    stop.set()
    if task:
        await task


app = FastAPI(title="Devin Dependency Remediation", lifespan=lifespan)
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))


def _verify(body: bytes, signature: str | None) -> None:
    secret = config.settings.webhook_secret
    if not secret:
        return  # local simulation; always set a secret for anything reachable from the internet
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="invalid signature")


@app.post("/webhooks/github")
async def github_webhook(request: Request, background: BackgroundTasks):
    body = await request.body()
    _verify(body, request.headers.get("X-Hub-Signature-256"))
    event = request.headers.get("X-GitHub-Event", "")
    payload = await request.json()
    action = payload.get("action")

    if event == "ping":
        return {"ok": True, "pong": True}

    if event == "issues" and action == "labeled":
        label = (payload.get("label") or {}).get("name")
        if label != config.settings.trigger_label:
            return {"ignored": f"label {label!r}"}
        issue = payload["issue"]
        if issue.get("state") == "closed":
            return {"ignored": "issue is closed"}
        rid, existing = orchestrator.claim(issue)
        if existing:
            return {"ignored": "already in progress", "remediation_id": existing["id"], "status": existing["status"]}
        # Ack GitHub immediately (it times out at 10s); create the Devin session in the background.
        background.add_task(orchestrator.launch, rid, issue)
        return {"accepted": True, "remediation_id": rid}

    if event == "pull_request" and action == "closed":
        return await orchestrator.handle_pr_closed(payload.get("pull_request") or {})

    return {"ignored": f"{event}/{action}"}


@app.get("/api/metrics")
async def api_metrics():
    return orchestrator.metrics()


@app.get("/api/remediations")
async def api_remediations():
    return db.list_all()


@app.get("/api/events")
async def api_events(limit: int = 50):
    return db.recent_events(limit)


@app.post("/api/reconcile")
async def api_reconcile():
    """Check GitHub and Devin now instead of waiting for the next tick. Handy during a demo."""
    started = await orchestrator.intake_once()
    return {"started": started, "checked": await orchestrator.reconcile_once()}


@app.get("/healthz")
async def healthz():
    return {"ok": True, "mock": config.settings.devin_mock}


def _ago(ts):
    if not ts:
        return ""
    d = int(time.time() - ts)
    if d < 60:
        return f"{d}s ago"
    if d < 3600:
        return f"{d // 60}m ago"
    if d < 86400:
        return f"{d // 3600}h ago"
    return f"{d // 86400}d ago"


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    s = config.settings
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "m": orchestrator.metrics(),
            "rows": db.list_all(),
            "events": db.recent_events(12),
            "repo": s.github_repo,
            "mock": s.devin_mock,
            "acu_price": s.acu_price_usd,
            "ago": _ago,
        },
    )
