#!/bin/bash
# Manage the warm Laya browser service (laya_browserd.py) on 127.0.0.1:8797.
#   browserd.sh start|stop|restart|status|logs|install|uninstall
# `install` registers a launchd agent so the service is warm from login.
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${LAYA_BROWSERD_PORT:-8797}"
LOGDIR="$HOME/.local/share/laya"; mkdir -p "$LOGDIR"
LOG="$LOGDIR/browserd.log"
LABEL="com.jarvis.laya-browserd"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
alive() { curl -sf -m 2 "http://127.0.0.1:$PORT/health" 2>/dev/null | grep -q '"status":"ok"'; }
installed() { [ -f "$PLIST" ] && launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; }

case "${1:-status}" in
  start)
    if alive; then echo "[browserd] already up on 127.0.0.1:$PORT"; exit 0; fi
    if installed; then launchctl kickstart -k "gui/$(id -u)/$LABEL"; else
      nohup "$DIR/.venv/bin/python" "$DIR/laya_browserd.py" >>"$LOG" 2>&1 &
    fi
    for i in $(seq 1 120); do alive && { echo "[browserd] ready on 127.0.0.1:$PORT"; exit 0; }; sleep 1; done
    echo "[browserd] not ready after 120s; see $LOG" >&2; exit 1 ;;
  stop)
    if installed; then launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null; fi
    pkill -f "$DIR/laya_browserd.py" 2>/dev/null && echo "[browserd] stopped" || echo "[browserd] not running" ;;
  restart) "$0" stop; sleep 1; if installed; then launchctl bootstrap "gui/$(id -u)" "$PLIST"; fi; "$0" start ;;
  status) if alive; then curl -s -m 2 "http://127.0.0.1:$PORT/health"; echo; else echo "[browserd] down"; exit 1; fi ;;
  logs) tail -n 40 "$LOG" ;;
  install)
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$DIR/.venv/bin/python</string><string>$DIR/laya_browserd.py</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict></plist>
PL
    pkill -f "$DIR/laya_browserd.py" 2>/dev/null
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
    launchctl bootstrap "gui/$(id -u)" "$PLIST" && echo "[browserd] launchd agent installed: $LABEL" && "$0" start ;;
  uninstall)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null; rm -f "$PLIST"; pkill -f "$DIR/laya_browserd.py" 2>/dev/null
    echo "[browserd] launchd agent removed" ;;
  *) echo "usage: $0 start|stop|restart|status|logs|install|uninstall" >&2; exit 2 ;;
esac
