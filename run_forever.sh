#!/bin/bash
# PolyTracker Watchdog - Keeps the bot running forever
# Automatically restarts if it crashes

cd "/Users/milann.eth/Desktop/polymarket bot"

while true; do
    echo "$(date): Starting PolyTracker..."
    python3 -m src.main >> polytracker.log 2>&1

    EXIT_CODE=$?
    echo "$(date): PolyTracker exited with code $EXIT_CODE. Restarting in 5 seconds..."
    sleep 5
done
