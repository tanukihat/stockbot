#!/bin/bash
# StockBot launcher — enforces single instance via PID file
cd "$(dirname "$0")"

PIDFILE="stockbot.pid"

# Kill any existing instance
if [ -f "$PIDFILE" ]; then
    OLD_PID=$(cat "$PIDFILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Stopping existing instance (PID $OLD_PID)..."
        kill "$OLD_PID"
        sleep 2
    fi
    rm -f "$PIDFILE"
fi

# Also nuke any strays just in case
pgrep -f "python3 main.py" | xargs -r kill 2>/dev/null
sleep 1

# Start fresh
echo "Starting StockBot..."
python3 main.py 2>/dev/null &
echo $! > "$PIDFILE"
echo "Started PID $(cat $PIDFILE)"
