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
- `/env` — open the `.env` file manager (Mini App), if `ENV_MINIAPP_URL` is
  set; also reachable one tap away from the ☰ menu button next to the text
  box (see below)
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
- The chat keyboard has quick buttons for `git pull`, a rebuild request,
  `ls`, `/pwd`, `/cd` (resets to home), `/start`, and (if `ENV_MINIAPP_URL`
  is set) `/env`, arranged two per row. The container deliberately has no
  `docker` CLI or socket access — see the rebuild watcher
  below.

#### Rebuild watcher (also auto-deploys on push)

The container can't run `docker compose` itself (that would mean mounting the
host's docker socket into it, which is effectively root on the host — too
much blast radius for a Telegram bot to hold). Instead, `rebuild-watcher.sh`
runs on the host, outside the container, via a systemd timer (as `komodo`, no
elevated privileges beyond what that user already has) polling every 15s.
It deploys (`git pull && docker compose up -d --build`) whenever either:

- the "rebuild" quick-command button dropped a marker file
  (`.rebuild-requested`) in the repo dir, or
- `git fetch` shows `origin/main` has commits the checkout doesn't — i.e. a
  push landed, no button needed.

Either way it reports straight to the bot's chat over Telegram's HTTP API
directly (not through the bot process, so it still works even when the
deploy it's reporting on is the one restarting that process): `✅ shell-bot
deployed: <hash> <subject>` on success, or `❌ shell-bot deploy failed:` plus
the captured output (git/docker errors) on failure. Uses `BOT_TOKEN`/
`ALLOWED_USER_ID` already in `.env` — no extra config needed. Note: if
`git pull` succeeds but the rebuild fails, the checkout is already at the new
commit, so the auto-detect won't retry on its own; use the rebuild button to
retry once fixed.

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
root (`/home/komodo/projects`). Two ways to open it:

- The **☰ menu button** next to the text box — set once at bot startup via
  `set_chat_menu_button(MenuButtonWebApp(...))` in `_post_init`, one tap
  away, no typing needed.
- `/env`, which replies with an inline "🔐 Manage .env files" button.

Both use Telegram's Mini App launch mechanisms deliberately, not
`KeyboardButton.web_app` (a button on the persistent reply keyboard):
that doesn't reliably populate `Telegram.WebApp.initData` across clients
(confirmed broken on both mobile and desktop), whereas the menu button and
inline `web_app` buttons are Telegram's standard, well-tested patterns.

- **`env-manager`** — a small FastAPI backend + single-page frontend
  (`env-manager/`), with all its routes (page + API) under a `/env` prefix
  the app owns itself. Every request must carry a Telegram-signed `initData`
  proving it's `ALLOWED_USER_ID` (validated via HMAC against `BOT_TOKEN`,
  same algorithm Telegram documents for Mini Apps); file paths are resolved
  and confirmed to stay inside the projects root and end in `.env` before
  any read/write. This is the only thing standing between the public
  internet and every project's secrets, so don't weaken it.
- **No TLS-terminating container of our own.** `srv1515969.hstgr.cloud`
  already has ports 80/443 held by a host-level nginx shared with other
  projects on this VPS (`/etc/nginx/sites-enabled/strava-bot`), each
  reachable via its own path (`/jarvis/`, `/training-booking/`, etc.) proxied
  to a loopback port — running our own Caddy/nginx would just fight that
  nginx for the ports. Instead, `docker-compose.yml` publishes
  `env-manager` to `127.0.0.1:8091` only, and that same host nginx gets one
  more `location` block added by hand (not tracked in this repo — it's
  shared, multi-project, system config):

  ```nginx
  location /env {
      proxy_pass http://127.0.0.1:8091;
      proxy_set_header Host $host;
      proxy_set_header X-Real-IP $remote_addr;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto $scheme;
  }
  ```

  Add it inside the existing `server { listen 443 ssl; ... }` block for
  `srv1515969.hstgr.cloud` (above its closing `}`), then
  `sudo nginx -t && sudo systemctl reload nginx`. If port 8091 is ever taken
  by another project, change it here and in `docker-compose.yml` together.
- **`ENV_MINIAPP_URL`** (set on the `shell-bot` service) — the HTTPS URL
  shell_bot puts on the Mini App button:
  `https://srv1515969.hstgr.cloud/env`. Must match the path nginx proxies
  above.

No BotFather registration is required for this — a `web_app` button in a
private-chat keyboard just needs a valid HTTPS URL.
