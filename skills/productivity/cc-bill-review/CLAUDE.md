# CLAUDE.md — cc-bill-review (attached to every command run in this dir — keep it short)

- Agent use: follow the contract at the top of `SKILL.md` — one script per request, `--post`
  (the scripts post to the current thread themselves), reply with their one-line stdout exactly
  (import exit 3/4: see `SKILL.md`), never paste merchant names or query/edit
  `scripts/cc_review.db` by hand.
- Editing code: read `references/developer-guide.md` first; run `python3 scripts/selftest.py`
  after any change (must be all-pass); history and the reason behind every rule: `CHANGELOG.md`.
- The daily reminder cron (`7b68c1ef7afe`) runs `~/.hermes/scripts/cc_payment_reminder.py` under
  the gateway venv; never pause/resume/create it from the skill.
