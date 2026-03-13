#!/bin/bash
# StockBot watchdog — restarts bot if it dies
# Run this in a loop via: nohup bash watchdog.sh >> watchdog.log 2>&1 &

LOGFILE="$HOME/workspace/stockbot/watchdog.log"
BOTDIR="$HOME/workspace/stockbot"
PYTHON="$BOTDIR/venv/bin/python3"
BOTLOG="$BOTDIR/stockbot.log"
PIDFILE="/tmp/stockbot.pid"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOGFILE"
}

is_running() {
    if [ -f "$PIDFILE" ]; then
        local pid=$(cat "$PIDFILE")
        kill -0 "$pid" 2>/dev/null && return 0
    fi
    # Also check by process name as fallback
    pgrep -f "python3 main.py" > /dev/null 2>&1 && return 0
    return 1
}

log "=== Watchdog started ==="

while true; do
    if ! is_running; then
        log "StockBot is DOWN — restarting..."
        cd "$BOTDIR"
        nohup "$PYTHON" main.py >> "$BOTLOG" 2>&1 &
        BOT_PID=$!
        echo $BOT_PID > "$PIDFILE"
        log "StockBot restarted (PID $BOT_PID)"
    fi
    sleep 60
done
