#!/usr/bin/env bash
#
# One-command installer for the Melon Men kit-request bot, on any systemd Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/aquariusnetwork9/Melon-Kits/main/install.sh | sudo bash
#
# Re-run it any time to upgrade: it pulls, reinstalls dependencies and restarts, and it will
# not overwrite your token, your config or your ledger. Nothing here is interactive except the
# token prompt, and even that can be supplied up front (see --help).
#
# What it sets up, and why each piece is where it is:
#
#   /opt/melon-kits            the checkout, owned by the service user, read-only at runtime
#   /var/lib/melonkit          the LEDGER. Deliberately outside the checkout so that a pull, a
#                              re-clone or a botched upgrade cannot touch kit history
#   /etc/melonkit/env          the token, and nothing else. root-owned 600, read by systemd
#                              before it drops privileges, so the bot's own user cannot read it
#                              off disk and it can never be committed
#
# It creates a dedicated unprivileged `melonkit` user rather than running as root or as a login
# account. The bot opens its ledger on startup, so anything that runs it as root leaves a
# root-owned database that the service can read but not write -- which does not fail at startup,
# only on the first real ticket. A dedicated user makes that mistake unavailable.
set -Eeuo pipefail

REPO_URL="${MELONKIT_REPO:-https://github.com/aquariusnetwork9/Melon-Kits.git}"
BRANCH="${MELONKIT_BRANCH:-main}"
SERVICE_USER="${MELONKIT_USER:-melonkit}"
INSTALL_DIR="${MELONKIT_DIR:-/opt/melon-kits}"
# Overridable so a second instance can coexist on one host -- and so this installer can be
# rehearsed on a box that already runs one without touching it, which is the only honest way to
# test it. Defaults are what a normal single install gets.
DATA_DIR="${MELONKIT_DATA_DIR:-/var/lib/melonkit}"
ETC_DIR="${MELONKIT_ETC_DIR:-/etc/melonkit}"
UNIT_NAME="${MELONKIT_UNIT_NAME:-melonkit-bot}"
UNIT="/etc/systemd/system/${UNIT_NAME}.service"
TOKEN="${MELONKIT_DISCORD_TOKEN:-}"
# api.2b2t.vc is run by a volunteer and Cloudflare 403s a default user agent, so this wants to
# be a real way to reach whoever runs the bot.
CONTACT="${MELONKIT_CONTACT:-set-me@example.com}"
ASSUME_YES=0
DO_UNINSTALL=0

# Everything the bot needs, as one number for the invite link. Worked out from discord.py's own
# flags rather than copied from a permissions calculator, so it cannot drift from what the code
# asks for. pin_messages is the odd one: Discord split it out of Manage Messages, so a bot with
# Manage Messages alone still gets a 403 on a pin.
PERMS=2252160859499536

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m !!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<EOF
Melon Men kit bot installer.

  sudo bash install.sh [options]

Options:
  --token <TOKEN>   bot token, instead of being prompted for it
  --user <NAME>     service user to create and run as   (default: $SERVICE_USER)
  --dir <PATH>      where to put the checkout           (default: $INSTALL_DIR)
  --branch <NAME>   branch to track                     (default: $BRANCH)
  --contact <TEXT>  contact address sent to api.2b2t.vc in the user agent
  --yes             never prompt; requires --token on a first install
  --uninstall       stop and remove the service, KEEPING the ledger and token
  --help            this

The token can also come from the MELONKIT_DISCORD_TOKEN environment variable, which is the
right way to do it from a provisioning tool -- a token on the command line is visible in ps
and lands in your shell history.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --token) TOKEN="${2:-}"; shift 2 ;;
        --contact) CONTACT="${2:-}"; shift 2 ;;
        --user)  SERVICE_USER="${2:-}"; shift 2 ;;
        --dir)   INSTALL_DIR="${2:-}"; shift 2 ;;
        --branch) BRANCH="${2:-}"; shift 2 ;;
        --yes|-y) ASSUME_YES=1; shift ;;
        --uninstall) DO_UNINSTALL=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) die "unknown option: $1  (try --help)" ;;
    esac
done

# --------------------------------------------------------------------- preflight

[ "$(id -u)" -eq 0 ] || die "run this with sudo: curl -fsSL <url> | sudo bash"
command -v systemctl >/dev/null 2>&1 || die "this installer needs systemd, which this system does not appear to have"

if [ "$DO_UNINSTALL" = 1 ]; then
    say "Stopping and removing the service"
    systemctl disable --now "$UNIT_NAME" 2>/dev/null || true
    rm -f "$UNIT"
    systemctl daemon-reload
    say "Service removed."
    echo
    echo "Deliberately left in place, because they are the parts you cannot regenerate:"
    echo "  $DATA_DIR     the ledger - every ticket, decision and reason"
    echo "  $ETC_DIR/env  the token"
    echo "  $INSTALL_DIR  the checkout"
    echo
    echo "Delete them by hand if you really mean to. Back up $DATA_DIR first."
    exit 0
