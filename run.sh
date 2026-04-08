#!/bin/bash
# run.sh — Convenience wrapper for the single-entry app.py launcher

cd "$(dirname "$0")"
exec .venv/bin/python app.py
