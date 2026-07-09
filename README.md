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
- `/env` — view/edit the `.env` in the current directory (Telegram Mini App)
- any other text — run it as a shell command

Behavior:

- Default working directory is the running user's home (`~`).
- Commands time out after 60s (`COMMAND_TIMEOUT_SECONDS`).
- stdout + stderr are combined, truncated to ~3500 chars, and returned in a code
  block.
- Every command run (and every rejected/blocked attempt) is written to
  `shell_bot.log` next to the script. The log rotates in place (5 × 2 MiB
  files), so it never grows unbounded — no external logrotate needed.

## `.env` editor (Mini App)

`/env` opens a Telegram **Mini App** to view and edit the `.env` in the bot's
current working directory — handy right after cloning a project or `/cd`-ing
into one (interactive editors like `nano` can't run over the bot; there's no
TTY). It **only** touches the literal `.env` in the current directory and
**refuses if that file doesn't exist** (it never creates one).

This is optional and **off by default**. Enable it by setting `WEBAPP_URL` to an
HTTPS URL that fronts the bot (Telegram requires HTTPS for Mini Apps):

```
WEBAPP_URL=https://shellbot.example.com   # HTTPS root nginx proxies to the bot
WEBAPP_BIND=127.0.0.1:8081                # where the bot's app server listens
```

How it stays safe as an inbound surface:

- The bot serves the Mini App + a tiny JSON API from an aiohttp server bound to
  **loopback** (`WEBAPP_BIND`); nginx is the only public entry, over HTTPS.
- Every API request must carry a valid Telegram `initData`, verified by HMAC
  against `BOT_TOKEN` (constant-time), with a freshness check, and the embedded
  user id must equal `ALLOWED_USER_ID`. Anything else → `401`.
- The client never sends a filesystem path — the server always uses
  `<current dir>/.env`, so the endpoint can only ever read/write a `.env` in the
  directory the bot is currently pointed at.
- Reads and writes are logged (`ENV VIEW` / `ENV WRITE`) to the audit log, and a
  confirmation message is sent after each save.

See [DEPLOY.md](DEPLOY.md) for the nginx block and end-to-end test steps.

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
- **`.env` Mini App under Docker:** set `WEBAPP_URL` in `.env` to enable it. The
  compose file already binds the in-container server to `0.0.0.0:8081` and
  publishes it to the host as `127.0.0.1:8081` (loopback only), so the host's
  nginx can reverse-proxy HTTPS to it — see the nginx block in
  [DEPLOY.md](DEPLOY.md). Left blank, the feature stays off.
- The chat keyboard has quick buttons for `git pull`, a rebuild request, and
  `ls`. The container deliberately has no `docker` CLI or socket access — see
  the rebuild watcher below.

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
