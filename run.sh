#!/bin/bash
# StockBot launcher — prevents duplicate instances
PIDFILE="/tmp/stockbot.pid"
if [ -f "$PIDFILE" ] && kill -0 $(cat "$PIDFILE") 2>/dev/null; then
    echo "StockBot already running (PID $(cat $PIDFILE)). Exiting."
    exit 1
fi
cd ~/workspace/stockbot
echo $$ > "$PIDFILE"
trap "rm -f $PIDFILE" EXIT
python3 main.py
