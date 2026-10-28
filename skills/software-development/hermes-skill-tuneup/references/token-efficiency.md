# Pass 3 — Token Efficiency

## What costs tokens
- **`SKILL.md` body** loads into context **every time the skill triggers** — this is the hot path. Shrinking it is the main win.
- **`description` frontmatter** is **always-on** (it sits in context for skill discovery across all sessions). Keep it one tight sentence; trim, but never to the point of hurting routing. For an interactive skill whose job is to be applied *every time* (formatting, delivery rules), the description is the only thing that gets it loaded — make it a directive ("X does NOT render Y — load this before ANY reply that …"), and don't trust non-standard frontmatter trigger keys (`trigger_conditions:` is read by no gateway code — discord-table-format was loaded in 14 of 200 sessions). Where a platform-wide rule is needed, `platform_hints.<platform>.append` in `config.yaml` is the supported lever.
- Files under `references/` and `CHANGELOG.md` load **only when the agent opens them** — so content moved there is effectively free until needed.
- **Runtime:** prefer a no-LLM script for the common path (it costs ~0 tokens). For ops that read data, read the **raw** compact file, not a verbose computed dump.

## Runtime: a scheduled skill's biggest lever is the execution MODE
For a **cron/scheduled** skill, the dominant runtime token cost isn't the `SKILL.md` body or a per-call prompt — it's **whether each tick runs as an LLM agent or as a `no_agent` script**. An agent-mode cron loads the *entire* `SKILL.md` (+ any `CLAUDE.md` + tool schemas) into a fresh context **every tick** and does the work via the model; converting the job to `no_agent` (the scheduler executes the script directly, and the script self-posts so `deliver: local`) drops that to **~0 tokens/tick**. This is almost always the single largest win available for a scheduled skill, and it's usually already a one-line job-config change — verify the mode before hunting for prompt/SKILL.md savings.
- **Measure it for free via cron-output size.** An agent-mode tick's output file contains the loaded skill + the model's response (tens of KB); a `no_agent` tick's is a short script log. (email-gmail-triage: ticks went **36 KB agent-mode → 567 B script-log** at the migration — that ratio *is* the per-run token delta, no instrumentation needed. `ls -S ~/.hermes/cron/output/<job_id>/` shows the before/after at a glance.)
- **When the skill is already `no_agent`, the honest finding is "runtime ≈ 0 — the migration already won."** Say so plainly (the sibling of *single-shot has nothing to optimize*) instead of inventing micro-savings. The real work then is **hardening against a regression back to agent mode**: a `no_agent` job that still carries `skill` / `skills` / `prompt` fields will reload the whole skill the instant `no_agent` gets flipped off (a UI edit, a `jobs.json` restore, a "tweak the triage" request) — clear those fields and/or monitor that the flag stays set. (email-gmail-triage: the triage job still listed `skill: google-workspace` + a prompt while the digest jobs were clean — a latent ~36 KB/tick relapse.)
- **A `no_agent` script that defers to a human conversation has an indirect per-deferral token cost.** The script itself is free, but every "I'm unsure → ask in Discord" posts a question whose human reply wakes the *interactive* agent (full context load). So keeping the defer/ask rate low is itself a token lever, even though it never shows up in the cron output.

