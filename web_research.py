"""
web_research.py
---------------
For each companion / mixed app that has a developerWebsite, fetch the site
and attempt to resolve four currently-UNKNOWN evaluation fields:

    web_accessible          — is there a real in-browser chat, not just marketing?
    login_required          — does the web chat require authentication?
    login_methods           — which OAuth / email / phone options are visible?
    languages_supported     — any explicit language-selector or hreflang tags?

Design principles:
  * Cache raw HTML to .cache/web_research/<domain>.html so reruns are cheap.
  * Never fabricate values — mark as UNKNOWN when signals are ambiguous.
  * A marketing homepage is NOT "web_accessible" per the task spec.

Usage:
    python web_research.py                          # default CSV path
    python web_research.py --csv my_apps.csv        # custom CSV
    python web_research.py --refresh                # ignore cache
    python web_research.py --dry-run                # show what would be fetched

Dependencies:  requests  (pip install requests)
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import os
# Windows console may choke on Unicode; force UTF-8 output.
if os.name == "nt":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

try:
    import requests
except ImportError:
    print("ERROR: 'requests' is required.  pip install requests", file=sys.stderr)
    raise SystemExit(1)

UNKNOWN = "UNKNOWN - requires manual verification"
CACHE_DIR = Path(".cache/web_research")
FETCH_TIMEOUT = 15          # seconds per request
POLITENESS_DELAY = 1.5      # seconds between fetches

# ---------- Heuristic signal lists ----------
# Signals that a page offers a real in-browser chat, not just marketing.
CHAT_SIGNALS = [
    "start chatting", "chat now", "talk now", "send a message",
    "open chat", "launch chat", "try it free", "start conversation",
    "app.replika.com", "/chat", "/app", "web.character.ai",
    "webapp", "web app",
]
CHAT_ELEMENT_PATTERNS = [
    r'<input[^>]*(?:type=["\']text["\']|placeholder=["\'].*(?:message|type|say|chat))',
    r'<textarea[^>]*(?:placeholder=["\'].*(?:message|type|say|chat))',
    r'contenteditable=["\']true["\']',
    r'id=["\'](?:chat|message|conversation)',
    r'class=["\'][^"\']*(?:chat-input|message-input|chat-box|chatbox)',
    r'wss?://',      # WebSocket — strong signal for live chat
]

# Marketing-only signals (presence alongside NO chat signals → not web-accessible).
MARKETING_SIGNALS = [
    "download on the app store", "get it on google play",
    "available on ios and android", "download the app",
    "scan the qr code", "coming soon",
]

# Login method detection
LOGIN_METHOD_PATTERNS = {
    "Google": [r'accounts\.google\.com', r'sign.?in.?with.?google', r'google-signin', r'btn[_-]google'],
    "Apple": [r'appleid\.apple\.com', r'sign.?in.?with.?apple', r'apple-auth'],
    "Facebook": [r'facebook\.com/v\d', r'sign.?in.?with.?facebook', r'fb-login', r'facebook-login'],
    "Email": [r'sign.?up.?with.?email', r'email.?and.?password', r'enter.?your.?email', r'type=["\']email["\']'],
    "Phone": [r'phone.?number', r'sms.?verification', r'enter.?your.?phone'],
    "Discord": [r'discord\.com/api', r'sign.?in.?with.?discord'],
    "Twitter/X": [r'api\.twitter\.com', r'sign.?in.?with.?(?:twitter|x\b)'],
}

# Language detection
LANG_PATTERNS = [
    # hreflang tags
    r'hreflang=["\']([a-z]{2}(?:-[A-Z]{2})?)["\']',
    # language selector options
    r'<option[^>]*value=["\']([a-z]{2}(?:-[A-Z]{2})?)["\'][^>]*>',
]


# ---------- Fetching + caching ----------
def _cache_key(url: str) -> str:
    """Stable filename for a URL."""
    parsed = urlparse(url)
    domain = parsed.netloc or parsed.path
    h = hashlib.md5(url.encode()).hexdigest()[:8]
    safe = re.sub(r'[^\w.-]', '_', domain)
    return f"{safe}_{h}"


def fetch_page(url: str, *, refresh: bool = False) -> str | None:
    """Fetch a URL, returning the HTML body or None on failure.
    Uses a file cache under CACHE_DIR."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"{_cache_key(url)}.html"

    if not refresh and cache_file.exists():
        return cache_file.read_text(encoding="utf-8", errors="replace")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=FETCH_TIMEOUT,
                            allow_redirects=True, verify=True)
        resp.raise_for_status()
        html = resp.text
        cache_file.write_text(html, encoding="utf-8")
        return html
    except Exception as e:
        # Cache the failure so we don't re-fetch on every run
        cache_file.write_text(f"<!-- FETCH_ERROR: {e} -->", encoding="utf-8")
        return None


