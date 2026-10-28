#!/usr/bin/env python3
"""
backup_cleaner_cron.py — weekly cron entry for the backup-cleaner skill.

Runs the engine with --delete: it AUTO-REMOVES the validated DELETABLE set (backups whose
preceding update was proven safe by a successful post-update cron run) and posts the full
list to Discord as a table image (via --post, rendered through the discord-table-format
skill), captioned with what was removed. Only the strictly-validated set is ever deleted —
NOT_YET / NO_CRON / ORPHAN / ANOMALY backups are report-only and never touched, and every
removal is logged to the manifest. The engine posts the image itself, so the cron uses
`deliver: local` (nothing to deliver via stdout); it is silent when there is nothing to remove.
"""
import subprocess
import sys

ENGINE = "/home/amh/.hermes/scripts/backup_cleaner.py"
REPORT_CHANNEL = "1499972245437550602"  # #alerts

if __name__ == "__main__":
    # spawn with the same (venv) interpreter that runs this cron script.
    # --delete: auto-remove the validated set (the report/caption then says what was removed).
    # --quiet: silent stdout on success (the image IS the delivery); a FAILED post makes the
    # engine dump the report to stdout (saved by deliver:local) and exit non-zero.
    r = subprocess.run([sys.executable, ENGINE, "--post", REPORT_CHANNEL, "--quiet", "--delete"])
    sys.exit(r.returncode)
