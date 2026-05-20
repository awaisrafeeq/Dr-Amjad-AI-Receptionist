#!/bin/bash
# Azure App Service Startup Script for Hospital Reception Bot

echo "Starting Hospital Reception Bot on Azure App Service..."
echo "Current directory: $(pwd)"
echo "Python version: $(python --version)"
echo "App directory contents:"
ls -la
echo "Routers directory contents:"
ls -la routers || true

# Install dependencies in the active App Service Python environment if Oryx
# did not create antenv during deployment.
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# Start the FastAPI application with Uvicorn
# Azure App Service sets the PORT environment variable
export PORT=${PORT:-8000}
export PYTHONPATH="$(pwd):${PYTHONPATH:-}"

echo "Starting uvicorn on port $PORT..."

# Use a single worker because call/session state is kept in memory.
# Multiple workers can route ACS callbacks and WebSocket streams to different
# processes, causing duplicate greetings, voice mixing, and cleanup races.
exec python -m uvicorn app:app \
    --host 0.0.0.0 \
    --port $PORT \
    --app-dir "$(pwd)" \
    --loop uvloop \
    --http h11 \
    --ws websockets \
    --lifespan on \
    --no-reload
