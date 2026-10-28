# Memory Cleanup — Cross-Reference MEMORY.md Against the Vault

## When to do this
- User asks "clean up memory", "audit memory", "what's redundant in memory"
- User notices memory is bloated or has stale entries
- After a significant vault update that might have made memory entries redundant

## Workflow

### 1. Take Inventory
There is no `memory(action='read')`; read the entries from the system prompt's memory section (injected every turn).

List each entry's content and what it covers.

### 2. Cross-Reference Each Entry Against the Vault
For each memory entry, check the relevant vault note(s):

| Entry topic | Vault note to check |
|---|---|
| Channel IDs, routing rules | `Hermes Agent/Operations.md` (Discord routing section) |
| Cron job triggers, schedules | `Hermes Agent/Routines.md` |
| Model config, profiles | `Hermes Agent/Operations.md` (Models/profiles section) |
| API keys, endpoint quirks | `Hermes Agent/API Reference.md` |
| Upgrade/backup procedures | `Hermes Agent/Operations.md` + `Hermes Agent/Decisions.md` |
| User preferences, people | `People/Anthony (Ham).md` |
| Output formatting rules | `Hermes Agent/Conventions/Output Formats.md` |

### 3. Classify Each Entry

| Classification | Action |
|---|---|
| **Fully duplicated** — same facts verbatim or near-verbatim in vault | Remove from memory |
| **Partially duplicated** — some facts in vault, some unique to memory | Slim to only the unique facts, then keep |
| **Unique & durable** — not in vault, should be | Move to vault (add to appropriate note), then remove from memory |
| **Unique & operational** — too niche/detailed for vault (code-level bug fix, transient workaround) | Keep in memory, slim if possible |

### 4. Backfill the Vault First
Before removing anything from memory, ensure the vault has the content:
- Patch relevant vault notes to add missing facts
- For cron jobs: add row to `Routines.md` table
- For operations/config: add section to `Operations.md`
- For API quirks: add to `API Reference.md`

### 5. Clean Up Memory
Use `memory(action='remove')` with a unique substring from each entry to delete.
Use `memory(action='replace')` to slim entries down.

### 6. Fix Stale Vault References
After removing from memory, check if any vault note referenced the memory entry (e.g. "This matches the entry in MEMORY.md"). Patch those references.

## Key Principles

- **Vault is canonical, memory is cache.** Preference: vault > memory for durable facts.
- **Verify before remove.** Always read the vault note to confirm the fact is actually there before removing the memory entry.
- **Char budget is tight (2,200).** Every entry should earn its place. Redundant entries waste budget that could hold unique operational knowledge.
- **Trigger phrases stay in memory.** "Say X → run cron Y" phrases must be in the prompt to work, and the vault is not
  in the prompt — keep the memory entry even when Routines.md also documents the phrase.
  *(Correction 2026-10-02: this used to say the memory copy could be removed when the vault had it. The 2026-06-15
  cleanup removed the "morning commute" trigger on that advice ("Already in Routines.md"); the next morning's
  "morning commute" was run by hand (script + curl) instead of via the cron job, and Ham asked for the entry back.)*
