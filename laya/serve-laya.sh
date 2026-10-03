#!/bin/bash
# Local Laya decision server, loopback only, multilingual checkpoint only.
# Speaks the TypeSafe /v1/systemone protocol, so Jev Browser can use it as its brain.
#   serve-laya.sh start|stop|status|logs
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${LAYA_PORT:-8799}"
PPORT="${JEV_LAYA_PORT:-8798}"
LOGDIR="$HOME/.local/share/laya"; mkdir -p "$LOGDIR"
LOG="$LOGDIR/serve.log"; PIDFILE="$LOGDIR/serve.pid"

alive() { curl -sf -m 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; }
proxy_alive() { curl -sf -m 2 "http://127.0.0.1:$PPORT/health" >/dev/null 2>&1; }
start_proxy() {
  proxy_alive && return 0
  JEV_LAYA_PORT="$PPORT" JEV_LAYA_UPSTREAM="http://127.0.0.1:$PORT" \
    nohup "$DIR/.venv/bin/python" "$DIR/jev_laya_proxy.py" >>"$LOGDIR/proxy.log" 2>&1 &
  for i in $(seq 1 10); do proxy_alive && { echo "[laya] jev proxy ready on 127.0.0.1:$PPORT"; return 0; }; sleep 0.5; done
  echo "[laya] proxy failed; see $LOGDIR/proxy.log" >&2; return 1
}

case "${1:-start}" in
  start)
    if alive; then echo "[laya] already serving on 127.0.0.1:$PORT"; start_proxy; exit $?; fi
    echo "[laya] starting on 127.0.0.1:$PORT (multilingual only) -> $LOG"
    LAYA_HOST=127.0.0.1 LAYA_PORT="$PORT" LAYA_MODELS=multilingual LAYA_DEFAULT_MODEL=multilingual \
    LAYA_MAX_LOADED=1 LAYA_PRELOAD=1 LAYA_LOG_LEVEL=warning \
      nohup "$DIR/.venv/bin/python" "$DIR/serve_laya.py" >>"$LOG" 2>&1 &
    echo $! > "$PIDFILE"
    for i in $(seq 1 90); do alive && { echo "[laya] ready"; start_proxy; exit $?; }; sleep 1; done
    echo "[laya] did not become ready in 90s; see $LOG" >&2; exit 1 ;;
  stop)
    pkill -f "$DIR/jev_laya_proxy.py" 2>/dev/null && echo "[laya] proxy stopped"
    pkill -f "$DIR/serve_laya.py" 2>/dev/null && echo "[laya] stopped" || echo "[laya] not running"
    rm -f "$PIDFILE" ;;
  status)
    if alive; then curl -s -m 2 "http://127.0.0.1:$PORT/health"; echo; else echo "[laya] not running on 127.0.0.1:$PORT"; exit 1; fi ;;
  logs) tail -n 40 "$LOG"; echo "--- proxy ---"; tail -n 20 "$LOGDIR/proxy.log" 2>/dev/null ;;
  *) echo "usage: $0 start|stop|status|logs" >&2; exit 2 ;;
esac
