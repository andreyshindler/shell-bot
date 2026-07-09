#!/usr/bin/env python3
"""shell_bot — a minimal Telegram bot for running shell commands on the VPS.

Lets a single whitelisted Telegram user run arbitrary shell commands on the host
from their phone (e.g. ``git clone`` into ``~/projects`` without opening SSH).

Security model (see project notes before weakening any of this):
  * Only the numeric Telegram user id in ``ALLOWED_USER_ID`` is served. Every
    other sender is silently ignored and logged as REJECTED.
  * ``BLOCKLIST`` hard-refuses a short list of catastrophic commands even for
    the allowed user.
  * ``shell=True`` is intentional — full shell semantics (cd, pipes, git) are
    the whole point. This is why the whitelist + blocklist matter.

Configuration is entirely via environment variables — nothing secret lives in
this file:

    BOT_TOKEN        token from @BotFather
    ALLOWED_USER_ID  the allowed user's numeric Telegram id (from @userinfobot)
"""

import hashlib
import hmac
import json
import logging
import logging.handlers
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

try:
    from aiohttp import web as aiohttp_web
except ImportError:  # aiohttp is only needed when the .env Mini App is enabled
    aiohttp_web = None

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
_ALLOWED_USER_ID_RAW = os.environ.get("ALLOWED_USER_ID", "").strip()

# Directory the quick-command keyboard's git/docker buttons `cd` into before
# running (see QUICK_COMMANDS below) — the repo checkout, which isn't
# necessarily WORKING_DIR's default (home). Optional: buttons run from
# whatever the current working directory is if unset.
REPO_DIR = os.environ.get("REPO_DIR", "").strip()

COMMAND_TIMEOUT_SECONDS = 60
MAX_OUTPUT_CHARS = 3500  # keep the reply under Telegram's 4096-char message cap

# --- .env Mini App (optional) --------------------------------------------- #
# When WEBAPP_URL is set, /env opens a Telegram Mini App to view/edit the .env
# in the current working directory. The bot serves the app + a tiny JSON API
# from an aiohttp server bound to WEBAPP_BIND (loopback by default); nginx
# fronts it over HTTPS at WEBAPP_URL. Left unset → the feature is disabled and
# the bot runs exactly as before.
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").strip()
WEBAPP_BIND = os.environ.get("WEBAPP_BIND", "127.0.0.1:8081").strip()
INIT_DATA_MAX_AGE_SECONDS = 3600   # reject Telegram initData older than this
MAX_ENV_BYTES = 256 * 1024         # cap on .env size read/written via the app
ENV_FILENAME = ".env"              # only ever this file, in the current dir

# Where audit logs land — next to this script, regardless of cwd. Rotated in
# place so the audit trail never grows unbounded: keep LOG_BACKUP_COUNT old
# files of up to LOG_MAX_BYTES each (shell_bot.log, shell_bot.log.1, …). Self
# contained, so it works identically under systemd or pm2 — no external
# logrotate/timer needed.
LOG_PATH = Path(__file__).resolve().parent / "shell_bot.log"
LOG_MAX_BYTES = 2 * 1024 * 1024  # 2 MiB per file
LOG_BACKUP_COUNT = 5             # ~10 MiB of history retained

# Catastrophic commands refused even for the allowed user. These are matched as
# regexes against the raw command text (whitespace-normalised). This is a
# best-effort backstop, NOT a sandbox — the allowed user is trusted, this just
# guards against fat-fingering something irreversible.
BLOCKLIST = [
    r"\brm\s+(-[a-z]*\s+)*-[a-z]*r[a-z]*f[a-z]*\s+(-[a-z]*\s+)*/\s*($|\S)",  # rm -rf /
    r"\brm\s+(-[a-z]*\s+)*-[a-z]*f[a-z]*r[a-z]*\s+(-[a-z]*\s+)*/\s*($|\S)",  # rm -fr /
    r"\bmkfs\b",                       # formatting a filesystem
    r"\bdd\b[^|]*\bif=",               # dd if=... (raw disk writes)
    r">\s*/dev/[sh]d[a-z]",            # redirecting into a raw disk device
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",  # classic fork bomb
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bhalt\b",
    r"\bpoweroff\b",
    r"\bmkswap\b",
    r"\bwipefs\b",
    r"\bfdisk\b",
    r"\bchown\s+-R\s+\S+\s+/\s*($|\S)",  # recursive chown of /
    r"\bchmod\s+-R\s+\S+\s+/\s*($|\S)",  # recursive chmod of /
]
_BLOCKLIST_RE = [re.compile(pattern, re.IGNORECASE) for pattern in BLOCKLIST]

