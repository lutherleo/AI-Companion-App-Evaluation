# Q2 Design — Automated Message Collection from Android AI Companion Apps

## 1. Approach

The PoC drives a real Android installation of the target app (PolyBuzz,
`ai.socialapps.speakmaster`, 10M+ installs — correctly classified as
`companion` in Q1) with **Appium 2.x + UiAutomator2**. A generic Python
runner (`poc_appium_runner.py`) handles the mechanics — launching, waiting,
typing, sending, polling for replies — while **per-app quirks live in a
YAML config** (`targets/*.yaml`). Adding a new companion app is a config
file, not a code change.

The runtime loop is:

1. Attach to a running Appium session using `noReset=true` (preserves the
   logged-in state captured by a one-time manual login).
2. Verify we landed in the chat view via a configurable readiness selector.
3. Snapshot the current count of bot-bubble elements (`baseline`).
4. For each message:
   a. Focus the input, type the prompt, tap send.
   b. Poll the chat view. As soon as the bot-bubble count exceeds `baseline`,
      read the newest bubble's text. Because replies stream token-by-token,
      we require the text to remain **unchanged for `response_stable_secs`**
      (default 2.5 s) before treating it as final.
   c. Record the `(prompt, response, sent_at, received_at, status)` tuple
      as one JSON line in the output file and advance `baseline`.
   d. Sleep `intermessage_cooldown_secs` before the next message.
5. Abort gracefully and mark the message as `timeout_or_paywall` if a
   configured paywall/interstitial selector becomes visible or the reply
   never stabilizes within `response_wait_secs`.

## 2. Why this is effective and scalable

| Concern | How the design addresses it |
|---|---|
| **Fidelity to production** | Drives the actual installed app; the bot sees exactly what a real user would send, so captured responses reflect the real product, filters, and personalization. |
| **Per-app variation** | All app-specific selectors, timeouts, paywall patterns, and pre-chat navigation live in YAML. The runner is one file for N apps. |
| **Streaming replies** | Stability-window heuristic (text unchanged for N seconds) handles token-streamed responses without relying on an app-specific "done" indicator. |
| **Incremental results** | JSONL with `flush()` per line — a crash or kill loses at most one pair. |
| **Resumability** | `noReset=true` + append-mode output means restarts pick up where they left off. |
| **Horizontal scale** | One Appium server ↔ one device. Spinning up N devices (physical farm or emulator shards with distinct UDIDs) runs N targets in parallel; the runner takes `--device-udid` for exactly this reason. |

## 3. Assumptions

- **Login is manual and one-time.** Account creation + login flows vary
  radically per app and frequently require CAPTCHA, SMS OTP, or third-party
  OAuth. We assume a human creates the account once, after which the app's
  stored session is reused on subsequent runs.
- **Selectors are stable between app versions for short windows.** We
  prefer `resource-id` > `accessibility_id` > XPath precisely because
  `resource-id`s break least often, but any app update may require a
  selector refresh via Appium Inspector.
- **The ToS permits this usage.** Research-purpose automation is typically
  handled via prior outreach / agreement with the vendor or via academic
  fair-use arguments; operationally the runner does nothing a user cannot
  do, but that is a legal question, not a technical one, and must be
  settled before scaling beyond PoC.
- **One character per run.** The PoC targets a single pinned character. A
  follow-up can parameterize character selection via `prechat_taps` in the
  config.

## 4. Limitations

- **No login automation.** See above — intentionally out of scope.
- **Vision-based fallback not implemented.** If an app renders its chat in a
  WebView or a custom Canvas with no accessible text, UiAutomator2 returns
  empty strings. For those apps, the right extension is OCR (Tesseract or a
  vision LLM) over periodic screenshots. The runner's `_bubble_text` helper
  is isolated so this can be swapped in without touching the main loop.
- **Paywall handling is detect-and-abort.** The PoC does not attempt to
  bypass subscription prompts (which would be wrong on both ethical and ToS
  grounds). A real production pipeline would budget for subscriptions on the
  apps under evaluation and record which apps paywall which message counts.
- **Rate-limit detection is coarse.** Soft rate limits (e.g. replies
  silently truncated after N messages) won't trigger the paywall check.
  Secondary monitoring — comparing response length distributions over time,
  or watching for canned "you've hit your limit" strings — would catch these
  and is straightforward to add.
- **No anti-bot evasion.** If an app actively fingerprints automation (e.g.
  detects Appium's `uiautomator2` server process), requests will be blocked.
  Mitigation options (rooted device + Frida, real-human-cadence delays,
  device farm rotation) exist but were deliberately left out of the PoC to
  keep the approach clean and defensible.

## 5. Multi-platform extension

### More Android apps
Add one `targets/<name>.yaml`; no code change. To pilot the full companion
subset from Q1 (~360 apps), the natural sequence is:

1. Sort candidates by install count × classification confidence.
2. For the top-N, manually capture selectors once (Appium Inspector → YAML).
3. Run the same driver across all of them on a shared device farm.
4. Aggregate the JSONL outputs keyed by `package`.

### iOS
The same architecture ports to iOS by swapping the driver. Replace
`UiAutomator2Options` with `XCUITestOptions`, change selector types to
iOS predicates / class chains, and leave the rest of the runner untouched.
The `TargetConfig` schema already accommodates this — only `Selector.by`
needs a new mapping in `to_appium()`.

### Web apps (implemented — `poc_playwright_runner.py`)
For apps with a real web chat client (identified by the `web_accessible=True`
rows after Q1 web research), **Playwright** is a drop-in alternative with
the exact same schema: `message_input`, `send_button`, `bot_bubble_selector`,
stability-window logic — all carry over. The only change is the underlying
driver. `poc_playwright_runner.py` reuses the YAML configs with a `web:`
section added:

```yaml
web:
  url: "https://app.example.com/chat"
  user_data_dir: ".browser_sessions/example"  # preserves login cookies
```

Login persistence uses Playwright's persistent browser context (analogous
to `noReset=true` in the Appium runner). A `--login-only` flag opens the
browser for one-time manual login before collection runs.

### Reverse-engineered APIs
For high-volume collection on apps with reasonably stable network traffic,
MITM-proxy capture (mitmproxy + a trusted cert on the device) can reveal the
underlying chat endpoint. This is strictly faster than UI automation but
carries higher ToS and brittleness risks. Recommended only for apps where
the UI path is blocked and the research value justifies the trade-off.

## 6. What to verify manually

The YAML configs shipped in this PoC contain **plausible default selectors
based on typical Android app conventions**, not selectors verified against
the live apps. Before a real run, an operator must:

1. Install the target app on the test device and log in.
2. Open Appium Inspector, navigate to the chat view with a chosen character.
3. For each element in the YAML (`message_input`, `send_button`,
   `bot_bubble_selector`, `paywall_selectors`, etc.), confirm or replace
   the selector with the observed value.
4. Run a dry pass with 1–2 messages and inspect `output/*.jsonl`.
5. Only then scale up to the full 10-message list.

This is intentional: hard-coding selectors against app versions I can't see
would be dishonest. The PoC's architecture is the deliverable; a 30-minute
inspection session per app turns it into a production run.
