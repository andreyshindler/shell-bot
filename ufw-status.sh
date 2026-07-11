#!/bin/sh
# Host-side: snapshot ufw firewall state + a summary of recently blocked
# connections, for the shell_bot /ufw button to read (the container can't run
# ufw itself — ufw lives on the host). Run by cron as root; see README.
#
# Writes to the projects dir, which the shell-bot container sees as the bot
# user's home (bind mount). Adjust OUT if your projects dir differs.
PATH=/usr/sbin:/usr/bin:/bin
OUT=/home/komodo/projects/.ufw-status.txt

# Gather blocked-packet log lines — prefer ufw's dedicated log, else the kernel
# journal (ufw logs UFW BLOCK entries when logging is on).
if [ -f /var/log/ufw.log ]; then
    BLOCKS=$(grep 'UFW BLOCK' /var/log/ufw.log 2>/dev/null)
else
    BLOCKS=$(journalctl -k --since '-24h' 2>/dev/null | grep 'UFW BLOCK')
fi
COUNT=$(printf '%s\n' "$BLOCKS" | grep -c 'UFW BLOCK')

{
    ufw status verbose
    echo
    echo "=== Blocked connections in log: ${COUNT} ==="
    if [ "${COUNT}" -gt 0 ]; then
        echo
        echo "Top source IPs:"
        printf '%s\n' "$BLOCKS" | grep -oE 'SRC=[0-9.]+' | sort | uniq -c | sort -rn | head -10
        echo
        echo "Top destination ports:"
        printf '%s\n' "$BLOCKS" | grep -oE 'DPT=[0-9]+' | sort | uniq -c | sort -rn | head -10
    fi
} > "$OUT" 2>&1
