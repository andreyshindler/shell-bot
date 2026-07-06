# Deploying shell_bot on srv1515969

Run the bot under systemd as a **dedicated, unprivileged user**, with secrets in
a **root-owned env file** that never touches the repo. Everything below assumes
you are `root` or using `sudo`.

## 1. Create a dedicated service user

Don't run this as `komodo` or any login user — give it its own account with only
the access it needs.

```bash
sudo useradd --system --create-home --home-dir /home/shellbot --shell /usr/sbin/nologin shellbot
```

## 2. Check out the repo

```bash
sudo git clone https://github.com/andreyshindler/shell-bot.git /opt/shell-bot
sudo chown -R shellbot:shellbot /opt/shell-bot
```

## 3. Install dependencies (isolated venv)

A venv keeps the bot's deps off the system Python. The unit file points at
`/opt/shell-bot/.venv/bin/python`.

```bash
sudo -u shellbot python3 -m venv /opt/shell-bot/.venv
sudo -u shellbot /opt/shell-bot/.venv/bin/pip install -r /opt/shell-bot/requirements.txt
```

(If you'd rather use the system Python, `pip install -r requirements.txt`
system-wide and change `ExecStart` in the unit to `/usr/bin/python3`.)

## 4. Create the secrets file (root-owned, 0600)

Get `BOT_TOKEN` from **@BotFather** and `ALLOWED_USER_ID` from **@userinfobot**.

```bash
sudo cp /opt/shell-bot/.env.example /etc/shell_bot.env
sudo nano /etc/shell_bot.env          # fill in both values
sudo chown root:root /etc/shell_bot.env
sudo chmod 600 /etc/shell_bot.env
```

The secrets are readable only by root; systemd injects them into the process at
start. They are never in the repo or the unit file.

## 5. Install and start the service

```bash
sudo cp /opt/shell-bot/shell_bot.service /etc/systemd/system/shell_bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now shell_bot.service
```

## 6. Verify

```bash
sudo systemctl status shell_bot.service        # should be active (running)
sudo journalctl -u shell_bot.service -f        # live logs
```

Then message the bot on Telegram:

- `/start` — should reply with status and the current working directory.
- `pwd` — should return `shellbot`'s home.
- Send a message from a *different* Telegram account — the bot must stay silent,
  and you should see a `REJECTED` line in the logs.

## Updating later

```bash
sudo -u shellbot git -C /opt/shell-bot pull
sudo -u shellbot /opt/shell-bot/.venv/bin/pip install -r /opt/shell-bot/requirements.txt
sudo systemctl restart shell_bot.service
```

## Notes

- **Why not lock the unit down harder?** The bot's whole job is running arbitrary
  shell commands as its user, so filesystem sandboxing (`ProtectSystem=strict`,
  `ProtectHome`, …) would break it. Containment comes from the dedicated non-root
  user plus the in-bot whitelist + blocklist. `NoNewPrivileges=true` is kept
  because it stops privilege escalation without limiting ordinary commands.
- **Audit log:** the bot writes `shell_bot.log` next to the script
  (`/opt/shell-bot/shell_bot.log`) in addition to journald.
- **pm2 alternative:** if you prefer pm2 to match other bots, run
  `pm2 start /opt/shell-bot/shell_bot.py --interpreter /opt/shell-bot/.venv/bin/python --name shell_bot`
  with the env vars exported first, then `pm2 save`. systemd is recommended here
  for the root-only secrets file and cleaner privilege separation.
