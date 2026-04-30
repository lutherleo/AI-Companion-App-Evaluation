"""
web_verify.py
-------------
Automated verification of web-accessible companion-app sites.

For each "Likely" or UNKNOWN site in the CSV, this script:
  1. Opens the URL in a real Chromium browser (Playwright) so JS renders.
  2. Analyses the rendered DOM for chat vs. marketing-page signals.
  3. If a signup wall is detected, creates an account using a disposable
     email (1secmail API — free, no key needed).
  4. Pulls the verification email, clicks the link.
  5. Re-checks for a real chat interface post-login.
  6. Writes results back to the CSV and saves screenshots.

Usage:
    # Verify all "Likely" sites
    python web_verify.py

    # Verify specific sites by index range (1-based, matches the list)
    python web_verify.py --range 1-20

    # Re-verify even if already resolved
    python web_verify.py --force

    # Just screenshot without signup attempts
    python web_verify.py --no-signup

Dependencies:
    pip install playwright requests
    playwright install chromium
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, urljoin
from typing import Optional

# Windows UTF-8 console fix
if os.name == "nt":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

try:
    import requests
except ImportError:
    print("pip install requests", file=sys.stderr); raise SystemExit(1)
try:
    from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout
except ImportError:
    print("pip install playwright && playwright install chromium", file=sys.stderr)
    raise SystemExit(1)

UNKNOWN = "UNKNOWN - requires manual verification"
SCREENSHOTS_DIR = Path(".cache/web_verify_screenshots")
RESULTS_CACHE = Path(".cache/web_verify_results.json")

# ──────────────────────── 1secmail temp-mail API ────────────────────────
SECMAIL_API = "https://www.1secmail.com/api/v1/"
SECMAIL_DOMAINS = ["1secmail.com", "1secmail.org", "1secmail.net"]

def _gen_email() -> tuple[str, str, str]:
    """Generate a disposable email. Returns (full_email, login, domain)."""
    try:
        resp = requests.get(SECMAIL_API, params={"action": "genRandomMailbox", "count": 1}, timeout=10)
        email = resp.json()[0]
    except Exception:
        import random, string
        login = "test" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        domain = SECMAIL_DOMAINS[0]
        email = f"{login}@{domain}"
    login, domain = email.split("@")
    return email, login, domain


def _poll_inbox(login: str, domain: str, timeout: int = 60, poll_interval: int = 5) -> Optional[dict]:
    """Poll the 1secmail inbox until a message arrives or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = requests.get(SECMAIL_API, params={
                "action": "getMessages", "login": login, "domain": domain
            }, timeout=10)
            msgs = resp.json()
            if msgs:
                msg_id = msgs[0]["id"]
                detail = requests.get(SECMAIL_API, params={
                    "action": "readMessage", "login": login, "domain": domain, "id": msg_id
                }, timeout=10)
                return detail.json()
        except Exception:
            pass
        time.sleep(poll_interval)
    return None


def _extract_verify_link(msg: dict) -> Optional[str]:
    """Extract the most likely verification/confirm link from an email."""
    body = msg.get("htmlBody") or msg.get("textBody") or ""
    # Look for links with verification-like keywords
    links = re.findall(r'https?://[^\s"\'<>]+', body)
    verify_keywords = ["verify", "confirm", "activate", "validate", "token", "auth", "click"]
    for link in links:
        if any(kw in link.lower() for kw in verify_keywords):
            return link
    # Fallback: return the first non-unsubscribe link
    for link in links:
        if "unsubscribe" not in link.lower() and "mailto" not in link.lower():
            return link
    return None