fi

# --------------------------------------------------------------------- packages

install_packages() {
    local pkgs_apt="git python3-venv python3-pip ca-certificates"
    local pkgs_dnf="git python3-pip"
    local pkgs_pac="git python-pip"
    if command -v apt-get >/dev/null 2>&1; then
        say "Installing packages with apt"
        apt-get update -qq </dev/null
        # shellcheck disable=SC2086
        apt-get install -y -qq $pkgs_apt </dev/null
    elif command -v dnf >/dev/null 2>&1; then
        say "Installing packages with dnf"
        # shellcheck disable=SC2086
        dnf install -y -q $pkgs_dnf
    elif command -v pacman >/dev/null 2>&1; then
        say "Installing packages with pacman"
        # shellcheck disable=SC2086
        pacman -Sy --noconfirm --needed $pkgs_pac
    else
        warn "No apt/dnf/pacman found. Make sure git and a working python3 venv are installed."
    fi
}
install_packages

command -v git >/dev/null 2>&1 || die "git is still not available after the package step"
PY="$(command -v python3 || true)"
[ -n "$PY" ] || die "python3 is not installed and could not be installed automatically"

# 3.9 is the floor: the code uses dict ordering guarantees and modern typing throughout, and
# discord.py 2.4 itself requires 3.8+.
"$PY" - <<'PYEOF' || die "python3 is older than 3.9; install a newer python3 and re-run"
import sys
sys.exit(0 if sys.version_info >= (3, 9) else 1)
PYEOF
say "Using $("$PY" --version 2>&1)"

# --------------------------------------------------------------------- user and dirs

if id -u "$SERVICE_USER" >/dev/null 2>&1; then
    say "Service user $SERVICE_USER already exists"
else
    say "Creating service user $SERVICE_USER"
    # A system account with no login shell and no home directory to speak of. It owns the
    # checkout and the ledger and nothing else.
    useradd --system --shell /usr/sbin/nologin --home-dir "$INSTALL_DIR" \
            --comment "Melon Men kit bot" "$SERVICE_USER" 2>/dev/null \
      || useradd --system --shell /sbin/nologin --home-dir "$INSTALL_DIR" \
            --comment "Melon Men kit bot" "$SERVICE_USER"
fi

install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 750 "$DATA_DIR"
install -d -o root -g root -m 750 "$ETC_DIR"

# --------------------------------------------------------------------- the code

if [ -d "$INSTALL_DIR/.git" ]; then
    say "Updating the existing checkout in $INSTALL_DIR"
    git config --global --add safe.directory "$INSTALL_DIR" 2>/dev/null || true
    sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" fetch --quiet origin "$BRANCH"
    # Hard reset rather than pull: this directory is ours, and a merge conflict here would
    # leave a half-updated bot running. Local edits belong somewhere else.
    sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" reset --quiet --hard "origin/$BRANCH"
else
    say "Cloning $REPO_URL into $INSTALL_DIR"
    rm -rf "$INSTALL_DIR"
    install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 755 "$INSTALL_DIR"
    sudo -u "$SERVICE_USER" git clone --quiet --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
    git config --global --add safe.directory "$INSTALL_DIR" 2>/dev/null || true
fi
APP_DIR="$INSTALL_DIR/kit-app"
[ -f "$APP_DIR/bot.py" ] || die "$APP_DIR/bot.py is missing -- the clone did not produce what was expected"

# --------------------------------------------------------------------- virtualenv

VENV="$APP_DIR/.venv"
if [ ! -x "$VENV/bin/python" ]; then
    say "Creating the virtualenv"
    # Checked by actually creating one, never by importing venv: on Debian and Ubuntu the venv
    # MODULE is present while ensurepip is a separate package, so `import venv` succeeds and
    # `python3 -m venv` then fails. This is the single most common way this install goes wrong.
    if ! sudo -u "$SERVICE_USER" "$PY" -m venv "$VENV" 2>/tmp/melonkit-venv.err; then
        warn "python3 -m venv failed:"
        sed 's/^/    /' /tmp/melonkit-venv.err >&2
        ver="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
        if command -v apt-get >/dev/null 2>&1; then
            say "Retrying after installing python${ver}-venv"
            apt-get install -y -qq "python${ver}-venv" </dev/null || true
            sudo -u "$SERVICE_USER" "$PY" -m venv "$VENV" \
              || die "still cannot create a virtualenv -- install python${ver}-venv by hand"
        else
            die "cannot create a virtualenv; install your distribution's python venv/ensurepip package"
        fi
    fi
