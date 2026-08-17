#!/bin/bash

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Starting Philomath..."
echo ""

# Start backend
(
    cd "$ROOT_DIR/backend" || exit 1

    if [ -f "venv/bin/activate" ]; then
        source venv/bin/activate
    elif [ -f ".venv/bin/activate" ]; then
        source .venv/bin/activate
    else
        echo "Backend virtual environment not found."
        exit 1
    fi

    python -m uvicorn app.main:app --reload
) &

BACKEND_PID=$!

# Start frontend
(
    cd "$ROOT_DIR/frontend" || exit 1
    npm run dev
) &

FRONTEND_PID=$!

# Stop both when Ctrl+C is pressed
cleanup() {
    echo ""
    echo "Stopping Philomath..."

    kill "$BACKEND_PID" 2>/dev/null
    kill "$FRONTEND_PID" 2>/dev/null

    wait "$BACKEND_PID" 2>/dev/null
    wait "$FRONTEND_PID" 2>/dev/null

    echo "Philomath stopped."
    exit 0
}

trap cleanup INT TERM

wait