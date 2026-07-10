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
import os
import time
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
_ALLOWED_USER_ID_RAW = os.environ.get("ALLOWED_USER_ID", "").strip()
PROJECTS_ROOT = Path(os.environ.get("PROJECTS_ROOT", "/projects")).resolve()
INIT_DATA_MAX_AGE_SECONDS = 3600

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN environment variable is required.")
if not _ALLOWED_USER_ID_RAW:
    raise SystemExit("ALLOWED_USER_ID environment variable is required.")
ALLOWED_USER_ID = int(_ALLOWED_USER_ID_RAW)

# Directories never worth surfacing even though they may contain a *.env match.
_SKIP_DIR_NAMES = {".git", "node_modules", ".venv", "__pycache__"}

app = FastAPI()


def validate_init_data(init_data: str) -> dict:
    """Verify Telegram WebApp initData per Telegram's documented algorithm
    and return the parsed `user` dict. Raises ValueError on any failure."""
    if not init_data:
        raise ValueError("missing initData")

    parsed = dict(parse_qsl(init_data, strict_parsing=True))
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

    auth_date = int(parsed.get("auth_date", "0"))
    if time.time() - auth_date > INIT_DATA_MAX_AGE_SECONDS:
        raise ValueError("initData too old")

    user = json.loads(parsed.get("user", "{}"))
    if user.get("id") != ALLOWED_USER_ID:
        raise ValueError(f"user {user.get('id')} is not the allowed user")

    return user


def require_auth(x_telegram_init_data: str = Header(default="")) -> dict:
    try:
        return validate_init_data(x_telegram_init_data)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


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


@app.get("/")
def index():
    return FileResponse(Path(__file__).resolve().parent / "static" / "index.html")


@app.get("/api/envs")
def list_envs(_user: dict = Depends(require_auth)):
    found = []
    for path in PROJECTS_ROOT.rglob("*.env"):
        if _SKIP_DIR_NAMES.intersection(path.relative_to(PROJECTS_ROOT).parts):
            continue
        found.append(str(path.relative_to(PROJECTS_ROOT)))
    return {"envs": sorted(found)}


@app.get("/api/envs/{rel_path:path}")
def read_env(rel_path: str, _user: dict = Depends(require_auth)):
    path = resolve_env_path(rel_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return {"path": rel_path, "content": path.read_text(encoding="utf-8")}


@app.put("/api/envs/{rel_path:path}")
def write_env(rel_path: str, body: EnvContent, _user: dict = Depends(require_auth)):
    path = resolve_env_path(rel_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    # Only ever overwrites an existing file (never creates one), so this
    # preserves whatever permission mode it already had (typically 600).
    path.write_text(body.content, encoding="utf-8")
    return {"path": rel_path, "saved": True}
