#!/bin/bash
PYTHONPATH=/usr/lib/python3/dist-packages:$HOME/.hermes/scripts/.venv-scripts/lib/python3.11/site-packages
export PYTHONPATH
exec python3 /home/amh/.hermes/scripts/token_report.py