## Runtime: keep large intermediates out of the orchestrator's context
A cost center separate from the `SKILL.md` body and from per-call LLM prompts: a value the skill **produces in one step and consumes in another** round-trips through the *orchestrating agent's* context whenever the flow is split across CLI invocations — the agent pays for it on the way out (step 1's stdout) **and** the way back in (step 2's argv), twice every run, and these are often dense, poorly-tokenizing blobs.
- **Collapse a multi-step CLI flow into one end-to-end script.** (google-route-map: the documented flow was Step 1 `computeRoutes` → emit the 300–3,000+ char encoded polyline into context → Step 2 `download_static_map.py <polyline> …` takes it straight back as argv. A single `route_map.py "<origin>" "<dest>"` fetches the route, builds the URL, and downloads the image **in one process**, so the polyline/URL never leave Python; the agent passes two short addresses and gets back a one-line `Saved …`.) Bigger and more variable than the `SKILL.md` trim for any skill whose flow hands a large value between steps. Keep the in-process helpers importable so the end-to-end and lower-level entry points share one code path (DRY) without re-spawning an interpreter.
- **Don't echo large values to stdout.** The agent pays for a script's stdout too — it's read back into context. Print a *summary* (a byte count, the saved path, a distance/duration), never the full URL / polyline / base64 / row dump. (google-route-map already printed `URL length: N bytes`, not the URL — keep that discipline: the size, never the blob.)

## Runtime: per-call LLM prompt tokens (skills that call a model)
Separate from the always-on `SKILL.md` budget above: if the skill calls an LLM **per item** at runtime (e.g. a summarizer), the prompt is the hot path. Measure on real data first (`chars/4` ≈ tokens), then:
- **Drop fields the model doesn't use.** A summarizer doesn't need the article URL — it's pure dead weight in every prompt (URLs average ~80 chars). Send only what the task needs.
- **Cap input context per task type.** A 2-sentence summary needs far less source text than a 5-sentence one — cap the lower-tier prompt tighter than the headline one.
- **Trim the prompt slice, not the stored field.** The stored content often also feeds scoring / keyword matching / paywall guards — trimming *it* has side effects; only trim the copy sent to the model.
- **`max_tokens` does NOT bill.** You pay for tokens *generated*, not the cap; lowering it saves nothing on output you don't produce. Its only token role is bounding a runaway (e.g. a reasoning model whose `thinking:disabled` got dropped and reasons to the cap). Don't "save tokens" by lowering it.
- **Watch for no-op slices.** `content[:3000]` when `content` was already capped at 2000 upstream ships the full text every call while looking like a limit.
- **Be honest about absolute impact.** A daily cron summarizing ~8 items is only ~5–6K tokens/run; a ~30% prompt trim saves ~1.5K/run — worth doing for hygiene, but say plainly it's not a cost lever; the always-on `SKILL.md` body usually dwarfs it.

## The refactor: progressive disclosure
Measure first: `scripts/skill_audit.py <skill_dir>` prints SKILL.md tokens per H2 section and flags reference-only chunks.

**Move OUT of SKILL.md** (load on demand):
- Changelog / version history → `CHANGELOG.md`
- Recovery / canonical data dumps → `references/<topic>.md`
- Verbose mechanics, long worked examples, QA checklists → `references/<topic>.md`

**Keep IN SKILL.md** (always needed to act):
- `When to Use` triggers
- The operations themselves (condensed)
- A **References table** that tells the agent which file to open *and when*
- A few golden, non-duplicated pitfalls

Then dedupe: pitfalls that merely restate the Operations section, and oversized examples (7 rows where 2 illustrate the format).

## Relocate, don't delete — and verify the move landed
**Run this pass only after the terminal validation gate passes clean (true code freeze — the gate is re-entrant, so a fix it finds re-runs it).** Relocating a block that *describes code* (a delivery snippet, a flow/contract, a constant, an example invocation) while a later pass — or a gate-found fix — can still change that code strands a **stale copy in a load-on-demand reference nobody re-reads** — and the per-change doc-sync's "grep for the old value" can miss it, because the value was just cut from `SKILL.md` so a SKILL.md-only grep finds nothing. (This is exactly the doc-miss this skill's own re-sequencing fixed: docs-last + a whole-dir grep, see `safe-edit-workflow.md` §5.) Move behavior/code docs only once the code they describe is final. Before cutting a block, classify it: **duplicated** elsewhere (the same content already lives in a reference) → replace with a one-line pointer; **unique** (this is the only copy — an archival log, a benchmark table, a worked example) → **append it to the natural reference** with a one-line "relocated from SKILL.md (date)" note, *then* cut it from the body. Never delete unique content outright. After the move, **verify nothing was lost**: grep the target reference for a distinctive string from the relocated block, and grep the new SKILL.md to confirm every load-bearing imperative still survives. (A larger token-usage tune-up took SKILL.md ~13,455 → ~2,617 tokens / −80% this way — the cut content went to `references/` and the pricing files, none of it deleted.)

