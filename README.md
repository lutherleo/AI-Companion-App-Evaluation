# AI Companion App Evaluation

Research pipeline for classifying ~492 Android (and iOS) AI apps and
collecting automated chat transcripts from companion apps.

## Project structure

```
├── build_csv.py                # Q1: classify apps → companion_apps_evaluation.csv
├── companion_apps_evaluation.csv  # Q1 output (492 Android rows + platform column)
├── web_research.py             # Q1: fetch developer sites, resolve UNKNOWN fields
├── google_play_apps_details.json  # raw input (492 Android apps)
│
├── poc_appium_runner.py        # Q2: Appium + UiAutomator2 runner (Android)
├── poc_playwright_runner.py    # Q2: Playwright runner (web-accessible apps)
├── targets/
│   ├── polybuzz.yaml           # per-app config — selectors, timeouts, paywall
│   └── replika.yaml
├── messages.txt                # 10 sample prompts
├── output/                     # JSONL results land here
│
├── DESIGN.md                   # Q2 approach writeup
└── README.md                   # this file
```

---

## Q1 — App classification pipeline

### Run (Android only, default)

```bash
python build_csv.py
```

### Run (combined Android + iOS)

```bash
python build_csv.py --android google_play_apps_details.json --ios ios_apps.json -o combined.csv
```

The script auto-detects JSON layout, normalises iOS fields to the Android
schema, and emits a single CSV with a `platform` column (`android` / `ios`).

### Current distribution (Android, 492 apps)

| Type | Count |
|------|-------|
| companion | 376 |
| general_purpose | 68 |
| mixed | 23 |
| other | 25 |

### Web research (resolve UNKNOWN fields)

For each companion/mixed app with a `developerWebsite`, fetches the page and
runs heuristics to resolve `web_accessible`, `login_required`,
`login_methods`, and `languages_supported`. Results are cached under
`.cache/web_research/`.

```bash
pip install requests
python web_research.py                  # fetch + update CSV in place
python web_research.py --dry-run        # preview URLs without fetching
python web_research.py --refresh        # ignore cache, re-fetch all
```

---

## Q2 — Automated message collection

### Android apps (Appium)

**Prerequisites:**

```bash
pip install Appium-Python-Client PyYAML
npm i -g appium && appium driver install uiautomator2
```

**Steps:**

1. Start Appium: `appium --base-path /wd/hub`
2. Install the target app, log in manually once.
3. Run:

```bash
python poc_appium_runner.py \
    --target   targets/polybuzz.yaml \
    --messages messages.txt \
    --output   output/polybuzz_run.jsonl
```

### Web-accessible apps (Playwright)

**Prerequisites:**

```bash
pip install playwright PyYAML
playwright install chromium
```

**Steps:**

1. Create a YAML config with a `web:` section (see below).
2. (One-time) Log in manually:
   ```bash
   python poc_playwright_runner.py --target targets/example_web.yaml --login-only
   ```
3. Run:
   ```bash
   python poc_playwright_runner.py \
       --target   targets/example_web.yaml \
       --messages messages.txt \
       --output   output/example_web_run.jsonl
   ```

### YAML config format (web apps)

Add a `web:` section alongside the standard selectors:

```yaml
name: "ExampleApp"
package: "com.example.app"

web:
  url: "https://app.example.com/chat"
  user_data_dir: ".browser_sessions/example"  # optional, preserves login

chat_ready_selector:
  by: "css"
  value: "[data-testid='chat-input']"
# ... rest of selectors
```

### JSONL output format

Each line is a prompt/response pair:

```json
{
  "index": 1,
  "sent_at_utc": "2026-04-16T20:14:05+00:00",
  "received_at_utc": "2026-04-16T20:14:11+00:00",
  "prompt": "Hi there, how are you today?",
  "response": "Hey! I'm doing alright — how about you?",
  "status": "ok",
  "target": "PolyBuzz",
  "package": "ai.socialapps.speakmaster"
}
```

### Selector verification

The YAML selectors in `targets/` are **plausible defaults, not verified
against live builds**. Before any real run:

1. Open Appium Inspector (Android) or browser DevTools (web).
2. Navigate to the chat view, inspect each element.
3. Update the YAML with observed `resource-id` / CSS / XPath values.
4. Dry-run with 1–2 messages, inspect the JSONL.
5. Then scale to the full 10-message list.

See `DESIGN.md` for the full approach writeup, limitations, and
multi-platform extension plan.
