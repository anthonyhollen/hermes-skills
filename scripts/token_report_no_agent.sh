#!/bin/bash
# Wrapper for token_report_no_agent.py — sets PYTHONPATH so system python3
# can reach tabulate from venv while PIL comes from system
PYTHONPATH=/usr/lib/python3/dist-packages:$HOME/.hermes/scripts/.venv-scripts/lib/python3.11/site-packages
export PYTHONPATH
exec python3 /home/amh/.hermes/scripts/token_report_no_agent.py