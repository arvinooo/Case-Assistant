#!/bin/bash
set -e
cd "$(dirname "$0")"
exec /home/arvin/.venvs/shera/bin/python app.py --port 8787 "$@"
