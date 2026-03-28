#!/bin/bash
# Azure App Service Startup Script for Hospital Reception Bot

echo "Starting Hospital Reception Bot on Azure App Service..."
echo "Current directory: $(pwd)"
echo "Python version: $(python --version)"

# Install dependencies (in case they weren't installed during deployment)
pip install -r requirements.txt

# Start the FastAPI application with Uvicorn
# Azure App Service sets the PORT environment variable
export PORT=${PORT:-8000}

echo "Starting uvicorn on port $PORT..."

# Use 4 workers for production, binding to 0.0.0.0 for Azure
python -m uvicorn app:app \
    --host 0.0.0.0 \
    --port $PORT \
    --workers 4 \
    --loop uvloop \
    --http h11 \
    --ws websockets \
    --lifespan on \
    --no-reload