# ---------- Analysis ----------
def analyse_page(url: str, html: str) -> dict:
    """Run heuristics on fetched HTML, return field updates."""
    if not html or "FETCH_ERROR" in html[:200]:
        return {
            "web_accessible": UNKNOWN,
            "login_required": UNKNOWN,
            "login_methods": UNKNOWN,
            "languages_supported": UNKNOWN,
        }

    html_lower = html.lower()

    # --- web_accessible ---
    chat_score = sum(1 for s in CHAT_SIGNALS if s in html_lower)
    chat_element_score = sum(1 for p in CHAT_ELEMENT_PATTERNS if re.search(p, html_lower))
    marketing_score = sum(1 for s in MARKETING_SIGNALS if s in html_lower)

    if chat_element_score >= 2 or (chat_score >= 2 and chat_element_score >= 1):
        web_accessible = "True"
    elif marketing_score >= 2 and chat_score == 0 and chat_element_score == 0:
        web_accessible = "False"
    elif chat_score >= 1 or chat_element_score >= 1:
        web_accessible = "Likely — manual verification needed"
    else:
        web_accessible = UNKNOWN

    # --- login_required ---
    login_wall_signals = [
        "sign in to continue", "log in to continue", "create an account",
        "sign up to chat", "login required", "you must log in",
        "please sign in", "register to continue",
    ]
    open_access_signals = [
        "no sign up required", "chat without login", "no account needed",
        "try without signing in", "guest",
    ]
    has_login_wall = any(s in html_lower for s in login_wall_signals)
    has_open = any(s in html_lower for s in open_access_signals)

    if has_login_wall and not has_open:
        login_required = "True"
    elif has_open:
        login_required = "False"
    else:
        login_required = UNKNOWN

    # --- login_methods ---
    detected_methods = []
    for method, patterns in LOGIN_METHOD_PATTERNS.items():
        if any(re.search(p, html, re.IGNORECASE) for p in patterns):
            detected_methods.append(method)
    login_methods = ", ".join(detected_methods) if detected_methods else UNKNOWN

    # --- languages_supported ---
    lang_codes = set()
    for pattern in LANG_PATTERNS:
        lang_codes.update(re.findall(pattern, html, re.IGNORECASE))
    if lang_codes:
        languages = ", ".join(sorted(lang_codes))
    else:
        languages = UNKNOWN

    return {
        "web_accessible": web_accessible,
        "login_required": login_required,
        "login_methods": login_methods,
        "languages_supported": languages,
    }


# ---------- Main ----------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", type=Path, default=Path("companion_apps_evaluation.csv"),
                    help="CSV to read and update in-place")
    ap.add_argument("--refresh", action="store_true",
                    help="Ignore cached HTML and re-fetch everything")
    ap.add_argument("--dry-run", action="store_true",
                    help="List URLs that would be fetched, don't actually fetch")
    args = ap.parse_args()

    with args.csv.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
        fieldnames = list(rows[0].keys())

    # Filter: companion or mixed, with a non-empty developer website
    targets = [
        (i, row) for i, row in enumerate(rows)
        if row["app_type"] in ("companion", "mixed")
        and row.get("developerWebsite", "").strip()
    ]

    print(f"Loaded {len(rows)} rows, {len(targets)} companion/mixed with developerWebsite")

    if args.dry_run:
        for _, row in targets:
            print(f"  {row['title'][:40]:<42} {row['developerWebsite']}")
        return

    updated = 0
    errors = 0
    cached = 0
    for seq, (idx, row) in enumerate(targets, 1):
        url = row["developerWebsite"].strip()
        title = row["title"][:40]

        # Check cache
        cache_file = CACHE_DIR / f"{_cache_key(url)}.html"
        is_cached = cache_file.exists() and not args.refresh

        print(f"[{seq}/{len(targets)}] {title:<42} {'(cached)' if is_cached else url[:60]}")

        html = fetch_page(url, refresh=args.refresh)

        if is_cached:
            cached += 1
        elif html is None:
            errors += 1
            print(f"    !! fetch failed")

        results = analyse_page(url, html)

        # Update row — only overwrite UNKNOWN fields, preserve manual edits
        changed = False
        for field in ("web_accessible", "login_required", "login_methods", "languages_supported"):
            if field in results and results[field] != UNKNOWN:
                if row.get(field, UNKNOWN) == UNKNOWN:
                    row[field] = results[field]
                    changed = True

        if changed:
            updated += 1
            for k in ("web_accessible", "login_required", "login_methods"):
                if results[k] != UNKNOWN:
                    print(f"    {k}: {results[k]}")

        if not is_cached:
            time.sleep(POLITENESS_DELAY)

    # Write back
    with args.csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(rows)

    print(f"\nDone. Updated {updated} rows, {errors} fetch errors, {cached} cache hits.")
    print(f"Cache dir: {CACHE_DIR.resolve()}")


if __name__ == "__main__":
    main()
