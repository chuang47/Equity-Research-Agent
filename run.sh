#!/bin/zsh
# Start the local Equity Research dashboard and open it in the browser.
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
fi
(sleep 2 && open http://127.0.0.1:5050) &
.venv/bin/python app.py