## Split by phase, and de-dupe the editing doc
Don't pour everything into one `references/` file — **split reference content by *when* it's needed**, so each part loads only in its phase and `SKILL.md` keeps only use-time essentials:
- `SETUP.md` — one-time install / cron creation; never needed again once deployed.
- `TROUBLESHOOTING.md` — runbooks read only when a run breaks (diagnostic recipes, "did it actually run?", catch-up).
- `CLAUDE.md` — editing internals (flow, regexes, gotchas), loaded only when an agent works in the dir.

Track **two** budgets, not one: the **per-trigger** cost (`SKILL.md` alone — the number that matters most) and the **edit-time** cost (`SKILL.md` + `CLAUDE.md`, which *both* load when editing). If `CLAUDE.md` restates the flow / constants / gotchas already in `SKILL.md`, an editing agent pays for them twice — make `CLAUDE.md` the single maintenance source and have `SKILL.md` link to it. After splitting, run **one coverage grep across the whole doc set** for every load-bearing fact (job ids, sheet/channel ids, runbook keywords, the regexes) to prove the move lost nothing. (foodpanda: `SKILL.md` 3,940 → **640** tokens / −84% by moving setup → `SETUP.md`, the empty-date runbook → `TROUBLESHOOTING.md`, and de-duping `CLAUDE.md`; edit-time 6,125 → 1,880.)

## Targets & caution
- The source tune-up took a SKILL.md from ~4,180 → ~1,200 tokens (~70% off the hot path) with no capability loss.
- A skill with **no `references/` dir** can still slim `SKILL.md` by cutting verbose code blocks — the script *is* the source of truth, so replace inline code with a one-line invocation + a pointer to `scripts/`, never a re-paste. (google-route-map: SKILL.md 5.2K → 2.8K / −45% by dropping two ~15-line Python snippets while keeping **every** gotcha.)
- **Don't over-trim operational fidelity.** If a write op needs a procedure, keep a one-line "read `references/…` before writing" pointer inline and put the steps in the reference — the agent must still be told the rule exists.
- **Risk:** the agent can skip reading a reference. Mitigate by keeping critical *imperatives* ("never edit the live file in place", "run the script, don't hand-render") inline; only the *details* go to the reference.
- **Bug history tables are the #1 token sink — and projections consistently undershoot.** Skills that have gone through multiple bug-fix cycles accumulate "Bugs Found & Fixed" tables that dominate the token budget (email-gmail-triage: 5,521 tok in bug tables alone, 45% of SKILL.md). The projected savings estimate of 67–72% landed at **85%** in practice because bug tables are even larger than a section-level token audit suggests — they span multiple H2 sections and each row is dense. Move them to `CHANGELOG.md` early; the operational rules they established are already captured in the "Critical Implementation Notes" or equivalent golden-rules section.
- **CLAUDE.md file maps go stale after relocations.** When you move files from SKILL.md to `references/` or `CHANGELOG.md`, the `CLAUDE.md` file map (which lists every file in the skill dir) is **not** automatically updated. Stale file maps confuse editing agents. Include the CLAUDE.md file map in your doc-sync sweep — add the new `CHANGELOG.md` and any new `references/` entries. (email-gmail-triage: the file map was missing `CHANGELOG.md`, `references/architecture.md`, and `references/category-rules.md` after the relocation.)
- **Verify the auditor can find the skill dir before running it.** The auditor assumes `~/.hermes/skills/<category>/<skill-name>/` but some skills live at `~/.hermes/skills/<skill-name>/` (flat, no category subdirectory). A "not a skill dir" error means the path is wrong — check with `search_files` first, then pass the correct path.
- **The auditor's DUPLICATION heuristic false-positives on legitimate repetition — treat it as advisory.** `skill_audit.py` flags (a) verbatim-repeated lines that are *intentional* (a Quick Start showing three runnable `python3 -c` invocations that each legitimately repeat the `sys.path.insert` / `db.DB_PATH=` preamble), and (b) `duplicate list numbers {'1':3,'2':3,'3':3} — likely a broken/merged numbered list` when the doc simply has several **distinct** numbered lists (wrapper steps, filter steps, guardrails) that each start at 1. Neither is a defect; DRYing copy-pasteable examples hurts usability. Confirm by eye and don't churn intentional example duplication for ~100 tok. (The anti-pattern scan is trustworthy; the duplication/numbered-list heuristic is a line-counter, not a Markdown-list parser — a *heuristic-approximates-a-parser* case in the auditor itself. spotify-playlist-refresh 2.10.0.)
- **Don't duplicate changelogs.** A skill with both `CHANGELOG.md` (root) and `references/changelog.md` will drift — the 1.9.4 entry was written to one but missed the other. Keep exactly one changelog file (the root `CHANGELOG.md`); if a `references/changelog.md` exists from an older layout, replace it with a one-line pointer. (hermes-skill-tuneup: consolidated 2026-06-09 after the writeback miss.)
- **After consolidating/renaming files, grep all references to the old path.** The SKILL.md References table still pointed at `references/changelog.md` after the merge — same staleness class as the CLAUDE.md file-map issue above. Run `grep -rn 'old-filename' <skill_dir>` after any file move/rename/consolidation.
- **A writeback entry needs a `## <version>` header, and there is exactly ONE top-level `#` title.** When you append a CHANGELOG writeback, give it a `## <version> — <date> — <what>` header and confirm the header count keeps pace with the version bumps — `grep -c '^## ' CHANGELOG.md` should rise by one per bump. (hermes-skill-tuneup self-audit: a `## 1.9.14` writeback shipped with **no** version header — orphaned prose invisible to a version grep — and a stray `# Changelog` H1 sat mid-file. The skill that polices doc drift had drifted its own changelog.) Tell: `grep -nE '^# |^## ' CHANGELOG.md` shows a version gap or a `# ` title anywhere but the very top.

