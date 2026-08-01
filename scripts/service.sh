#!/usr/bin/env bash
# Manage call-followup as launchd user agents: start at login, restart on failure.
#
#   ./scripts/service.sh install | uninstall | start | stop | restart | status | logs
#
# There are two services, because Baileys is Node-only:
#   brain     - python -m app  (HTTP brain + Telegram watcher)
#   whatsapp  - node watcher.js
#
# Every command takes an optional target: all (default), brain, whatsapp.
#   ./scripts/service.sh restart whatsapp
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOMAIN="gui/$(id -u)"

LABEL_BRAIN="com.donieltripura.call-followup"
LABEL_WHATSAPP="com.donieltripura.call-followup-whatsapp"

PYTHON="$ROOT/.venv/bin/python"

die() { echo "error: $*" >&2; exit 1; }

plist_path() { echo "$HOME/Library/LaunchAgents/$1.plist"; }

label_for() {
    case "$1" in
        brain)    echo "$LABEL_BRAIN" ;;
        whatsapp) echo "$LABEL_WHATSAPP" ;;
        *) die "unknown target '$1' (use: all, brain, whatsapp)" ;;
    esac
}

targets_for() {
    case "${1:-all}" in
        all)      echo "brain whatsapp" ;;
        brain)    echo "brain" ;;
        whatsapp) echo "whatsapp" ;;
        *) die "unknown target '$1' (use: all, brain, whatsapp)" ;;
    esac
}

# ---------------------------------------------------------------------------

write_plist() {
    local target="$1" label plist program workdir out err
    label="$(label_for "$target")"
    plist="$(plist_path "$label")"

    if [[ "$target" == brain ]]; then
        [[ -x "$PYTHON" ]] || die "no venv at $PYTHON — run 'uv sync' first"
        program="<string>$PYTHON</string>
        <string>-m</string>
        <string>app</string>"
        workdir="$ROOT"
        out="$ROOT/logs/launchd.out.log"
        err="$ROOT/logs/launchd.err.log"
    else
        local node
        node="$(command -v node)" || die "node not found on PATH"
        [[ -d "$ROOT/whatsapp/node_modules" ]] || die "run 'npm install' in whatsapp/ first"
        program="<string>$node</string>
        <string>watcher.js</string>"
        workdir="$ROOT/whatsapp"
        out="$ROOT/logs/whatsapp.out.log"
        err="$ROOT/logs/whatsapp.err.log"
    fi

    [[ -f "$ROOT/.env" ]] || die "no .env at $ROOT/.env"
    mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/logs"

    cat > "$plist" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$label</string>

    <key>ProgramArguments</key>
    <array>
        $program
    </array>

    <key>WorkingDirectory</key>
    <string>$workdir</string>

    <key>RunAtLoad</key>
    <true/>

    <!-- Restart if it exits non-zero or crashes; stay down after a clean stop. -->
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
        <key>Crashed</key>
        <true/>
    </dict>

    <!-- Don't hammer: at most one restart attempt per minute. -->
    <key>ThrottleInterval</key>
    <integer>60</integer>

    <key>ProcessType</key>
    <string>Background</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>

    <key>StandardOutPath</key>
    <string>$out</string>
    <key>StandardErrorPath</key>
    <string>$err</string>
</dict>
</plist>
PLIST_EOF
    echo "wrote $plist"
}

is_loaded() { launchctl print "$DOMAIN/$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------------------

cmd_install() {
    for target in $(targets_for "${1:-all}"); do
        local label plist
        label="$(label_for "$target")"
        plist="$(plist_path "$label")"
        write_plist "$target"
        is_loaded "$label" && launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
        launchctl bootstrap "$DOMAIN" "$plist"
        launchctl enable "$DOMAIN/$label"
        echo "installed $target"
    done
    echo
    echo "they will now come up at login. safe to close your terminal."
    sleep 3
    cmd_status "${1:-all}"
}

cmd_uninstall() {
    for target in $(targets_for "${1:-all}"); do
        local label plist
        label="$(label_for "$target")"
        plist="$(plist_path "$label")"
        is_loaded "$label" && launchctl bootout "$DOMAIN/$label" || true
        rm -f "$plist"
        echo "uninstalled $target"
    done
}

cmd_start() {
    for target in $(targets_for "${1:-all}"); do
        local label plist
        label="$(label_for "$target")"
        plist="$(plist_path "$label")"
        [[ -f "$plist" ]] || die "$target not installed — run '$0 install'"
        is_loaded "$label" || launchctl bootstrap "$DOMAIN" "$plist"
        launchctl kickstart "$DOMAIN/$label"
        echo "started $target"
    done
}

cmd_stop() {
    for target in $(targets_for "${1:-all}"); do
        local label
        label="$(label_for "$target")"
        if is_loaded "$label"; then
            launchctl bootout "$DOMAIN/$label"
            echo "stopped $target"
        else
            echo "$target not running"
        fi
    done
}

cmd_restart() {
    for target in $(targets_for "${1:-all}"); do
        local label plist
        label="$(label_for "$target")"
        plist="$(plist_path "$label")"
        [[ -f "$plist" ]] || die "$target not installed — run '$0 install'"
        if is_loaded "$label"; then
            launchctl kickstart -k "$DOMAIN/$label"
        else
            launchctl bootstrap "$DOMAIN" "$plist"
        fi
        echo "restarted $target"
    done
}

cmd_status() {
    for target in $(targets_for "${1:-all}"); do
        local label pid last
        label="$(label_for "$target")"
        printf '%-9s ' "$target:"
        if ! is_loaded "$label"; then
            echo "not loaded"
            [[ -f "$(plist_path "$label")" ]] || echo "          (not installed — run '$0 install')"
            continue
        fi
        pid=$(launchctl print "$DOMAIN/$label" | awk '/^\tpid = /{print $3}')
        last=$(launchctl print "$DOMAIN/$label" | awk '/last exit code = /{print $NF}')
        if [[ -n "${pid:-}" ]]; then
            echo "running (pid $pid)"
        else
            echo "loaded but not running (last exit code: ${last:-?})"
        fi
    done

    echo
    if curl -fsS -m 3 http://127.0.0.1:8787/health 2>/dev/null; then
        echo "  <- brain responding"
    else
        echo "brain:    not responding on :8787"
    fi
    echo
    echo "recent activity:"
    tail -n 4 "$ROOT/logs/call-followup.log" 2>/dev/null | sed 's/^/  /' || echo "  (no log yet)"
}

cmd_logs() {
    case "${1:-all}" in
        brain)    tail -f "$ROOT/logs/call-followup.log" ;;
        whatsapp) tail -f "$ROOT/logs/whatsapp.out.log" ;;
        *)        tail -f "$ROOT/logs/call-followup.log" "$ROOT/logs/whatsapp.out.log" ;;
    esac
}

case "${1:-}" in
    install)   cmd_install "${2:-all}" ;;
    uninstall) cmd_uninstall "${2:-all}" ;;
    start)     cmd_start "${2:-all}" ;;
    stop)      cmd_stop "${2:-all}" ;;
    restart)   cmd_restart "${2:-all}" ;;
    status)    cmd_status "${2:-all}" ;;
    logs)      cmd_logs "${2:-all}" ;;
    *) echo "usage: $0 {install|uninstall|start|stop|restart|status|logs} [all|brain|whatsapp]" >&2; exit 1 ;;
esac