# ──────────────────────── DOM analysis ────────────────────────
def analyse_rendered_page(page: Page, url: str) -> dict:
    """Analyse the fully-rendered DOM for chat/marketing signals."""
    result = {
        "web_accessible": UNKNOWN,
        "login_required": UNKNOWN,
        "login_methods": UNKNOWN,
        "languages_supported": UNKNOWN,
        "has_signup_form": False,
        "signup_attempted": False,
        "notes": "",
    }

    try:
        content = page.content().lower()
        visible_text = (page.evaluate("() => document.body?.innerText || ''") or "").lower()
    except Exception as e:
        result["notes"] = f"page error: {e}"
        return result

    # ── Chat signals (in rendered DOM) ──
    chat_signals = 0
    chat_evidence = []

    # Real chat input elements (JS-rendered)
    chat_input_count = page.evaluate("""() => {
        const inputs = document.querySelectorAll(
            'input[placeholder*="message" i], input[placeholder*="type" i], ' +
            'input[placeholder*="chat" i], input[placeholder*="say" i], ' +
            'textarea[placeholder*="message" i], textarea[placeholder*="type" i], ' +
            'textarea[placeholder*="chat" i], [contenteditable="true"]'
        );
        return inputs.length;
    }""")
    if chat_input_count > 0:
        chat_signals += 3
        chat_evidence.append(f"chat_inputs={chat_input_count}")

    # WebSocket connections (very strong signal)
    ws_count = page.evaluate("""() => {
        return (document.documentElement.innerHTML.match(/wss?:\\/\\//gi) || []).length;
    }""")
    if ws_count > 0:
        chat_signals += 2
        chat_evidence.append(f"websocket_refs={ws_count}")

    # Chat-related UI containers
    chat_container_count = page.evaluate("""() => {
        const els = document.querySelectorAll(
            '[class*="chat" i], [class*="message-list" i], [class*="conversation" i], ' +
            '[id*="chat" i], [id*="message" i], [data-testid*="chat" i]'
        );
        return els.length;
    }""")
    if chat_container_count >= 3:
        chat_signals += 2
        chat_evidence.append(f"chat_containers={chat_container_count}")

    # Text-based signals
    for phrase in ["start chatting", "send a message", "chat now", "talk now",
                   "type a message", "type your message", "open chat"]:
        if phrase in visible_text:
            chat_signals += 1
            chat_evidence.append(f'text:"{phrase}"')

    # ── Marketing-page signals ──
    marketing_signals = 0
    marketing_evidence = []
    for phrase in ["download on the app store", "get it on google play",
                   "available on ios", "download the app", "coming soon",
                   "scan the qr", "download now"]:
        if phrase in visible_text:
            marketing_signals += 1
            marketing_evidence.append(f'text:"{phrase}"')

    # App store badge images
    store_badges = page.evaluate("""() => {
        const imgs = document.querySelectorAll('img[src*="app-store" i], img[src*="google-play" i], img[src*="badge" i], a[href*="apps.apple.com"], a[href*="play.google.com"]');
        return imgs.length;
    }""")
    if store_badges > 0:
        marketing_signals += store_badges
        marketing_evidence.append(f"store_badges={store_badges}")

    # ── Decide web_accessible ──
    if chat_signals >= 3:
        result["web_accessible"] = "True"
    elif marketing_signals >= 2 and chat_signals == 0:
        result["web_accessible"] = "False"
    elif chat_signals >= 1:
        result["web_accessible"] = "Likely — manual verification needed"
    elif marketing_signals >= 1:
        result["web_accessible"] = "False"
    else:
        result["web_accessible"] = UNKNOWN

    # ── Login detection ──
    login_wall_phrases = ["sign in to continue", "log in to continue", "please sign in",
                          "login required", "create an account to", "sign up to chat",
                          "sign up to continue", "register to continue"]
    open_phrases = ["no sign up", "chat without login", "try without", "guest mode"]

    has_login_wall = any(p in visible_text for p in login_wall_phrases)
    has_open = any(p in visible_text for p in open_phrases)

    if has_login_wall and not has_open:
        result["login_required"] = "True"
    elif has_open:
        result["login_required"] = "False"

    # ── Login methods ──
    methods = []
    method_checks = {
        "Google": ['[class*="google" i]', 'button:has-text("Google")', 'a[href*="accounts.google.com"]'],
        "Apple": ['[class*="apple" i]', 'button:has-text("Apple")', 'a[href*="appleid.apple.com"]'],
        "Facebook": ['[class*="facebook" i]', 'button:has-text("Facebook")', 'a[href*="facebook.com"]'],
        "Discord": ['[class*="discord" i]', 'button:has-text("Discord")', 'a[href*="discord.com"]'],
        "Email": ['input[type="email"]', 'input[placeholder*="email" i]', 'button:has-text("Email")'],
        "Phone": ['input[type="tel"]', 'input[placeholder*="phone" i]'],
    }
    for method, selectors in method_checks.items():
        for sel in selectors:
            try:
                if page.locator(sel).count() > 0:
                    methods.append(method)
                    break
            except Exception:
                continue
    if methods:
        result["login_methods"] = ", ".join(methods)

    # ── Signup form detection ──
    signup_form = page.evaluate("""() => {
        const forms = document.querySelectorAll('form');
        for (const f of forms) {
            const text = f.innerText?.toLowerCase() || '';
            if (text.includes('sign up') || text.includes('register') ||
                text.includes('create account') || text.includes('get started')) {
                return true;
            }
        }
        const buttons = document.querySelectorAll('button, a');
        for (const b of buttons) {
            const text = b.innerText?.toLowerCase() || '';
            if (text.includes('sign up') || text.includes('register') ||
                text.includes('create account') || text.includes('get started')) {
                return true;
            }
        }
        return false;
    }""")
    result["has_signup_form"] = bool(signup_form)

    # ── Languages ──
    lang_codes = page.evaluate("""() => {
        const codes = new Set();
        document.querySelectorAll('[hreflang]').forEach(el => {
            codes.add(el.getAttribute('hreflang'));
        });
        document.querySelectorAll('select option').forEach(opt => {
            const v = opt.value || opt.textContent;
            if (/^[a-z]{2}(-[A-Z]{2})?$/.test(v.trim())) codes.add(v.trim());
        });
        return [...codes];
    }""")
    if lang_codes:
        result["languages_supported"] = ", ".join(sorted(lang_codes))

    evidence = "; ".join(chat_evidence + marketing_evidence)
    result["notes"] = evidence[:200] if evidence else "no strong signals"

    return result