# Per-process working directory. There is no per-user session state — this bot
# only ever serves one user, so a module-level cwd is sufficient and matches the
# "no conversation history" design.
WORKING_DIR = str(Path.home())


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            LOG_PATH,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        ),
        logging.StreamHandler(sys.stdout),
    ],
)
# Quiet down the very chatty http client used by python-telegram-bot.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("shell_bot")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _load_allowed_user_id() -> int:
    """Parse ALLOWED_USER_ID at startup, failing loudly if it is missing/bad."""
    if not _ALLOWED_USER_ID_RAW:
        raise SystemExit("ALLOWED_USER_ID environment variable is required.")
    try:
        return int(_ALLOWED_USER_ID_RAW)
    except ValueError:
        raise SystemExit(
            f"ALLOWED_USER_ID must be a numeric Telegram id, got: "
            f"{_ALLOWED_USER_ID_RAW!r}"
        )


ALLOWED_USER_ID = _load_allowed_user_id() if _ALLOWED_USER_ID_RAW else None


def is_allowed(update: Update) -> bool:
    """True only for the single whitelisted user id."""
    user = update.effective_user
    return user is not None and user.id == ALLOWED_USER_ID


def _describe_sender(update: Update) -> str:
    user = update.effective_user
    if user is None:
        return "unknown sender"
    return f"id={user.id} username={user.username!r}"


def is_blocked(command: str) -> bool:
    """True if the command matches any catastrophic pattern in BLOCKLIST."""
    normalized = " ".join(command.split())
    return any(pattern.search(normalized) for pattern in _BLOCKLIST_RE)


def run_shell(command: str) -> str:
    """Run a command with shell semantics and return combined stdout+stderr."""
    global WORKING_DIR
    try:
        completed = subprocess.run(
            command,
            shell=True,
            cwd=WORKING_DIR,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return f"⏱️ Command timed out after {COMMAND_TIMEOUT_SECONDS}s."
    except Exception as exc:  # pragma: no cover - defensive
        return f"⚠️ Failed to run command: {exc}"

    output = completed.stdout or ""
    if completed.stderr:
        output += ("\n" if output else "") + completed.stderr
    if not output.strip():
        output = f"(no output, exit code {completed.returncode})"
    return output


def format_reply(output: str) -> str:
    """Truncate to Telegram's limits and wrap in a code block."""
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS] + "\n… (truncated)"
    # Guard against breaking out of the code fence with a stray ``` in output.
    safe = output.replace("```", "``​`")
    return f"```\n{safe}\n```"


# --- .env Mini App helpers ------------------------------------------------- #

def validate_init_data(init_data: str):
    """Validate a Telegram WebApp ``initData`` string.

    Returns the parsed ``user`` dict when the signature is valid, recent, and
    from the whitelisted user; otherwise ``None``. This is the only thing
    standing between the network and the .env read/write endpoints, so it is
    deliberately strict and uses a constant-time hash comparison.
    """
    if not init_data or not BOT_TOKEN:
        return None
    try:
        fields = dict(
            urllib.parse.parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
        )
    except ValueError:
        return None

    received_hash = fields.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(computed, received_hash):
        return None

    try:
        auth_date = int(fields.get("auth_date", "0"))
    except ValueError:
        return None
    if auth_date <= 0 or (time.time() - auth_date) > INIT_DATA_MAX_AGE_SECONDS:
        return None

    try:
        user = json.loads(fields.get("user", "{}"))
    except json.JSONDecodeError:
        return None
    if user.get("id") != ALLOWED_USER_ID:
        return None
    return user


