# Provider Switch Checklist (summarize.py)

`summarize.py` is **provider-agnostic** — it summarizes with whatever model Hermes
Agent is configured to use. It reads `model.default` from `~/.hermes/config.yaml` and
resolves the live `base_url` / `api_key` / `api_mode` via
`hermes_cli.runtime_provider.resolve_runtime_provider()`. So switching providers is a
**Hermes config change, not a code change**. This file is the checklist for doing it
cleanly.

> Background: the rewrite (May 2026) replaced the old hardcoded-MiniMax script with this
> provider-agnostic one. See **Model Provider** in `SKILL.md` for the design. (There is no
> `summarize.py.minimax.bak` — the provider-agnostic script is canonical; roll a provider
> back via `hermes model`, not a script swap.)

---

## 1. Switch the model in Hermes (not in this skill)

```bash
hermes model                 # interactive picker, or:
# edit ~/.hermes/config.yaml  ->  model.default / model.provider / model.base_url
```

Make sure the new provider's credentials are set up the way Hermes expects — either an
API key in the environment / `~/.hermes/.env` (e.g. `OPENROUTER_API_KEY`, `GEMINI_API_KEY`,
`GLM_API_KEY`, `ANTHROPIC_API_KEY`, …) or an OAuth login via `hermes` if it's an OAuth
provider. `summarize.py` does not store any model credential itself.

## 2. Confirm what got resolved

Run the resolver the same way `summarize.py` does. The two values that matter are
**`api_mode`** (decides the wire format) and **`base_url`** / **`api_key`** (must be non-empty):

```bash
python3 - <<'EOF'
import sys, os
sys.path.insert(0, os.path.expanduser('~/.hermes/hermes-agent'))
from hermes_cli.config import load_config
from hermes_cli.runtime_provider import resolve_runtime_provider
mc = load_config().get('model', {})
print('model:', mc.get('default') if isinstance(mc, dict) else mc)
rt = resolve_runtime_provider()
print({k: ('<set>' if (k == 'api_key' and v) else v)
       for k, v in rt.items() if k in ('provider', 'api_mode', 'base_url', 'api_key')})
EOF
```

Expected: a non-empty `model`, a non-empty `base_url`, `api_key` shown as `<set>`
(OAuth providers also populate this with a bearer token), and `api_mode` of either
`anthropic_messages` or `chat_completions`.

- **`api_mode` is neither of those two?** `summarize.py` falls back to the
  `chat_completions` wire format. If the new endpoint isn't OpenAI-compatible, see
  *Adding a new wire format* below.
- **`base_url` / `api_key` empty?** Hermes couldn't resolve credentials — fix the
  provider setup before going further (re-login or set the `*_API_KEY` env var).

## 3. Smoke-test summarization end-to-end

Use the most recent fetch output (or run `python fetch_rss.py` first to refresh it):

```bash
cd ~/.hermes/skills/productivity/morning-brief/scripts
python summarize.py            # prints "Using Hermes model: <name> (api_mode=..., base_url=...)"
python send_brief.py --dry-run # renders the brief WITHOUT posting to Discord
```

Check the dry-run output for these failure signatures:

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Summaries are **empty / "Summary unavailable"** | reasoning model spent the whole token budget thinking before emitting visible text | raise `MAX_TOKENS` in `summarize.py` (must-read / maybe-relevant) |
| Visible text contains `<think>…</think>` | a `chat_completions` model emits inline reasoning the strip didn't catch | confirm `THINK_RE` matches the tag variant; extend if needed |
| Garbled / wrong field extracted | response shape differs from the assumed schema | check `_extract_anthropic` / `_extract_chat` against the raw JSON |
| HTTP 400 mentioning `temperature` | model rejects custom temperature | already auto-retried without it; no action unless it still 400s |
| HTTP 400 mentioning `max_tokens` vs `max_completion_tokens` | provider uses the newer param name | add the alias in `call_model`'s payload |

Only post for real once the dry-run looks right:

```bash
python summarize.py && python send_brief.py
```

## 4. Update the docs if anything material changed

- If you changed `MAX_TOKENS` or the prompt, note it in `SKILL.md` → **Model Provider**.
- The MiniMax-specific entries in `references/pitfalls-log.md` are historical; leave them as
  a record, but add a new entry there (and note it in `CHANGELOG.md`) if the new provider surprises you.

---

## Adding a new wire format

`summarize.py::call_model` currently branches on `api_mode`:

- `anthropic_messages` → `POST {base_url}/v1/messages`, `Authorization: Bearer <key>` +
  `anthropic-version: 2023-06-01`; response text = concatenation of `content[]` blocks
  where `type == "text"` (reasoning is in separate `thinking` blocks and is dropped).
- anything else → treated as `chat_completions`: `POST {base_url}/chat/completions`,
  `Authorization: Bearer <key>`; response text = `choices[0].message.content` with inline
  `<think>…</think>` stripped.

If a future provider needs a third shape (different path, headers, auth scheme, or
response schema), add a branch in `call_model` keyed on the resolved `api_mode`, plus a
matching `_extract_*` helper. Keep using only `requests` so the cron interpreter
(`/usr/bin/python3`, no openai/anthropic SDK) can still run it.

## Rollback

There is no `summarize.py.minimax.bak` (see the note at the top). To go back to a previous
provider, switch it back in Hermes (`hermes model` / `config.yaml`) — `summarize.py`
follows automatically. (Corrected 2026-09-26: this section used to say to copy that backup.)
