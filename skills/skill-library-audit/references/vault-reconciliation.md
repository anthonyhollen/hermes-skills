# Vault reconciliation — documented skills vs the live library

Task class: "check my skills against the vault", "did you miss any skills", "is the vault's
skill list stale". Reconciles the vault's skill records with the live machine and fixes what is
machine-verifiable. Vault root: `~/.hermes/memories/ObsidianHam`; per-skill notes and the
curated index live in `Hermes Agent/Skills/` (`Skills MOC.md`).

## Step 0 — run the vault audit engine first

    python3 ~/.hermes/scripts/vault_audit.py          # detect only — safe, no edits, no posts
    python3 ~/.hermes/scripts/vault_audit.py --fix    # apply auto-fixable drift (notes backed up)

It catches stale `(vX.Y.Z)` note versions, MOC grouping, cron schedule/paused markers, and
orphaned cron output dirs — plus review-class findings it will NEVER auto-edit: an active job
missing from the Routines table, a documented skill whose install path is gone, dangling
wikilinks. Reconcile review items yourself (that is the intended LLM role in the loop) from
structured ground truth: cron rows from `~/.hermes/cron/jobs.json` (when adding a row to the
Routines table, also bump the header's "**N active + M paused**" count); skill facts from the
skill dirs. Re-run plain until "✓ Vault in sync". The weekly Sun 10:30 cron runs `--fix`
itself, so deterministic drift self-heals; only review-class items need an agent. Full check
registry: the `vault-audit` skill → `references/checks.md`.

## Step 1 — three-way reconciliation (vault ↔ disk ↔ usage)

Records: vault notes (`Hermes Agent/Skills/*.md`, ignore `*.bak-*` and the MOC itself); on-disk
skill dirs (`~/.hermes/skills/**/SKILL.md` excluding `.archive/`, plus the `.archive/` set
separately); usage (the `state.db` loads query in SKILL.md §1).

1. Parse the MOC sections for entry titles (`[[…]]`) and dedupe — entry glosses contain links
   too, so a naive regex over a section double-counts.
2. Resolve title → skill dir: `title.lower().replace(" ", "-")`, with a manual overrides map
   for divergent names (`{"SG Economy Barometer": "sg-econ-barometer"}`). Report unresolved
   titles; never fuzzy-guess silently.
3. Classify every loaded skill key by disk status: active / archived / phantom (old or
   malformed load names — mention as trivia; they slightly inflate "distinct skills").
4. Report both gap directions:
   - documented skills with **zero recorded loads** — usually normal: cron-only `no_agent`
     jobs, SSH-run tune-ups, or reference notes; explain, don't report as broken;
   - heavily-used skills **absent from the vault** — the vault documents a curated subset by
     design; propose candidates for approval, do not bulk-document the library.
5. Verify the MOC's "Archived" names against `skills/.archive/` in both directions.

## Step 2 — adding a skill to the vault (manual, two parts)

The per-skill note is the source of truth; the MOC mirrors it. The audit's `moc` check only
**re-groups existing bullets** — it never adds an entry for a note that has none — so both
halves are hand-written:

1. Note: `Hermes Agent/Skills/<Title Case Name>.md` — the filename IS the wikilink target.
   Mirror a sibling note's shape: `# Title`; a bold origin line; skill path + version; 2–4
   short sections (how it runs, gotchas); `## Related` wikilinks. Format the audit parses:
   - **Origin marker** — `built by Ham` / `author: ham` / `user-tuned` → "Built by Ham"
     section; `origin: bundled` / `bundled (` / `origin: hermes agent` → "Bundled" section.
   - **Version** — the backticked `~/.hermes/skills/<path>` token followed by `(vX.Y.Z)`;
     keep that adjacency exact, and the version equal to the skill's frontmatter.
2. MOC: one bullet under the matching section — `- [[Title]] - short gloss`. Sections must
   contain bullets + blank lines ONLY; anything else downgrades future auto-regrouping to
   review.
3. Back the MOC up first as `Skills MOC.md.bak-manual-<YYYYMMDD_HHMM>` — take the stamp from
   the runtime clock, never hand-written.
4. Re-run the audit → "✓ Vault in sync".

## Pitfalls

- **Classify paths by their FIRST segment relative to the root:**
  `os.path.relpath(path, root).split(os.sep)[0]`. Scanning all absolute segments for a leading
  dot misclassifies everything — the `.hermes` component matches and every path lands in
  "hidden".
- **`glob.glob('**/SKILL.md')` never descends into hidden directories** — `.archive/` is
  invisible to it; use `os.walk` when hidden dirs matter.
- **Zero loads ≠ broken** — for cron-only / SSH-maintained skills it is expected; state why
  instead of implying neglect.
- **Never auto-edit the subjective side** — what a note claims about behavior/preferences has
  no machine ground truth; flag it for the user.
