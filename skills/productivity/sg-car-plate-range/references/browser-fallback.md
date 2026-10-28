# Fallback: Browser Approach (CDP, requires manual reCAPTCHA)

> Relocated from `SKILL.md` (2026-06-11 tune-up) to keep the hot-path SKILL.md
> small. This is the **non-preferred** path — use the ScraperAPI script
> (`~/.hermes/scripts/lta_car_prefix.py`) for normal prefix checks. This fallback
> is only for the **VRL app page** (`vrl.lta.gov.sg`) when you need the full
> *numeric range* (e.g. "SPH918X to SPH1631R"), which the OneMotoring info page
> does not expose.

**Browserbase / headless browsers FAIL here** — the LTA VRL site has an invisible
reCAPTCHA that detects and blocks automated sessions. `browser_navigate` directly
will not work. The only reliable path is the user's own Chrome via Chrome
DevTools Protocol (CDP), with the user solving the reCAPTCHA manually.

## Steps

### 1. Start Chromium on the Pi with remote debugging
Run on the Pi host (not in a subshell):
```bash
pkill chromium 2>/dev/null; chromium-browser --remote-debugging-port=9222 --no-sandbox --disable-gpu
```
Verify: `curl -s http://127.0.0.1:9222/json/version` → JSON with `"Browser": "Chrome/..."`.

### 2. Connect via CDP
Create a new tab: `http://127.0.0.1:9222/json/new?wnd`, then `browser_navigate` to the LTA page.

### 3. The user solves reCAPTCHA once
The user's own Chrome (with `--remote-debugging-port=9222`) carries the reCAPTCHA
token from manual solving; the CDP session inherits it.

### 4. Interact with the form — ⚠️ Angular checkbox requires label click, not just input click
The checkbox is an Angular component. Clicking the `<input>` programmatically does
NOT trigger Angular's change detection — the DOM box looks checked but Angular's
FormControl stays unaware, so Next never enables. Working approaches in order:

a) **Click the `LabelText` wrapper** (e.g. ref `e6` — the row containing the
   checkbox AND the "I have read and agree..." text). Verify with `browser_console`:
   ```javascript
   document.querySelector('input[type="checkbox"]').checked  // should be true
   ```
b) **Dispatch a proper MouseEvent** if the label click doesn't work:
   ```javascript
   const label = document.querySelector('lib-checkbox')?.shadowRoot?.querySelector('label') ||
                 document.querySelector('[class*="checkbox"] label');
   if (label) label.click();
   ```
c) **Click the span/div adjacent to the checkbox.**

What does NOT work: calling `.click()` on the raw `<input>` — Angular listens on the
host element/label, not the input. After clicking: wait 500ms, verify `checked=true`,
then click Next.

### 5. Extract Range — results are a STATE change, not a navigation
After Next, results appear on the same route (`enq-highest-sys-no-proxy`) — the form
panel collapses and the range text displays in its place. If the form resets to its
initial state, the checkbox wasn't really checked — retry step 4.
Expected result text:
```
Vehicle type: Car
Details
Range of new vehicle numbers assigned by LTA as of {dd MMM yyyy} / {hh:mm:ss} :
SPH918X to SPH1631R (both vehicle no. inclusive)
```
⚠️ If you close the tab or navigate away, the results are gone and you must redo the
full flow — there is no URL/API that preserves the results page state.

### 6. Check a target plate — prefix-aware comparison
Plate ranges are prefix-specific. `SPH918X to SPH1631R` means only plates starting
with `SPH` in that numeric range; `SPR8008` is NOT in range (different prefix `SPR`≠`SPH`,
and `8008 > 1631`). Logic: extract prefix (letters before numbers) → extract numeric
portion → in range only if same prefix AND `minNum ≤ plateNum ≤ maxNum`.

## Pitfalls (browser/CDP path)
- **Multiple Chromium processes on the same port = wrong browser.** Any prior Chromium
  started with `--remote-debugging-port=9222` claims the port; CDP may connect to the
  OLDER process. Always `pkill chromium` before the user starts their browser.
- **Browserbase stealth mode still gets blocked** — the LTA reCAPTCHA is aggressive
  against any headless/unusual profile.
- **Angular checkbox may STILL fail via CDP even with the label wrapper.** If
  `browser_click` on `e6`/`e10` doesn't yield `checked=true`, the user must click
  manually in their own browser. For recurring use there's no headless workaround —
  manual clicking or a CAPTCHA-solving service is required.
- **`http://127.0.0.1:9222/json/new?wnd` creates a new tab** — the first
  `browser_navigate` after this goes to that new tab.
- **Token expiration** — reCAPTCHA tokens expire; recurring use needs a solver service
  or manual solving each session.
- **`curl http://127.0.0.1:9222/json` shows ALL tabs from ALL Chromium processes** —
  filter by URL to find the right one.
- **`pkill chromium` in background mode kills the foreground process** — use
  `pkill -f "chromium-browser"` in a separate terminal call before starting in background.
- **Chromium output not captured in background mode** — verify it started in a separate
  terminal: `curl -s http://127.0.0.1:9222/json/version`.
- **Headless always fails on LTA** — even with `--disable-blink-features=AutomationControlled`
  and a proper UA. Use headed mode or manual clicking.
- **Results don't persist** — no URL, cookie, or API preserves the state.

## CDP connection workflow (summary)
1. `pkill chromium` — kill ALL existing Chromium first.
2. User runs `chromium-browser --remote-debugging-port=9222`.
3. User opens the LTA page, solves reCAPTCHA, clicks Next, confirms results.
4. `curl http://127.0.0.1:9222/json` — list tabs, find the LTA URL.
5. `browser_navigate` to the LTA URL — connects to the correct tab.
6. Extract via `browser_snapshot` or `browser_console`.
