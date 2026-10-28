# MiniMax API Quirks (Morning Brief Context)

> **Historical (noted 2026-09-26):** MiniMax is no longer the provider (currently
> deepseek-flash via `chat_completions`); `summarize.py` is provider-agnostic. See
> `provider-switch-checklist.md`.

## Extended Thinking Blocks

MiniMax models (M2.7, M2.5, etc.) emit extended thinking tokens (`<think>...</think>`) in their responses when `thinking.type` is not explicitly disabled. This causes problems:

- The think block **consumes the entire output** — the LLM places its actual summary *inside* the think block, and after stripping the block, you get an empty string.
- Affects all MiniMax models with default settings.

**Fix — always include in the API payload:**
```python
payload = {
    "model": "MiniMax-M2.7",
    "thinking": {"type": "off"},  # ← CRITICAL: suppresses think blocks
    "messages": [...],
    "max_tokens": ...,
}
```

**Why this matters:** Without `thinking: {"type": "off"}`, the `summarize.py` script will silently produce empty summaries for every article. The error is silent — MiniMax returns HTTP 200, the think block is just part of the `content` string.

## Base URL

```
https://api.minimax.io/v1/chat/completions   # Global endpoint
```

Not `api.minimax.chat` (that's an old endpoint). The correct base for OpenAI-compatible chat completions is `https://api.minimax.io/v1`.

## Available Models (2026-05)

```
MiniMax-M2.7
MiniMax-M2.7-highspeed
MiniMax-M2.5
MiniMax-M2.5-highspeed
MiniMax-M2.1
MiniMax-M2.1-highspeed
MiniMax-M2
```

Use `MiniMax-M2.7` for summarization (best quality/speed balance).

## Token Limits

- `max_tokens: 1000` for must-read (3-5 sentence summaries) — **must be ≥1000** or summaries get hard-truncated mid-sentence; MiniMax stops at the token cap regardless of sentence boundary
- `max_tokens: 2000` for maybe-relevant (2-sentence summaries)
- `temperature: 0.3` — factual summarization, keep it deterministic

> **Bug 4 / Bug 12 history**: Original values were 300/100. At 300 tokens, complete 3-5 sentence summaries were hard-truncated mid-word mid-sentence. 1000 and 2000 were settled on empirically. Never reduce must-read below 1000.

## Concurrency

MiniMax API rate limits apply. Cap concurrent calls at 5 with a semaphore. Seeing 429 errors under load → lower `CONCURRENT_LIMIT` in `summarize.py`.
