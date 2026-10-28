#!/usr/bin/env python3
"""sg_econ_cron.py — no_agent cron entry for the sg-econ-barometer skill.

The cron runner invokes a bare script name with no args, so this thin wrapper
runs the engine in monthly mode: fetch + compute + update history, then self-post
the barometer to the Discord home channel. --quiet means a successful post leaves
stdout empty (deliver: local => silent); on a delivery failure the engine prints
the report to the saved job output and exits non-zero. Manual/on-demand runs
should call sg_econ_barometer.py directly.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sg_econ_barometer  # noqa: E402

sys.exit(sg_econ_barometer.main(["--post-home", "--quiet"]))
