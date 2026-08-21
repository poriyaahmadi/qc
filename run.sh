#!/bin/bash
# One-command launcher: checks basic requirements, sets up an isolated
# virtual environment (so it never fights with your system Python), makes
# sure config.json exists, then starts the server.
set -e
cd "$(dirname "$0")"

echo "🔎 Checking requirements..."

if ! command -v python3 &> /dev/null; then
  echo "❌ python3 not found. Install it from https://www.python.org/downloads/ and re-run this script."
  exit 1
fi

if ! command -v ffmpeg &> /dev/null; then
  echo "❌ ffmpeg not found. Install it first:"
  echo "   macOS:  brew install ffmpeg"
  echo "   Linux:  sudo apt install ffmpeg"
  exit 1
fi

if [ ! -f "config.json" ]; then
  echo "📝 No config.json found — creating one from the template."
  cp config.example.json config.json
  echo ""
  echo "⚠️  Open config.json and paste your API key before running this again:"
  echo "    - groq_api_key   (free, from https://console.groq.com)"
  echo "    - or anthropic_api_key + set qc_provider to \"claude\""
  echo ""
  exit 0
fi

if [ ! -d ".venv" ]; then
  echo "📦 Setting up a local Python environment (one-time, only happens once)..."
  python3 -m venv .venv
fi

echo "📦 Installing/checking Python packages..."
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo ""
echo "🚀 Starting server..."
.venv/bin/python server.py
