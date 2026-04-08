#!/bin/bash
# run.sh — Start gtt_strategy + upstox_order_tracker together
# Usage: ./run.sh
# Stop: Ctrl+C

cd "$(dirname "$0")"

echo "Starting UpstoxGTT..."

# Start both processes
.venv/bin/python gtt_strategy.py &
PID1=$!

.venv/bin/python upstox_order_tracker.py &
PID2=$!

echo "gtt_strategy  PID: $PID1"
echo "upstox_order_tracker PID: $PID2"
echo "Press Ctrl+C to stop both."

# On Ctrl+C, kill both
trap "echo 'Stopping...'; kill $PID1 $PID2 2>/dev/null; exit 0" SIGINT SIGTERM

wait $PID1 $PID2
