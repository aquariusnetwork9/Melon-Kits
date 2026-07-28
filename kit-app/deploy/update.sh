#!/usr/bin/env bash
# Pull the latest code and restart the bot. Linux/systemd host.
#
#   bash kit-app/deploy/update.sh            # pull, test, restart, verify
#   bash kit-app/deploy/update.sh --no-test  # skip the suite (it takes ~25s)
#
# Restarting is the part people forget: a pull changes the checkout, not the running process,
# and the two then disagree silently. `/version` in Discord reports exactly that gap.
set -euo pipefail

CHECKOUT="${MELONKIT_CHECKOUT:-/home/ubuntu/melon-kits}"
VENV_PY="$CHECKOUT/kit-app/.venv/bin/python"
RUN_TESTS=1
[ "${1:-}" = "--no-test" ] && RUN_TESTS=0

say() { printf '  %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

cd "$CHECKOUT" || die "no checkout at $CHECKOUT"
before="$(git rev-parse --short HEAD)"
say "at $before, pulling..."
git pull --ff-only || die "pull failed - resolve it by hand rather than forcing"
after="$(git rev-parse --short HEAD)"

if [ "$before" = "$after" ]; then
    say "already up to date ($after)"
else
    say "$before -> $after"
    git --no-pager log --oneline "$before..$after" | sed 's/^/    /'
fi

# Dependencies can move under you; a bot that will not import is worse than one a version old.
say "checking dependencies..."
"$VENV_PY" -m pip install --quiet --disable-pip-version-check "discord.py>=2.4,<3"

if [ "$RUN_TESTS" = "1" ]; then
    say "running the test suite..."
    (cd "$CHECKOUT/kit-app" && "$VENV_PY" -m unittest discover -s tests -q 2>&1 | tail -3) \
        || die "tests FAILED - not restarting. The running bot is untouched."
fi

say "restarting..."
# reset-failed first: a crash-loop (a rejected token, most often) trips the start limit, and
# systemd then refuses a plain restart with a message that does not mention the counter.
sudo systemctl reset-failed melonkit-bot 2>/dev/null || true
sudo systemctl restart melonkit-bot
sleep 6
if systemctl is-active --quiet melonkit-bot; then
    say "active on $after"
    sudo journalctl -u melonkit-bot -n 3 --no-pager -o cat | sed 's/^/    /'
else
    printf 'ERROR: the service did not come back. Last lines:\n' >&2
    sudo journalctl -u melonkit-bot -n 20 --no-pager -o cat >&2
    exit 1
fi
