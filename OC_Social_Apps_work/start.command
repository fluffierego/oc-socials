#!/bin/zsh
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "Creating the Python environment..."
  python3 -m venv .venv
fi

source .venv/bin/activate
python -m pip install -r requirements.txt
echo ""
echo "Starting OC Socials..."
echo "Open http://127.0.0.1:5050 in your browser."
echo "Keep this window open while using the site."
echo ""
python app.py