def current_env_file():
    """The .env in the *current* working dir, or None if it doesn't exist.

    The path is derived entirely server-side (WORKING_DIR + a fixed filename) —
    the client never supplies a path — so the endpoints can only ever touch a
    .env in the directory the bot is currently pointed at.
    """
    path = Path(WORKING_DIR) / ENV_FILENAME
    return path if path.is_file() else None


def write_env_atomic(path: Path, content: str) -> None:
    """Overwrite ``path`` atomically (temp file in the same dir + os.replace)."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #

def _repo_command(command: str) -> str:
    """Prefix a command with `cd $REPO_DIR &&` if REPO_DIR is configured."""
    return f"cd {REPO_DIR} && {command}" if REPO_DIR else command


QUICK_COMMANDS = ReplyKeyboardMarkup(
    [
        [_repo_command("git pull")],
        # The container has no docker access (by design — see rebuild-watcher.sh).
        # This just drops a marker file; a host-side systemd timer running
        # outside the container does the actual git pull + rebuild.
        [_repo_command("touch .rebuild-requested")],
        # -A so dotfiles (.env, .git, …) show up; plain `ls` hides them.
        ["ls -A"],
    ],
    resize_keyboard=True,
)

HELP_TEXT = (
    "shell_bot — run shell commands on the VPS.\n\n"
    "Send any message and it runs as a shell command in the current working "
    "directory. Output (stdout+stderr) comes back in a code block, truncated "
    f"to ~{MAX_OUTPUT_CHARS} chars; commands time out after "
    f"{COMMAND_TIMEOUT_SECONDS}s.\n\n"
    "Commands:\n"
    "/start — show status and current directory\n"
    "/help — show this help\n"
    "/pwd — print the current working directory\n"
    "/cd <path> — change directory (no arg → home)\n"
    "/env — view/edit the .env in the current directory (Mini App)\n\n"
    "The keyboard below has quick buttons for common commands (git pull, "
    "rebuild, ls).\n\n"
    "Catastrophic commands (rm -rf /, fork bombs, mkfs, dd if=, …) are refused."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        logger.warning("REJECTED /start from %s", _describe_sender(update))
        return
    await update.message.reply_text(
        "shell_bot ready.\n"
        f"cwd: {WORKING_DIR}\n\n"
        f"{HELP_TEXT}",
        reply_markup=QUICK_COMMANDS,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        logger.warning("REJECTED /help from %s", _describe_sender(update))
        return
    await update.message.reply_text(HELP_TEXT, reply_markup=QUICK_COMMANDS)


async def pwd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        logger.warning("REJECTED /pwd from %s", _describe_sender(update))
        return
    await update.message.reply_text(f"cwd: {WORKING_DIR}")


async def cd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global WORKING_DIR
    if not is_allowed(update):
        logger.warning("REJECTED /cd from %s", _describe_sender(update))
        return

    target = " ".join(context.args).strip() if context.args else ""
    if not target:
        target = str(Path.home())

    expanded = Path(os.path.expanduser(os.path.expandvars(target)))
    if not expanded.is_absolute():
        expanded = Path(WORKING_DIR) / expanded

    resolved = expanded.resolve()
    if not resolved.is_dir():
        logger.info("CD FAILED (not a dir): %s", resolved)
        await update.message.reply_text(f"Not a directory: {resolved}")
        return

    WORKING_DIR = str(resolved)
    logger.info("CD -> %s", WORKING_DIR)
    await update.message.reply_text(f"cwd: {WORKING_DIR}")


async def env_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        logger.warning("REJECTED /env from %s", _describe_sender(update))
        return

    if not WEBAPP_URL:
        await update.message.reply_text(
            "The .env editor Mini App isn't configured on this bot "
            "(WEBAPP_URL is unset)."
        )
        return

    if current_env_file() is None:
        await update.message.reply_text(f"No .env in {WORKING_DIR}")
        return

    button = InlineKeyboardButton("✏️ Edit .env", web_app=WebAppInfo(url=WEBAPP_URL))
    await update.message.reply_text(
        f"Edit .env in {WORKING_DIR}",
        reply_markup=InlineKeyboardMarkup([[button]]),
    )


async def handle_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        text = update.message.text if update.message else ""
        logger.warning(
            "REJECTED command from %s: %r", _describe_sender(update), text
        )
        return

    command = (update.message.text or "").strip()
    if not command:
        return

    if is_blocked(command):
        logger.warning("BLOCKED command from allowed user: %r", command)
        await update.message.reply_text(
            "🚫 Refused: this command matches the catastrophic-command blocklist."
        )
        return

    logger.info("RUN (cwd=%s): %r", WORKING_DIR, command)
    output = run_shell(command)
    logger.info("DONE: %r -> %d chars", command, len(output))
    await update.message.reply_text(
        format_reply(output), parse_mode=ParseMode.MARKDOWN
    )


# --------------------------------------------------------------------------- #
# .env Mini App — HTTP server (aiohttp)
# --------------------------------------------------------------------------- #

# Single self-contained page served at the web app root. Uses relative URLs so
# it works under whatever host nginx serves it from, and Telegram theme colors
# so it blends into the client. Not an f-string — the JS braces are literal.
WEBAPP_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Edit .env</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; margin: 0;
         padding: 12px; background: var(--tg-theme-bg-color, #fff);
         color: var(--tg-theme-text-color, #000); }
  #dir { font-size: 12px; opacity: .7; margin-bottom: 8px; word-break: break-all; }
  textarea { width: 100%; box-sizing: border-box; height: 70vh; resize: vertical;
             font-family: ui-monospace, Menlo, monospace; font-size: 14px;
             border: 1px solid var(--tg-theme-hint-color, #ccc); border-radius: 8px;
             padding: 8px; background: var(--tg-theme-secondary-bg-color, #f4f4f5);
             color: inherit; }
  #status { margin-top: 8px; font-size: 14px; min-height: 1.2em; }
  .err { color: #d70000; } .ok { color: #0a870a; }
</style>
</head>
<body>
<div id="dir"></div>
<textarea id="content" spellcheck="false" autocapitalize="off" autocorrect="off"></textarea>
<div id="status"></div>
<script>
  var tg = window.Telegram.WebApp;
  tg.ready(); tg.expand();
  var headers = { 'Authorization': 'tma ' + tg.initData };
  var ta = document.getElementById('content');
  var statusEl = document.getElementById('status');
  var dirEl = document.getElementById('dir');
  function setStatus(msg, isErr) { statusEl.textContent = msg; statusEl.className = isErr ? 'err' : 'ok'; }
  async function load() {
    try {
      var r = await fetch('api/env', { headers: headers });
      var j = await r.json();
      if (!r.ok) { setStatus(j.error || ('HTTP ' + r.status), true); ta.disabled = true; tg.MainButton.hide(); return; }
      dirEl.textContent = j.dir;
      ta.value = j.content;
    } catch (e) { setStatus('Load failed: ' + e, true); }
  }
  async function save() {
    tg.MainButton.showProgress();
    try {
      var r = await fetch('api/env', {
        method: 'POST',
        headers: Object.assign({ 'Content-Type': 'application/json' }, headers),
        body: JSON.stringify({ content: ta.value })
      });
      var j = await r.json();
      if (!r.ok) { setStatus(j.error || ('HTTP ' + r.status), true); tg.MainButton.hideProgress(); return; }
      setStatus('Saved ✓', false);
      tg.MainButton.hideProgress();
      tg.close();
    } catch (e) { setStatus('Save failed: ' + e, true); tg.MainButton.hideProgress(); }
  }
  tg.MainButton.setText('Save .env');
  tg.MainButton.onClick(save);
  tg.MainButton.show();
  load();
</script>
</body>
</html>
"""


