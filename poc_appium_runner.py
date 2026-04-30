"""
poc_appium_runner.py
--------------------
Proof-of-concept: programmatically send a list of messages to an AI companion
character on an Android app, and capture the bot's replies with timestamps.

Design goals:
  * Configurable per-target (different apps have different selectors) — the
    runner itself stays generic, per-app quirks live in targets/*.yaml.
  * Robust response capture: polls the chat view until the newest bot bubble
    stabilizes (streaming replies take time), then dedupes against previously
    captured bubbles.
  * Resumable: writes results incrementally as JSONL so a crash loses at most
    one message pair.
  * Safe defaults: inter-message cooldown, retry on stale elements, optional
    soft-abort if the UI shifts unexpectedly (e.g. a paywall dialog appears).

Usage:
    # 1. Start Appium server:        appium --base-path /wd/hub
    # 2. Attach Android device/emu:  adb devices    (one UDID visible)
    # 3. Run:
    python poc_appium_runner.py \
        --target   targets/polybuzz.yaml \
        --messages messages.txt \
        --output   output/polybuzz_run.jsonl

Dependencies:
    pip install Appium-Python-Client PyYAML

Tested against Appium 2.x + UiAutomator2 driver on Android 13+.
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
from appium import webdriver
from appium.options.android import UiAutomator2Options
from appium.webdriver.common.appiumby import AppiumBy
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.support.ui import WebDriverWait


# ---------- Target config schema ----------
@dataclass
class Selector:
    """Locator for an on-screen UI element. `by` accepts 'id', 'xpath',
    'accessibility_id', or 'uiautomator' (UIAutomator2 selector string)."""
    by: str
    value: str

    def to_appium(self) -> tuple:
        mapping = {
            "id": AppiumBy.ID,
            "xpath": AppiumBy.XPATH,
            "accessibility_id": AppiumBy.ACCESSIBILITY_ID,
            "uiautomator": AppiumBy.ANDROID_UIAUTOMATOR,
            "class": AppiumBy.CLASS_NAME,
        }
        if self.by not in mapping:
            raise ValueError(f"Unknown selector type: {self.by}")
        return mapping[self.by], self.value


@dataclass
class TargetConfig:
    """Everything the runner needs to know about one companion app."""
    name: str
    package: str
    activity: Optional[str]                   # launcher activity, optional if appWaitActivity set
    chat_ready_selector: Selector            # element that confirms we landed in chat view
    message_input: Selector                  # the EditText to type into
    send_button: Selector                    # button to submit the message
    bot_bubble_selector: Selector            # selects ALL bot message bubbles
    bot_bubble_text_attr: str = "text"       # how to extract text from the bubble (or "xpath:./descendant::...")
    response_wait_secs: int = 45             # hard cap on waiting for a reply
    response_stable_secs: float = 2.5        # how long the newest bubble text must be unchanged
    poll_interval_secs: float = 0.8
    intermessage_cooldown_secs: float = 3.0  # throttle to avoid rate limits / bans
    paywall_selectors: list = field(default_factory=list)   # list of Selector; if any appears -> abort run
    # Optional pre-chat navigation (e.g. tap "Continue Chat" on a specific character)
    prechat_taps: list = field(default_factory=list)        # list of Selector, tapped in order

    @classmethod
    def from_yaml(cls, path: Path) -> "TargetConfig":
        data = yaml.safe_load(path.read_text())

        def mk(sel):
            return Selector(**sel)

        return cls(
            name=data["name"],
            package=data["package"],
            activity=data.get("activity"),
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


# ---------- Driver orchestration ----------
class AppiumSession:
    """Thin wrapper that owns the Appium driver for the run."""
    def __init__(self, target: TargetConfig, appium_url: str, device_udid: Optional[str]):
        opts = UiAutomator2Options()
        opts.platform_name = "Android"
        opts.automation_name = "UiAutomator2"
        opts.app_package = target.package
        if target.activity:
            opts.app_activity = target.activity
        opts.no_reset = True          # keep the app's logged-in state between runs
        opts.new_command_timeout = 300
        if device_udid:
            opts.udid = device_udid
        self.driver = webdriver.Remote(appium_url, options=opts)
        self.target = target

    def wait_for(self, sel: Selector, timeout: int = 15):
        by, val = sel.to_appium()
        return WebDriverWait(self.driver, timeout).until(
            lambda d: d.find_element(by, val)
        )

    def find_all(self, sel: Selector) -> list:
        by, val = sel.to_appium()
        try:
            return self.driver.find_elements(by, val)
        except NoSuchElementException:
            return []

    def is_paywall_visible(self) -> bool:
        for sel in self.target.paywall_selectors:
            by, val = sel.to_appium()
            try:
                if self.driver.find_elements(by, val):
                    return True
            except WebDriverException:
                continue
        return False

    def close(self):
        try:
            self.driver.quit()
        except Exception:
            pass


# ---------- Response capture ----------
def _bubble_text(el, attr: str) -> str:
    """Extract visible text from a bot chat bubble."""
    if attr == "text":
        return (el.text or "").strip()
    if attr.startswith("attr:"):
        return (el.get_attribute(attr.split(":", 1)[1]) or "").strip()
    if attr.startswith("xpath:"):
        # return concatenated text of matched descendants
        try:
            kids = el.find_elements(AppiumBy.XPATH, attr.split(":", 1)[1])
            return " ".join((k.text or "").strip() for k in kids if k.text).strip()
        except WebDriverException:
            return ""
    return (el.text or "").strip()


def capture_new_response(session: AppiumSession, previously_seen: int) -> Optional[dict]:
    """
    Wait for a new bot bubble to appear (count increases beyond `previously_seen`)
    and its text to stabilize. Returns {"text": str, "bubble_index": int} or None
    on timeout / paywall.
    """
    target = session.target
    deadline = time.time() + target.response_wait_secs

    last_text = None
    last_change = time.time()

    while time.time() < deadline:
        if session.is_paywall_visible():
            print("  !! paywall/interstitial detected — aborting this message")
            return None

        try:
            bubbles = session.find_all(target.bot_bubble_selector)
        except StaleElementReferenceException:
            time.sleep(target.poll_interval_secs)
            continue

        if len(bubbles) <= previously_seen:
            time.sleep(target.poll_interval_secs)
            continue

        try:
            newest = bubbles[-1]
            text = _bubble_text(newest, target.bot_bubble_text_attr)
        except (StaleElementReferenceException, WebDriverException):
            time.sleep(target.poll_interval_secs)
            continue

        if not text:
            time.sleep(target.poll_interval_secs)
            continue

        if text != last_text:
            last_text = text
            last_change = time.time()
        else:
            if time.time() - last_change >= target.response_stable_secs:
                return {"text": text, "bubble_index": len(bubbles) - 1}

        time.sleep(target.poll_interval_secs)

    # timed out
    return None


# ---------- Main loop ----------
def run(target_path: Path, messages_path: Path, output_path: Path,
        appium_url: str, device_udid: Optional[str]) -> int:
    target = TargetConfig.from_yaml(target_path)
    messages = [ln.strip() for ln in messages_path.read_text().splitlines() if ln.strip()]
    if not messages:
        print("No messages in input file", file=sys.stderr)
        return 2

    print(f"Target: {target.name} ({target.package})")
    print(f"Messages to send: {len(messages)}")
    print(f"Output: {output_path}")

    session = AppiumSession(target, appium_url, device_udid)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        # 1. Land in the chat view (user should have logged in previously;
        # no_reset=True preserves that state).
        session.wait_for(target.chat_ready_selector, timeout=30)

        # 2. Pre-chat taps (e.g. select a specific character card).
        for sel in target.prechat_taps:
            session.wait_for(sel, timeout=15).click()
            time.sleep(1.5)

        # 3. Baseline bubble count so we only capture NEW replies.
        baseline = len(session.find_all(target.bot_bubble_selector))

        with output_path.open("a", encoding="utf-8") as out:
            for i, msg in enumerate(messages, start=1):
                print(f"[{i}/{len(messages)}] sending: {msg!r}")

                # Type into the input and tap send.
                input_el = session.wait_for(target.message_input, timeout=15)
                input_el.click()
                input_el.clear()
                input_el.send_keys(msg)

                send_el = session.wait_for(target.send_button, timeout=10)
                send_el.click()

                sent_at = datetime.now(timezone.utc).isoformat()

                # Wait for response to come in and stabilize.
                reply = capture_new_response(session, previously_seen=baseline)
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
                }
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
                out.flush()
                print(f"    -> {record['status']}: {(record['response'] or '')[:90]}")

                if reply:
                    baseline = reply["bubble_index"] + 1  # advance past this bubble

                time.sleep(target.intermessage_cooldown_secs)

    finally:
        session.close()

    print("done")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", required=True, type=Path,
                    help="YAML config describing the app under test")
    ap.add_argument("--messages", required=True, type=Path,
                    help="Plain text file, one message per line")
    ap.add_argument("--output", required=True, type=Path,
                    help="JSONL file to append captured prompt/response pairs to")
    ap.add_argument("--appium-url", default="http://127.0.0.1:4723/wd/hub")
    ap.add_argument("--device-udid", default=None,
                    help="Specific device UDID (omit if only one attached)")
    args = ap.parse_args()
    sys.exit(run(args.target, args.messages, args.output, args.appium_url, args.device_udid))


if __name__ == "__main__":
    main()