fi

say "Installing dependencies"
sudo -u "$SERVICE_USER" "$VENV/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
sudo -u "$SERVICE_USER" "$VENV/bin/pip" install --quiet -r "$APP_DIR/requirements.txt" \
  || die "dependency install failed"

# --------------------------------------------------------------------- the token

if [ -s "$ETC_DIR/env" ] && [ -z "$TOKEN" ]; then
    say "Keeping the token already in $ETC_DIR/env"
else
    if [ -z "$TOKEN" ]; then
        [ "$ASSUME_YES" = 1 ] && die "--yes was given but there is no token: pass --token or set MELONKIT_DISCORD_TOKEN"
        [ -t 0 ] || die "no token supplied and no terminal to ask on. Pass --token, or set MELONKIT_DISCORD_TOKEN, or run the installer from a shell rather than through a pipe."
        echo
        echo "Paste the bot token from the Discord developer portal."
        echo "  https://discord.com/developers/applications  ->  your app  ->  Bot  ->  Reset Token"
        echo "It will not be shown as you type, and it is written only to $ETC_DIR/env (root, 600)."
        printf 'Token: '
        read -rs TOKEN
        echo
    fi
    [ -n "$TOKEN" ] || die "no token given"
    # A Discord bot token is three dot-separated parts. Catching a truncated paste here saves a
    # confusing "Discord rejected the token" five steps later.
    case "$TOKEN" in
        *.*.*) : ;;
        *) warn "that does not look like a bot token (expected three dot-separated parts). Continuing anyway." ;;
    esac
    umask 077
    printf '# Melon Men kit bot. This file is the ONLY place the token lives.\n' >"$ETC_DIR/env"
    printf '# After changing it: sudo systemctl restart %s\n' "$UNIT_NAME" >>"$ETC_DIR/env"
    printf 'MELONKIT_DISCORD_TOKEN=%s\n' "$TOKEN" >>"$ETC_DIR/env"
    umask 022
    chown root:root "$ETC_DIR/env"
    chmod 600 "$ETC_DIR/env"
    say "Token written to $ETC_DIR/env (root-owned, 600)"
fi

# --------------------------------------------------------------------- config and lexicon

CONFIG="$APP_DIR/melonkit.json"
if [ -f "$CONFIG" ]; then
    say "Keeping the existing $CONFIG"
else
    say "Writing $CONFIG"
    # No comment keys in here. config.py treats ANY unknown key as a hard startup error --
    # deliberately, so a typo cannot silently do nothing -- and that includes a "_comment" one.
    # The notes that would have gone here are in DEPLOY.md and in the summary this prints at the
    # end, where they will actually be read.
    cat >"$CONFIG" <<EOF
{
  "store": { "path": "$DATA_DIR/melonkit.sqlite3" },
  "screening": { "lexicon_path": "$DATA_DIR/lexicon.json" },
  "vc": { "user_agent": "melon-kits/1.0 (+contact: $CONTACT)" }
}
EOF
    chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG"
    chmod 640 "$CONFIG"
fi

# Validated before systemd ever sees it, because an invalid config makes the unit flap five
# times and stop, and the reason is then buried in a journal the installer has already told you
# to go and read. Importing config touches neither the ledger nor the token.
if ! sudo -u "$SERVICE_USER" "$VENV/bin/python" -c "
import sys
sys.path.insert(0, '$APP_DIR')
import config
config.load_config('$CONFIG')
" 2>/tmp/melonkit-config.err; then
    warn "$CONFIG is not valid:"
    sed 's/^/    /' /tmp/melonkit-config.err >&2
    die "fix or delete $CONFIG and re-run"
fi

if [ -f "$DATA_DIR/lexicon.json" ]; then
    say "Keeping the existing lexicon"
else
    say "Installing the starter lexicon"
    # Shipped with the off_game and scam lists filled in and the slur/profanity lists EMPTY.
    # Those are deliberately not in a public repo; add your own with `python tools/mine.py`, or
    # the screening simply reports nothing for them, which is a safe default rather than a
    # broken one.
    install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 640 \
        "$APP_DIR/lexicon.example.json" "$DATA_DIR/lexicon.json"
fi

# --------------------------------------------------------------------- systemd

say "Installing the systemd unit"
TEMPLATE="$APP_DIR/deploy/melonkit-bot.service.in"
[ -f "$TEMPLATE" ] || die "$TEMPLATE is missing"
sed -e "s|@@USER@@|$SERVICE_USER|g" \
    -e "s|@@APPDIR@@|$APP_DIR|g" \
    -e "s|@@DATADIR@@|$DATA_DIR|g" \
    -e "s|@@ETCDIR@@|$ETC_DIR|g" \
    "$TEMPLATE" >"$UNIT"