def _extract_init_data(request) -> str:
    auth = request.headers.get("Authorization", "")
    return auth[4:] if auth.startswith("tma ") else ""


async def serve_index(request):
    return aiohttp_web.Response(text=WEBAPP_HTML, content_type="text/html")


async def api_get_env(request):
    user = validate_init_data(_extract_init_data(request))
    if user is None:
        return aiohttp_web.json_response({"error": "unauthorized"}, status=401)

    path = current_env_file()
    if path is None:
        return aiohttp_web.json_response(
            {"error": f"no {ENV_FILENAME} in {WORKING_DIR}", "dir": WORKING_DIR},
            status=404,
        )
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        return aiohttp_web.json_response({"error": f"read failed: {exc}"}, status=500)

    logger.info("ENV VIEW (cwd=%s) by user %s", WORKING_DIR, user.get("id"))
    return aiohttp_web.json_response({"dir": WORKING_DIR, "content": content})


async def api_post_env(request):
    user = validate_init_data(_extract_init_data(request))
    if user is None:
        return aiohttp_web.json_response({"error": "unauthorized"}, status=401)

    if request.content_length and request.content_length > MAX_ENV_BYTES:
        return aiohttp_web.json_response({"error": "content too large"}, status=413)
    try:
        body = await request.json()
    except Exception:
        return aiohttp_web.json_response({"error": "invalid JSON body"}, status=400)

    content = body.get("content")
    if not isinstance(content, str):
        return aiohttp_web.json_response({"error": "missing 'content'"}, status=400)
    if len(content.encode("utf-8")) > MAX_ENV_BYTES:
        return aiohttp_web.json_response({"error": "content too large"}, status=413)

    # Re-check existence at write time: refuse to create a missing .env.
    path = current_env_file()
    if path is None:
        return aiohttp_web.json_response(
            {
                "error": f"no {ENV_FILENAME} in {WORKING_DIR} (refusing to create)",
                "dir": WORKING_DIR,
            },
            status=404,
        )
    try:
        write_env_atomic(path, content)
    except OSError as exc:
        return aiohttp_web.json_response({"error": f"write failed: {exc}"}, status=500)

    n_bytes = len(content.encode("utf-8"))
    logger.info(
        "ENV WRITE (cwd=%s, %d bytes) by user %s", WORKING_DIR, n_bytes, user.get("id")
    )
    bot = request.app.get("bot")
    if bot is not None:
        try:
            await bot.send_message(
                ALLOWED_USER_ID, f"✅ Saved {ENV_FILENAME} in {WORKING_DIR}"
            )
        except Exception:  # pragma: no cover - best-effort notification
            logger.warning("Failed to send .env save confirmation", exc_info=True)
    return aiohttp_web.json_response({"ok": True, "dir": WORKING_DIR})


