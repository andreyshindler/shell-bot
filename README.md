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

### Docker

```bash
cp .env.example .env && chmod 600 .env   # fill in BOT_TOKEN, ALLOWED_USER_ID,
                                          # HOST_UID, HOST_GID (from `id -u`/`id -g`)
docker compose up -d --build
```

Notes:

- The container runs as a non-root user (`botuser`), built with `HOST_UID`/
  `HOST_GID` (default 1000) so it matches the host user that owns the
  bind-mounted directories below (both `/app` and `/home/botuser`). If those
  don't match, the bot will fail to open `shell_bot.log` with
  `PermissionError`, or fail to write files when running commands.
- The repo checkout (e.g. `/home/komodo/projects/shell-bot`) is bind-mounted
  to `/app` in the container, so `shell_bot.log` lands directly in that
  directory on the host — view it with `tail -f shell_bot.log`, no `docker
  exec` needed. It's also streamed to stdout, viewable with
  `docker compose logs -f`.
- The bot's default working directory (`/home/botuser`, where `/cd` and any
  `git clone` land) is bind-mounted to the parent projects dir (e.g.
  `/home/komodo/projects`), so cloned repos and file changes land directly on
  the host, exactly like the non-Docker deployment — not in an isolated
  container volume.
- To run without compose: `docker build -t shell-bot --build-arg
  UID=$(id -u) --build-arg GID=$(id -g) . && docker run -d --name shell-bot
  --restart unless-stopped -e BOT_TOKEN -e ALLOWED_USER_ID -v
  /home/komodo/projects:/home/botuser -v
  /home/komodo/projects/shell-bot:/app shell-bot`.
- The chat keyboard has quick buttons for `git pull`, a rebuild request, `ls`,
  and (if `ENV_MINIAPP_URL` is set) a `.env` file manager. The container
  deliberately has no `docker` CLI or socket access — see the rebuild watcher
  below.

#### Rebuild watcher

The container can't run `docker compose` itself (that would mean mounting the
host's docker socket into it, which is effectively root on the host — too
much blast radius for a Telegram bot to hold). Instead, its "rebuild" quick
button just drops a marker file (`.rebuild-requested`) in the repo dir.
`rebuild-watcher.sh`, run on the host by a systemd timer (as `komodo`, no
elevated privileges beyond what that user already has), polls for that
marker every 15s and does the actual `git pull && docker compose up -d
--build` outside the container.

Install it once:

```bash
sudo cp rebuild-watcher.service rebuild-watcher.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rebuild-watcher.timer
```

Logs land in `rebuild-watcher.log` next to the script. Adjust the hardcoded
`/home/komodo/projects/shell-bot` path in `rebuild-watcher.service` and the
`User=` if your checkout lives elsewhere or runs as a different user.

#### env-manager (.env file Mini App)

A Telegram Mini App for viewing/editing any `*.env` file under the projects
root (`/home/komodo/projects`), opened via the "🔐 Manage .env files" button
in shell_bot's keyboard. Three pieces, all in `docker-compose.yml`:

- **`env-manager`** — a small FastAPI backend + single-page frontend
  (`env-manager/`), with all its routes (page + API) under a `/env` prefix
  the app owns itself — so Caddy can stay a plain reverse proxy with no path
  rewriting. Every request must carry a Telegram-signed `initData` proving
  it's `ALLOWED_USER_ID` (validated via HMAC against `BOT_TOKEN`, same
  algorithm Telegram documents for Mini Apps); file paths are resolved and
  confirmed to stay inside the projects root and end in `.env` before any
  read/write. This is the only thing standing between the public internet
  and every project's secrets, so don't weaken it.
- **`caddy`** — reverse-proxies `https://srv1515969.hstgr.cloud` (or
  whatever hostname you set in `Caddyfile`) straight to `env-manager` on
  port 8080, no path matching needed, auto-provisioning a Let's Encrypt
  certificate. Needs ports 80 and 443 free on the host — check
  `sudo ss -tlnp | grep -E ':80|:443'` first; if something else (e.g. a
  control panel) already holds them, this won't be able to get a
  certificate.
- **`ENV_MINIAPP_URL`** (set on the `shell-bot` service) — the HTTPS URL
  shell_bot puts on the Mini App button, e.g.
  `https://srv1515969.hstgr.cloud/env`. Must match the hostname in
  `Caddyfile` plus the `/env` path the app is mounted under.

No BotFather registration is required for this — a `web_app` button in a
private-chat keyboard just needs a valid HTTPS URL.
