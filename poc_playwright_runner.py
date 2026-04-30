"""
poc_playwright_runner.py
------------------------
Proof-of-concept: programmatically send messages to a web-accessible AI
companion app and capture responses. This is the web counterpart to
poc_appium_runner.py, designed per DESIGN.md §5.

Reuses the same YAML config schema (targets/*.yaml) with an additional
top-level `web` section for web-specific settings:

    web:
      url: "https://app.example.com/chat"
      # Optional: path to a Chromium user-data dir with an active session
      user_data_dir: ""

When `user_data_dir` is set, Playwright launches a persistent context so
that cookies / localStorage from a prior manual login are preserved
(equivalent to `noReset=true` in the Appium runner).

Design goals:
  * Same YAML schema as the Appium runner — selectors are reusable.
  * Stability-window heuristic for streaming replies (identical logic).
  * Incremental JSONL output, same format as the Appium runner.
  * Paywall detection — abort if a paywall element appears.

Usage:
    # 1. Install Playwright + browsers
    pip install playwright
    playwright install chromium

    # 2. (One-time) Log in manually via the persistent browser:
    python poc_playwright_runner.py --target targets/example_web.yaml --login-only

    # 3. Run the message collection:
    python poc_playwright_runner.py \
        --target   targets/example_web.yaml \
        --messages messages.txt \
        --output   output/example_web_run.jsonl

Dependencies:
    pip install playwright PyYAML
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

try:
    from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout
except ImportError:
    print("ERROR: 'playwright' is required.  pip install playwright && playwright install chromium",
          file=sys.stderr)
    raise SystemExit(1)


# ---------- Config (extends the Appium YAML schema) ----------
@dataclass
class Selector:
    """CSS or XPath locator. `by` accepts 'css', 'xpath', 'text', 'role',
    or the Appium-style 'id' (mapped to CSS [resource-id=...] or #id)."""
    by: str
    value: str

    def to_playwright(self) -> str:
        """Return a Playwright-compatible locator string."""
        if self.by == "css":
            return self.value
        if self.by == "xpath":
            return f"xpath={self.value}"
        if self.by == "text":
            return f"text={self.value}"
        if self.by == "id":
            # Try CSS id selector — works for web; Appium resource-ids
            # with colons need quoting.
            if ":" in self.value:
                return f'[resource-id="{self.value}"]'
            return f"#{self.value}"
        if self.by == "role":
            return f"role={self.value}"
        return self.value


@dataclass
class WebConfig:
    """Web-specific settings from the `web:` section of the YAML."""
    url: str
    user_data_dir: str = ""


@dataclass
class TargetConfig:
    """Everything the runner needs to know about one companion app/site."""
    name: str
    package: str
    web: WebConfig
    chat_ready_selector: Selector
    message_input: Selector
    send_button: Selector
    bot_bubble_selector: Selector
    bot_bubble_text_attr: str = "text"
    response_wait_secs: int = 45
    response_stable_secs: float = 2.5
    poll_interval_secs: float = 0.8
    intermessage_cooldown_secs: float = 3.0
    paywall_selectors: list = field(default_factory=list)
    prechat_taps: list = field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: Path) -> "TargetConfig":
        data = yaml.safe_load(path.read_text())

        def mk(sel):
            return Selector(**sel)

        web_data = data.get("web", {})
        if not web_data.get("url"):
            raise ValueError(
                f"{path}: missing 'web.url'. The Playwright runner requires a "
                f"'web:' section with at least a 'url' field."
            )

        return cls(
            name=data["name"],
            package=data.get("package", ""),
            web=WebConfig(
                url=web_data["url"],
                user_data_dir=web_data.get("user_data_dir", ""),
            ),
            chat_ready_selector=mk(data["chat_ready_selector"]),
            message_input=mk(data["message_input"]),
            send_button=mk(data["send_button"]),
            bot_bubble_selector=mk(data["bot_bubble_selector"]),
            bot_bubble_text_attr=data.get("bot_bubble_text_attr", "text"),
            response_wait_secs=data.get("response_wait_secs", 45),
            response_stable_secs=data.get("response_stable_secs", 2.5),
            poll_interval_secs=data.get("poll_interval_secs", 0.8),
            intermessage_cooldown_secs=data.get("intermessage_cooldown_secs", 3.0),
            paywall_selectors=[mk(s) for s in data.get("paywall_selectors", [])],
            prechat_taps=[mk(s) for s in data.get("prechat_taps", [])],
        )


# ---------- Page helpers ----------
def _count_bubbles(page: Page, sel: Selector) -> int:
    loc = sel.to_playwright()
    return page.locator(loc).count()


def _bubble_text(page: Page, sel: Selector, index: int, attr: str) -> str:
    """Extract visible text from the nth bot bubble."""
    loc = page.locator(sel.to_playwright()).nth(index)
    if attr == "text":
        return (loc.inner_text() or "").strip()
    if attr.startswith("attr:"):
        return (loc.get_attribute(attr.split(":", 1)[1]) or "").strip()
    return (loc.inner_text() or "").strip()