## Don't undo your own work
A changelog/notes section you add for documentation is itself hot-path tokens — put it in `CHANGELOG.md`, not `SKILL.md`.

## Runtime prompt trimming is a CODE change — sequence it accordingly (morning-brief 2026-09-15)

This file's *Runtime: per-call LLM prompt tokens* section is the one part of the docs-last pass
that edits **code**, and `SKILL.md`'s sequence says the docs pass runs *after* the terminal
gate has frozen the code. Taken literally that ships a prompt change the gate never reviewed.
**Resolve it this way: do the prompt trim during the CODE passes** (it changes what the model
receives, so it is behavioural and belongs under the gate), and keep only the *documentation*
of the prompt in the docs-last pass. Same rule for any other "token" item that touches a
script rather than a `.md`.

Two concrete tells from the same skill:
- **A no-op slice that looks like a limit.** `content[:3000]` where `content` was already
  capped at 2000 chars upstream — it ships the full text every call while reading as a bound.
  Grep the upstream cap before trusting a slice.
- **A field the model never uses.** The article URL was interpolated into every summarization
  prompt (~144 chars each) although the model is asked for prose and the link is rendered from
  the stored field. Drop it.
And be honest about scale: for a daily 8-item cron this is ~3.8 K input tokens/run, so a 30%
trim is hygiene, not a cost lever — say so rather than presenting it as a saving.

## `CLAUDE.md` is a per-RUN cost when the agent runs scripts from the skill dir (sg-commute 2026-09-29)

Hermes attaches a directory's `CLAUDE.md` ("Subdirectory context discovered") whenever the agent runs a command in that dir — so for a skill whose interactive entry point is `scripts/<x>.py`, `CLAUDE.md` loads on **every** use, not just when editing. sg-commute's 24.5 KB `CLAUDE.md` (cron/route maintenance notes) rode along on every parking query, and its stale "~90 s" runtime made agents set 300–540 s timeouts. Keep such a `CLAUDE.md` to a few lines (what runs where, the one safe command, edit rules) and relocate the long form to `references/developer-guide.md`. Check `sessions` in `state.db` for "Subdirectory context discovered" to see which skills pay this.
