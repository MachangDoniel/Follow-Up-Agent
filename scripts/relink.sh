#!/usr/bin/env bash
#
# Re-link WhatsApp with a fresh pairing.
#
# Two reasons you would run this:
#
#   1. Incoming messages stopped decrypting ("Bad MAC" spam in
#      logs/whatsapp.err.log). Signal sessions cannot be repaired once they
#      desync - the keys have to be re-established, which means pairing again.
#   2. You want the chat list, unread counts and address book. WhatsApp sends
#      those to a linked device exactly once, when it pairs.
#
# The QR is printed to THIS terminal, which is the point: run it yourself and
# you can scan it off your own screen.
#
#   ./scripts/relink.sh            pair again
#   ./scripts/relink.sh --restore  undo it, back to the previous link
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
AUTH="$ROOT/data/whatsapp-auth"

cd "$ROOT"

if [ "${1:-}" = "--restore" ]; then
    backup="$(ls -d "$ROOT"/data/whatsapp-auth.bak-* 2>/dev/null | tail -1 || true)"
    if [ -z "$backup" ]; then
        echo "No backup to restore from." >&2
        exit 1
    fi
    ./scripts/service.sh stop whatsapp || true
    sleep 1
    rm -rf "$AUTH"
    mv "$backup" "$AUTH"
    ./scripts/service.sh start whatsapp
    echo "Restored from $(basename "$backup"). The old link is back."
    exit 0
fi

echo "This replaces your WhatsApp link. The current one stops working the"
echo "moment you scan, and the agent is blind to WhatsApp until you do."
echo
read -r -p "Continue? [y/N] " answer
case "$answer" in
    [yY]*) ;;
    *) echo "Nothing changed."; exit 0 ;;
esac

# The service holds the auth folder open; a second process on the same keys is
# one way sessions desync in the first place.
echo
echo "==> stopping the background watcher"
./scripts/service.sh stop whatsapp || true
sleep 2

stamp="$(date +%Y%m%d-%H%M%S)"
backup="$ROOT/data/whatsapp-auth.bak-$stamp"
if [ -d "$AUTH" ]; then
    echo "==> backing up the current link to $(basename "$backup")"
    mv "$AUTH" "$backup"
fi

cat <<'BANNER'

==> starting the watcher in the foreground

    A QR code will appear below. On your phone:

      WhatsApp > Settings > Linked devices > Link a device

    It refreshes every ~20 seconds; just scan whichever one is on screen.
    Widen this window if the code looks squashed.

    Once you see "connected as ...", wait about a minute for the history
    sync, then press Ctrl-C and run:

      ./scripts/service.sh start whatsapp

    Changed your mind? Ctrl-C now and run:

      ./scripts/relink.sh --restore

BANNER

cd "$ROOT/whatsapp"
exec npm start