def _is_paywall_visible(page: Page, selectors: list) -> bool:
    for sel in selectors:
        try:
            if page.locator(sel.to_playwright()).count() > 0:
                return True
        except Exception:
            continue
    return False


# ---------- Response capture ----------
def capture_response(page: Page, target: TargetConfig, baseline: int) -> Optional[dict]:
    """Wait for a new bot bubble and its text to stabilise."""
    deadline = time.time() + target.response_wait_secs
    last_text = None
    last_change = time.time()

    while time.time() < deadline:
        if _is_paywall_visible(page, target.paywall_selectors):
            print("  !! paywall/interstitial detected — aborting this message")
            return None

        try:
            count = _count_bubbles(page, target.bot_bubble_selector)
        except Exception:
            time.sleep(target.poll_interval_secs)
            continue

        if count <= baseline:
            time.sleep(target.poll_interval_secs)
            continue

        try:
            text = _bubble_text(page, target.bot_bubble_selector,
                                count - 1, target.bot_bubble_text_attr)
        except Exception:
            time.sleep(target.poll_interval_secs)
            continue

        if not text:
            time.sleep(target.poll_interval_secs)
            continue

        if text != last_text:
            last_text = text
            last_change = time.time()
        elif time.time() - last_change >= target.response_stable_secs:
            return {"text": text, "bubble_index": count - 1}

        time.sleep(target.poll_interval_secs)

    return None


# ---------- Main loop ----------
def run(target_path: Path, messages_path: Path, output_path: Path,
        headless: bool, login_only: bool) -> int:
    target = TargetConfig.from_yaml(target_path)
    messages = [ln.strip() for ln in messages_path.read_text().splitlines() if ln.strip()]

    if not messages and not login_only:
        print("No messages in input file", file=sys.stderr)
        return 2

    print(f"Target:   {target.name} ({target.web.url})")
    if not login_only:
        print(f"Messages: {len(messages)}")
        print(f"Output:   {output_path}")

    with sync_playwright() as pw:
        # Use persistent context if user_data_dir is set (preserves login state)
        launch_kwargs = {"headless": headless}
        if target.web.user_data_dir:
            ctx = pw.chromium.launch_persistent_context(
                target.web.user_data_dir, **launch_kwargs
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
        else:
            browser = pw.chromium.launch(**launch_kwargs)
            ctx = browser.new_context()
            page = ctx.new_page()

        try:
            page.goto(target.web.url, wait_until="domcontentloaded", timeout=30000)

            if login_only:
                print("\n--- LOGIN MODE ---")
                print("Log in manually in the browser window.")
                print("Press Enter here when done...")
                input()
                print("Session saved. You can now run the message collection.")
                return 0

            # Wait for chat readiness
            page.locator(target.chat_ready_selector.to_playwright()).wait_for(
                state="visible", timeout=30000
            )

            # Pre-chat taps
            for sel in target.prechat_taps:
                page.locator(sel.to_playwright()).click()
                page.wait_for_timeout(1500)

            # Baseline bubble count
            baseline = _count_bubbles(page, target.bot_bubble_selector)

            output_path.parent.mkdir(parents=True, exist_ok=True)
            with output_path.open("a", encoding="utf-8") as out:
                for i, msg in enumerate(messages, 1):
                    print(f"[{i}/{len(messages)}] sending: {msg!r}")

                    # Type and send
                    input_loc = page.locator(target.message_input.to_playwright())
                    input_loc.click()
                    input_loc.fill(msg)
                    page.locator(target.send_button.to_playwright()).click()

                    sent_at = datetime.now(timezone.utc).isoformat()

                    # Capture response
                    reply = capture_response(page, target, baseline)
                    received_at = datetime.now(timezone.utc).isoformat()

                    record = {
                        "index": i,
                        "sent_at_utc": sent_at,
                        "received_at_utc": received_at,
                        "prompt": msg,
                        "response": reply["text"] if reply else None,
                        "status": "ok" if reply else "timeout_or_paywall",
                        "target": target.name,
                        "package": target.package,
                        "web_url": target.web.url,
                    }
                    out.write(json.dumps(record, ensure_ascii=False) + "\n")
                    out.flush()
                    print(f"    -> {record['status']}: {(record['response'] or '')[:90]}")

                    if reply:
                        baseline = reply["bubble_index"] + 1

                    time.sleep(target.intermessage_cooldown_secs)

        finally:
            ctx.close()

    print("done")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", required=True, type=Path,
                    help="YAML config with a 'web:' section")
    ap.add_argument("--messages", type=Path, default=Path("messages.txt"),
                    help="Plain text file, one message per line")
    ap.add_argument("--output", type=Path, default=Path("output/web_run.jsonl"),
                    help="JSONL output file")
    ap.add_argument("--headless", action="store_true",
                    help="Run browser in headless mode (no visible window)")
    ap.add_argument("--login-only", action="store_true",
                    help="Open browser for manual login, then save session and exit")
    args = ap.parse_args()
    sys.exit(run(args.target, args.messages, args.output, args.headless, args.login_only))


if __name__ == "__main__":
    main()
