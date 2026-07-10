#!/usr/bin/env python3
"""env-manager — a tiny Telegram Mini App for viewing/editing *.env files
under PROJECTS_ROOT.

This is a real public HTTPS endpoint (unlike shell_bot, which only
long-polls outbound), so Telegram WebApp initData validation below is the
only thing between the internet and every project's secrets. Every request
must carry a valid, freshly-signed initData for ALLOWED_USER_ID — see
validate_init_data().
"""

import hashlib
import hmac
import json
import logging
import os
import time
import urllib.request
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

logger = logging.getLogger("env-manager")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
_ALLOWED_USER_ID_RAW = os.environ.get("ALLOWED_USER_ID", "").strip()
PROJECTS_ROOT = Path(os.environ.get("PROJECTS_ROOT", "/projects")).resolve()
INIT_DATA_MAX_AGE_SECONDS = 3600

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN environment variable is required.")
if not _ALLOWED_USER_ID_RAW:
    raise SystemExit("ALLOWED_USER_ID environment variable is required.")
ALLOWED_USER_ID = int(_ALLOWED_USER_ID_RAW)

# When on (default), a request that fails Telegram-signature auth alerts the
# owner over Telegram. This endpoint is public, so alerts are throttled hard
# (per-IP cooldown + hourly cap) and the owner's own expired-session 403s are
# suppressed as benign. SECURITY_ALERTS=0 disables.
SECURITY_ALERTS = os.environ.get("SECURITY_ALERTS", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "",
)
ALERT_IP_COOLDOWN = 1800  # seconds: min gap between alerts for the same source IP
ALERT_HOURLY_CAP = 20     # max alerts sent in any wall-clock hour (flood guard)

# Directories never worth surfacing even though they may contain a *.env match.
_SKIP_DIR_NAMES = {".git", "node_modules", ".venv", "__pycache__"}

app = FastAPI()
# Everything lives under /env — Caddy just plain-proxies to this app, no path
# rewriting on its end, so the served URL is exactly .../env consistently for
# both the page and its API calls.
router = APIRouter(prefix="/env")


def validate_init_data(init_data: str) -> dict:
    """Verify Telegram WebApp initData per Telegram's documented algorithm
    and return the parsed `user` dict. Raises ValueError on any failure."""
    if not init_data:
        raise ValueError("missing initData")

    parsed = dict(parse_qsl(init_data, strict_parsing=True, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise ValueError("initData missing hash")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(computed_hash, received_hash):
        raise ValueError("initData signature mismatch")

    try:
        auth_date = int(parsed.get("auth_date", "0"))
    except ValueError as exc:
        raise ValueError("initData has non-numeric auth_date") from exc
    if time.time() - auth_date > INIT_DATA_MAX_AGE_SECONDS:
        raise ValueError("initData too old")

    user = json.loads(parsed.get("user", "{}"))
    if user.get("id") != ALLOWED_USER_ID:
        raise ValueError(f"user {user.get('id')} is not the allowed user")

    return user


# --- Unauthorized-access alerting (throttled) ------------------------------ #

_alert_by_ip: dict[str, float] = {}
_alert_hour_bucket: int | None = None
_alert_hour_count = 0


def _should_alert(ip: str) -> bool:
    """True at most once per ALERT_IP_COOLDOWN per IP, and never more than
    ALERT_HOURLY_CAP times per hour overall — so scanners can't flood Telegram."""
    global _alert_hour_bucket, _alert_hour_count
    now = time.time()
    hour = int(now // 3600)
    if _alert_hour_bucket != hour:
        _alert_hour_bucket = hour
        _alert_hour_count = 0
    if _alert_hour_count >= ALERT_HOURLY_CAP:
        return False
    # Bound memory: drop IPs whose cooldown has expired.
    for stale in [k for k, t in _alert_by_ip.items() if now - t > ALERT_IP_COOLDOWN]:
        _alert_by_ip.pop(stale, None)
    if now - _alert_by_ip.get(ip, 0.0) < ALERT_IP_COOLDOWN:
        return False
    _alert_by_ip[ip] = now
    _alert_hour_count += 1
    return True


def _send_telegram(text: str) -> None:
    """Best-effort owner DM via Telegram's HTTP API. Never raises."""
    data = urlencode({"chat_id": ALLOWED_USER_ID, "text": text}).encode()
    try:
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=data, timeout=5
        )
    except Exception:  # pragma: no cover - best-effort notification
        logger.warning("failed to send security alert", exc_info=True)


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def require_auth(
    request: Request, x_telegram_init_data: str = Header(default="")
) -> dict:
    try:
        return validate_init_data(x_telegram_init_data)
    except ValueError as exc:
        reason = str(exc)
        # "too old" means the signature was valid (verified before auth_date) but
        # the session expired — i.e. the owner's own stale tab. Benign, no alert.
        if SECURITY_ALERTS and "too old" not in reason:
            ip = _client_ip(request)
            if _should_alert(ip):
                _send_telegram(
                    "⚠️ Unauthorized env-manager request\n"
                    f"from {ip}\npath: {request.url.path}\nreason: {reason}"
                )
        raise HTTPException(status_code=403, detail=reason) from exc


def resolve_env_path(rel_path: str) -> Path:
    """Resolve rel_path under PROJECTS_ROOT, rejecting traversal and
    anything not named like a *.env file."""
    candidate = (PROJECTS_ROOT / rel_path).resolve()
    try:
        candidate.relative_to(PROJECTS_ROOT)
    except ValueError:
        raise HTTPException(status_code=400, detail="path escapes PROJECTS_ROOT")
    if not candidate.name.endswith(".env"):
        raise HTTPException(status_code=400, detail="not a *.env file")
    return candidate


class EnvContent(BaseModel):
    content: str


@router.get("/health")
def health():
    """Unauthenticated liveness probe — no secrets, no file access. Used by the
    container healthcheck (see docker-compose.yml)."""
    return {"ok": True}


@router.get("")
def index():
    return FileResponse(Path(__file__).resolve().parent / "static" / "index.html")


@router.get("/api/envs")
def list_envs(_user: dict = Depends(require_auth)):
    found = []
    for path in PROJECTS_ROOT.rglob("*.env"):
        if _SKIP_DIR_NAMES.intersection(path.relative_to(PROJECTS_ROOT).parts):
            continue
        found.append(str(path.relative_to(PROJECTS_ROOT)))
    return {"envs": sorted(found)}


@router.get("/api/envs/{rel_path:path}")
def read_env(rel_path: str, _user: dict = Depends(require_auth)):
    path = resolve_env_path(rel_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return {"path": rel_path, "content": path.read_text(encoding="utf-8")}


@router.put("/api/envs/{rel_path:path}")
def write_env(rel_path: str, body: EnvContent, _user: dict = Depends(require_auth)):
    path = resolve_env_path(rel_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    # Only ever overwrites an existing file (never creates one), so this
    # preserves whatever permission mode it already had (typically 600).
    path.write_text(body.content, encoding="utf-8")
    return {"path": rel_path, "saved": True}


app.include_router(router)


@app.on_event("startup")
def _log_startup() -> None:
    logger.info(
        "env-manager started: PROJECTS_ROOT=%s, allowed user=%s",
        PROJECTS_ROOT,
        ALLOWED_USER_ID,
    )
