#!/usr/bin/env bash
# Bundle everything the bot needs to move to another host, and stop it running here.
#
# Run this ON THE LINUX HOST (ovh-2) as the service user. It produces one tar.gz to copy to
# the Windows box.
#
#   bash kit-app/deploy/windows/export_from_linux.sh [outfile.tar.gz]
#
# The order matters and is the whole point of the script:
#
#   1. STOP the service first. A ledger copied out from under a running bot is a torn copy,
#      and a bot still running here after the Windows one starts means two gateway sessions
#      on one token -- both acting on every interaction, against two ledgers that immediately
#      disagree about cooldowns and kit history.
#   2. DISABLE it, so a reboot cannot resurrect the second bot weeks later.
#   3. CHECKPOINT the WAL before copying. `melonkit.sqlite3` alone is the state as of the last
#      checkpoint; recent writes live in `-wal`. Copying just the one file yields a database
#      that looks fine and is silently stale -- it is exactly how you lose the last day of
#      tickets without an error anywhere.
set -euo pipefail

OUT="${1:-melonkit-export-$(date +%Y%m%d-%H%M%S).tar.gz}"
CHECKOUT="${MELONKIT_CHECKOUT:-/home/ubuntu/melon-kits}"
STATE="${MELONKIT_STATE:-/var/lib/melonkit}"
CONFIG="$CHECKOUT/kit-app/melonkit.json"
LEDGER="$STATE/melonkit.sqlite3"
LEXICON="$STATE/lexicon.json"
VENV_PY="$CHECKOUT/kit-app/.venv/bin/python"

say() { printf '  %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[ -f "$CONFIG" ] || die "no config at $CONFIG"
[ -f "$LEDGER" ] || die "no ledger at $LEDGER"

say "stopping and disabling melonkit-bot"
sudo systemctl stop melonkit-bot || die "could not stop the service"
sudo systemctl disable melonkit-bot || say "WARNING: could not disable -- do it by hand"
sleep 2
if systemctl is-active --quiet melonkit-bot; then
    die "melonkit-bot is still active; refusing to copy a live ledger"
fi

say "checkpointing the WAL"
"$VENV_PY" - "$LEDGER" <<'PY'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
# TRUNCATE folds the -wal back into the main file and empties it, so the single file that
# gets copied is genuinely complete.
mode, pages, moved = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
print("  checkpoint: mode=%s pages=%s moved=%s" % (mode, pages, moved))
print("  schema_version:",
      db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0])
print("  tickets:", db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0],
      "kits:", db.execute("SELECT COUNT(*) FROM kits").fetchone()[0],
      "claims:", db.execute("SELECT COUNT(*) FROM kit_claims").fetchone()[0])
db.close()
PY

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/melonkit"
cp "$LEDGER" "$STAGE/melonkit/melonkit.sqlite3"
# Sidecars too, belt and braces: after a TRUNCATE checkpoint they hold nothing, but copying
# them costs nothing and a -wal left behind on the source is a trap for whoever looks next.
for side in "$LEDGER-wal" "$LEDGER-shm"; do
    [ -f "$side" ] && cp "$side" "$STAGE/melonkit/" || true
done
cp "$CONFIG" "$STAGE/melonkit/melonkit.json"
[ -f "$LEXICON" ] && cp "$LEXICON" "$STAGE/melonkit/lexicon.json" || say "no lexicon to copy"

say "verifying the copy opens and matches"
"$VENV_PY" - "$STAGE/melonkit/melonkit.sqlite3" <<'PY'
import sqlite3, sys
db = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
print("  copy tickets:", db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0],
      "kits:", db.execute("SELECT COUNT(*) FROM kits").fetchone()[0],
      "claims:", db.execute("SELECT COUNT(*) FROM kit_claims").fetchone()[0])
db.close()
PY

tar -czf "$OUT" -C "$STAGE" melonkit
say "wrote $OUT"
echo
echo "The bot is STOPPED and DISABLED here. Copy $OUT to the Windows box and follow"
echo "kit-app/deploy/windows/DEPLOY-WINDOWS.md. Do not re-enable this service unless you"
echo "have first stopped the Windows one -- one token, one bot."
echo
echo "The token is NOT in this bundle. Read it from /etc/melonkit/env (root) and set it on"
echo "the Windows box yourself."