# ──────────────────────── Signup attempt ────────────────────────
def attempt_signup(page: Page, url: str) -> dict:
    """Try to create an account with a temp email. Returns outcome dict."""
    email, login, domain = _gen_email()
    outcome = {"email": email, "success": False, "notes": ""}

    try:
        # Look for signup buttons/links
        signup_clicked = False
        for selector in [
            'button:has-text("Sign Up")', 'a:has-text("Sign Up")',
            'button:has-text("Register")', 'a:has-text("Register")',
            'button:has-text("Create Account")', 'a:has-text("Create Account")',
            'button:has-text("Get Started")', 'a:has-text("Get Started")',
        ]:
            try:
                loc = page.locator(selector).first
                if loc.is_visible(timeout=1000):
                    loc.click(timeout=3000)
                    signup_clicked = True
                    page.wait_for_timeout(2000)
                    break
            except Exception:
                continue

        if not signup_clicked:
            outcome["notes"] = "no signup button found"
            return outcome

        # Fill email field
        email_filled = False
        for sel in ['input[type="email"]', 'input[placeholder*="email" i]',
                    'input[name*="email" i]', 'input[autocomplete="email"]']:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=1000):
                    loc.fill(email)
                    email_filled = True
                    break
            except Exception:
                continue

        if not email_filled:
            outcome["notes"] = "no email field found after clicking signup"
            return outcome

        # Fill password if present
        for sel in ['input[type="password"]', 'input[placeholder*="password" i]',
                    'input[name*="password" i]']:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=1000):
                    loc.fill("TestPass#2026x!")
                    break
            except Exception:
                continue

        # Fill username/name if present
        for sel in ['input[placeholder*="name" i]', 'input[name*="name" i]',
                    'input[placeholder*="username" i]', 'input[name*="user" i]']:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=1000):
                    loc.fill("ResearchUser42")
                    break
            except Exception:
                continue

        # Submit the form
        submitted = False
        for sel in ['button[type="submit"]', 'button:has-text("Sign Up")',
                    'button:has-text("Register")', 'button:has-text("Create")',
                    'button:has-text("Continue")', 'button:has-text("Submit")',
                    'button:has-text("Get Started")', 'input[type="submit"]']:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=1000):
                    loc.click(timeout=3000)
                    submitted = True
                    page.wait_for_timeout(3000)
                    break
            except Exception:
                continue

        if not submitted:
            outcome["notes"] = "filled form but couldn't find submit button"
            return outcome

        # Check for CAPTCHA — if present, abort gracefully
        captcha_present = page.evaluate("""() => {
            const html = document.documentElement.innerHTML.toLowerCase();
            return html.includes('captcha') || html.includes('recaptcha') ||
                   html.includes('hcaptcha') || html.includes('turnstile') ||
                   html.includes('i-am-not-a-robot') ||
                   document.querySelector('iframe[src*="captcha"]') !== null;
        }""")
        if captcha_present:
            outcome["notes"] = "CAPTCHA detected — signup aborted"
            return outcome

        # Check for "verify your email" message
        verify_text = page.evaluate("() => document.body?.innerText || ''").lower()
        needs_verify = any(p in verify_text for p in [
            "verify your email", "check your email", "confirmation email",
            "verification email", "we sent you", "check your inbox"
        ])

        if needs_verify:
            outcome["notes"] = "email verification requested, polling inbox..."
            msg = _poll_inbox(login, domain, timeout=60)
            if msg:
                link = _extract_verify_link(msg)
                if link:
                    page.goto(link, timeout=15000, wait_until="domcontentloaded")
                    page.wait_for_timeout(3000)
                    outcome["success"] = True
                    outcome["notes"] = "account created + email verified"
                else:
                    outcome["notes"] = "got email but no verify link found"
            else:
                outcome["notes"] = "no verification email received within 60s"
        else:
            # Maybe signup succeeded without email verification
            post_text = page.evaluate("() => document.body?.innerText || ''").lower()
            if any(p in post_text for p in ["welcome", "dashboard", "chat", "profile", "get started"]):
                outcome["success"] = True
                outcome["notes"] = "signup succeeded (no email verification needed)"
            else:
                outcome["notes"] = "form submitted, unclear if signup succeeded"

    except Exception as e:
        outcome["notes"] = f"signup error: {str(e)[:100]}"

    return outcome


