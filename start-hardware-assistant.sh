#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/hardware-assistant"
exec python3 assistant.py