async def _start_webapp(application) -> None:
    """post_init hook: start the Mini App server inside PTB's event loop."""
    if not WEBAPP_URL:
        return
    host, _, port = WEBAPP_BIND.partition(":")
    app = aiohttp_web.Application()
    app["bot"] = application.bot
    app.router.add_get("/", serve_index)
    app.router.add_get("/api/env", api_get_env)
    app.router.add_post("/api/env", api_post_env)

    runner = aiohttp_web.AppRunner(app)
    await runner.setup()
    site = aiohttp_web.TCPSite(runner, host or "127.0.0.1", int(port or "8081"))
    await site.start()
    application.bot_data["_webapp_runner"] = runner
    logger.info(
        "Mini App server listening on %s (public URL %s)", WEBAPP_BIND, WEBAPP_URL
    )


async def _stop_webapp(application) -> None:
    """post_shutdown hook: tear the Mini App server down cleanly."""
    runner = application.bot_data.pop("_webapp_runner", None)
    if runner is not None:
        await runner.cleanup()


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #

def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN environment variable is required.")

    if WEBAPP_URL and aiohttp_web is None:
        raise SystemExit(
            "WEBAPP_URL is set but aiohttp is not installed — "
            "run: pip install -r requirements.txt"
        )

    logger.info(
        "Starting shell_bot (allowed user %s, cwd %s, log %s)",
        ALLOWED_USER_ID,
        WORKING_DIR,
        LOG_PATH,
    )

    builder = Application.builder().token(BOT_TOKEN)
    if WEBAPP_URL:
        builder = builder.post_init(_start_webapp).post_shutdown(_stop_webapp)
    application = builder.build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("pwd", pwd))
    application.add_handler(CommandHandler("cd", cd))
    application.add_handler(CommandHandler("env", env_command))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_command)
    )

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
