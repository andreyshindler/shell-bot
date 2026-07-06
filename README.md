# shell-bot

A minimal single-file Telegram bot that lets one whitelisted user run shell
commands on a VPS from their phone — e.g. `git clone` into `~/projects` without
opening an SSH session.

## Security model

- **Hard user whitelist.** Only the numeric Telegram id in `ALLOWED_USER_ID` is
  served. Any other sender is silently ignored (no reply) and logged as
  `REJECTED`.
- **Command blocklist.** A short list of catastrophic commands (`rm -rf /`, fork
  bombs, `mkfs`, `dd if=`, disk writes, `shutdown`/`reboot`, …) is refused even
  for the allowed user.
- **Run as a non-root user** with only the permissions it actually needs.
- Secrets (`BOT_TOKEN`, `ALLOWED_USER_ID`) are read from the environment and are
  **never** committed to source.

`shell=True` is used intentionally so full shell semantics (`cd`, pipes, `git`)
work. That is exactly why the whitelist and blocklist matter — do not remove
either as a "simplification".

## Configuration

```
BOT_TOKEN=<from @BotFather>
ALLOWED_USER_ID=<your numeric Telegram id, from @userinfobot>
```

## Usage

Once running, send the bot any message and it is executed as a shell command in
the current working directory. Commands:

- `/start` — show status and current working directory
- `/help` — show the command reference
- `/pwd` — print the current working directory
- `/cd <path>` — change the working directory for subsequent commands
- any other text — run it as a shell command

Behavior:

- Default working directory is the running user's home (`~`).
- Commands time out after 60s (`COMMAND_TIMEOUT_SECONDS`).
- stdout + stderr are combined, truncated to ~3500 chars, and returned in a code
  block.
- Every command run (and every rejected/blocked attempt) is written to
  `shell_bot.log` next to the script. The log rotates in place (5 × 2 MiB
  files), so it never grows unbounded — no external logrotate needed.

## Deployment

```bash
pip install -r requirements.txt
export BOT_TOKEN="..."
export ALLOWED_USER_ID="..."
python3 shell_bot.py
```

Prefer running under `pm2` or a systemd unit so it survives reboots and
auto-restarts on crash. A ready-to-use systemd unit (`shell_bot.service`) and
step-by-step instructions for srv1515969 — dedicated non-root user, root-owned
secrets file, venv — are in [DEPLOY.md](DEPLOY.md).
