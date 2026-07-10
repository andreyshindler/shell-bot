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

import logging
import logging.handlers
import os
import re
import subprocess
import sys
from pathlib import Path

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

# HTTPS URL of the env-manager Mini App (see env-manager/). Optional: the
# quick-command keyboard just omits that button if unset.
ENV_MINIAPP_URL = os.environ.get("ENV_MINIAPP_URL", "").strip()

COMMAND_TIMEOUT_SECONDS = 60
MAX_OUTPUT_CHARS = 3500  # keep the reply under Telegram's 4096-char message cap

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
    + ("/env — open the .env file manager (Mini App)\n" if ENV_MINIAPP_URL else "")
    + "\n"
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

    if not ENV_MINIAPP_URL:
        await update.message.reply_text(
            "The .env file manager isn't configured on this bot "
            "(ENV_MINIAPP_URL is unset)."
        )
        return

    # An inline button, not a persistent-keyboard one: KeyboardButton.web_app
    # doesn't reliably populate Telegram.WebApp.initData across all clients
    # (confirmed broken on both mobile and desktop), while inline web_app
    # buttons are Telegram's standard, well-tested Mini App launch pattern.
    button = InlineKeyboardButton(
        "🔐 Manage .env files", web_app=WebAppInfo(url=ENV_MINIAPP_URL)
    )
    await update.message.reply_text(
        "Open the .env file manager:", reply_markup=InlineKeyboardMarkup([[button]])
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
# Entrypoint
# --------------------------------------------------------------------------- #

def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN environment variable is required.")

    logger.info(
        "Starting shell_bot (allowed user %s, cwd %s, log %s)",
        ALLOWED_USER_ID,
        WORKING_DIR,
        LOG_PATH,
    )

    application = Application.builder().token(BOT_TOKEN).build()
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