chmod 644 "$UNIT"
if command -v systemd-analyze >/dev/null 2>&1; then
    systemd-analyze verify "$UNIT" 2>&1 | sed 's/^/    /' || warn "systemd-analyze reported the above; continuing"
fi
systemctl daemon-reload
systemctl enable --quiet "$UNIT_NAME"
say "Starting the bot"
# Clear any latched failure first. The unit stops itself after five failed starts in five
# minutes (deliberately -- a crash-loop against Discord's auth endpoint gets the application
# rate-limited), and systemd then REFUSES a plain restart until the counter is reset. Without
# this, the single most likely recovery path -- mistype the token, re-run with the right one --
# fails with "control process exited with error code" and never gets as far as the new token.
systemctl reset-failed "$UNIT_NAME" 2>/dev/null || true
# Not fatal: a failure here is the interesting case, and the diagnostic block below explains it
# far better than `set -e` killing the script silently would.
systemctl restart "$UNIT_NAME" || true

# --------------------------------------------------------------------- did it work

sleep 8
if systemctl is-active --quiet "$UNIT_NAME"; then
    STATUS="running"
else
    STATUS="not running"
fi

echo
# Read the journal ONCE into a variable rather than piping it into grep -q per test. Under
# `set -o pipefail`, a matching `grep -q` exits early, journalctl takes SIGPIPE, and the whole
# pipeline reports failure -- so the branch that matched is the one that silently does not fire.
# That bug hid the "Discord rejected that token" message behind the generic one.
LOG="$(journalctl -u "$UNIT_NAME" --since "-2 min" --no-pager -o cat 2>/dev/null || true)"

if printf '%s' "$LOG" | grep -q "connected as"; then
    WHO="$(printf '%s' "$LOG" | grep 'connected as' | tail -1 | sed 's/.*connected as //')"
    say "Connected to Discord as $WHO"
elif printf '%s' "$LOG" | grep -qi "rejected the token"; then
    warn "Discord rejected that token."
    echo "    Reset it at https://discord.com/developers/applications -> Bot -> Reset Token,"
    echo "    then re-run this installer and paste the new one."
    exit 3
else
    warn "The bot is $STATUS and did not report a successful connection within 8 seconds."
    echo
    echo "  What it actually said:"
    # Printed rather than pointed at. Somebody following a one-command install should not have
    # to learn journalctl to find out why that one command did not work, and the reason is
    # usually the single line above the first "Main process exited".
    printf '%s\n' "$LOG" \
        | grep -v -e '^$' -e 'Scheduled restart' -e 'Stopped ' -e 'Started ' \
        | tail -12 | sed 's/^/    /'
    echo
    echo "  Full log:  sudo journalctl -u $UNIT_NAME -n 50 --no-pager"
    [ "$STATUS" = "running" ] || exit 4
fi

# The first segment of a bot token is the application id in base64, so the invite link can be
# built without asking for it separately -- one less thing to copy from the portal wrongly.
APP_ID="$("$PY" - "$ETC_DIR/env" <<'PYEOF' 2>/dev/null || true
import base64, re, sys
line = ""
for row in open(sys.argv[1], encoding="utf-8", errors="replace"):
    if row.startswith("MELONKIT_DISCORD_TOKEN="):
        line = row.split("=", 1)[1].strip()
seg = line.split(".")[0]
seg += "=" * (-len(seg) % 4)
try:
    out = base64.b64decode(seg).decode("ascii")
    print(out if re.fullmatch(r"\d{15,25}", out) else "")
except Exception:
    print("")
PYEOF
)"

cat <<EOF

$(printf '\033[1;32m%s\033[0m' "Installed.")

  service    $UNIT_NAME        (systemctl status $UNIT_NAME)
  code       $APP_DIR
  ledger     $DATA_DIR/melonkit.sqlite3
  token      $ETC_DIR/env
  logs       journalctl -u $UNIT_NAME -f

Two things left, both in Discord:

 1. Invite the bot to your server, if you have not already:
EOF
if [ -n "$APP_ID" ]; then
    echo "    https://discord.com/oauth2/authorize?client_id=$APP_ID&scope=bot+applications.commands&permissions=$PERMS"
else
    echo "    https://discord.com/oauth2/authorize?client_id=YOUR_APPLICATION_ID&scope=bot+applications.commands&permissions=$PERMS"
    echo "    (application id is on the General Information page of your app)"
fi
cat <<EOF

 2. Run  /setup  in your server, as somebody with Manage Server. It will offer to use your
    existing roles and channels, and creates whatever is missing. Nothing works until it runs:
    channel and role ids live per-guild in the ledger, not in any config file here.

To upgrade later, re-run this same command. Your token, config and ledger are left alone.
EOF