# ──────────────────────── Main orchestration ────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", type=Path, default=Path("companion_apps_evaluation.csv"))
    ap.add_argument("--range", type=str, default=None,
                    help="1-based index range, e.g. '1-20' or '5' (matches the printed list)")
    ap.add_argument("--force", action="store_true",
                    help="Re-verify even if web_accessible is already resolved")
    ap.add_argument("--no-signup", action="store_true",
                    help="Skip signup attempts — just analyse pages")
    ap.add_argument("--headless", action="store_true",
                    help="Run browser headlessly (no visible window)")
    args = ap.parse_args()

    with args.csv.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
        fieldnames = list(rows[0].keys())

    # Select target rows: "Likely" web_accessible with a developer website
    LIKELY = "Likely"
    targets = []
    for i, row in enumerate(rows):
        if row["app_type"] not in ("companion", "mixed"):
            continue
        wa = row.get("web_accessible", "")
        if not args.force and wa in ("True", "False"):
            continue
        if LIKELY not in wa and wa != UNKNOWN:
            continue
        url = row.get("developerWebsite", "").strip()
        if not url:
            continue
        targets.append((i, row))

    # Sort by installs descending
    targets.sort(key=lambda x: -int(x[1].get("minInstalls", 0) or 0))

    # Apply range filter
    if args.range:
        parts = args.range.split("-")
        start = int(parts[0]) - 1
        end = int(parts[-1]) if len(parts) > 1 else start + 1
        targets = targets[start:end]

    print(f"Sites to verify: {len(targets)}")
    if not targets:
        print("Nothing to do.")
        return

    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    # Load previous results cache
    results_cache = {}
    if RESULTS_CACHE.exists():
        try:
            results_cache = json.loads(RESULTS_CACHE.read_text(encoding="utf-8"))
        except Exception:
            pass

    updated = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        )

        for seq, (row_idx, row) in enumerate(targets, 1):
            url = row["developerWebsite"].strip()
            title = row["title"][:42]
            print(f"\n[{seq}/{len(targets)}] {title}")
            print(f"  URL: {url}")

            page = context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(3000)  # let JS render

                # Screenshot
                safe_name = re.sub(r'[^\w.-]', '_', row["appId"])
                ss_path = SCREENSHOTS_DIR / f"{safe_name}.png"
                page.screenshot(path=str(ss_path), full_page=False)

                # Analyse rendered page
                result = analyse_rendered_page(page, url)
                print(f"  web_accessible: {result['web_accessible']}")
                print(f"  login_required: {result['login_required']}")
                print(f"  login_methods:  {result['login_methods']}")
                print(f"  has_signup:     {result['has_signup_form']}")
                print(f"  notes:          {result['notes'][:80]}")

                # Attempt signup if: page looks like it has chat but needs login,
                # and we haven't resolved it yet
                if (not args.no_signup
                    and result["has_signup_form"]
                    and result["web_accessible"] in ("True", "Likely — manual verification needed")
                    and result["login_required"] != "False"):

                    print("  → Attempting signup with temp email...")
                    signup_result = attempt_signup(page, url)
                    result["signup_attempted"] = True
                    print(f"  → Signup: {signup_result['notes']}")

                    if signup_result["success"]:
                        # Re-analyse after login
                        page.wait_for_timeout(2000)
                        post_result = analyse_rendered_page(page, url)
                        if post_result["web_accessible"] == "True":
                            result["web_accessible"] = "True"
                            result["login_required"] = "True"
                        result["notes"] += f" | post-signup: {post_result['notes'][:60]}"

                # Update CSV row — only overwrite UNKNOWN / Likely fields
                changed = False
                for field in ("web_accessible", "login_required", "login_methods", "languages_supported"):
                    new_val = result.get(field, UNKNOWN)
                    old_val = row.get(field, UNKNOWN)
                    if new_val != UNKNOWN and (old_val == UNKNOWN or LIKELY in old_val or args.force):
                        row[field] = new_val
                        changed = True

                if changed:
                    updated += 1

                # Cache result
                results_cache[row["appId"]] = {
                    "url": url,
                    "web_accessible": result["web_accessible"],
                    "login_required": result["login_required"],
                    "login_methods": result["login_methods"],
                    "notes": result["notes"][:200],
                    "screenshot": str(ss_path),
                }

            except PWTimeout:
                print(f"  !! Page load timeout")
                row_wa = row.get("web_accessible", UNKNOWN)
                if row_wa == UNKNOWN or LIKELY in row_wa:
                    row["web_accessible"] = UNKNOWN
                results_cache[row["appId"]] = {"url": url, "notes": "timeout"}
            except Exception as e:
                print(f"  !! Error: {str(e)[:80]}")
                results_cache[row["appId"]] = {"url": url, "notes": f"error: {str(e)[:100]}"}
            finally:
                page.close()

            # Save results cache incrementally
            RESULTS_CACHE.write_text(json.dumps(results_cache, indent=2, ensure_ascii=False),
                                     encoding="utf-8")

        context.close()
        browser.close()

    # Write updated CSV
    with args.csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(rows)

    print(f"\n{'='*50}")
    print(f"Done. Updated {updated} rows in CSV.")
    print(f"Screenshots: {SCREENSHOTS_DIR.resolve()}")
    print(f"Results cache: {RESULTS_CACHE.resolve()}")


if __name__ == "__main__":
    main()
