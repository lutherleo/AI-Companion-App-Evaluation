"""
Q1 pipeline: load app store records (Android and/or iOS), classify app_type,
and extract the additional fields requested by the task. Strategy (A):
heuristics + JSON only; unknowns clearly marked.

Usage:
    # Android only (default — backward-compatible)
    python build_csv.py

    # Explicit source(s)
    python build_csv.py --android google_play_apps_details.json
    python build_csv.py --ios ios_apps_details.json
    python build_csv.py --android android.json --ios ios.json -o combined.csv
"""
import argparse
import json
import csv
import re
from pathlib import Path

UNKNOWN = "UNKNOWN - requires manual verification"

# ---------- Classification keyword banks ----------
# Strong signals that the app markets itself as a companion / relational /
# roleplay / emotional-engagement product.
COMPANION_TERMS = [
    # romantic / intimate
    "girlfriend", "boyfriend", "waifu", "husbando", "virtual lover",
    "ai lover", "ai partner", "ai wife", "ai husband", "crush",
    "romance", "romantic", "flirt", "seduce", "dating sim", "ai date",
    "dreamgf", "dreambf", "soulmate", "dating space", "ai dating",
    "virtual dating",
    # roleplay / character fiction
    "roleplay", "role-play", "role play", "character chat", "ai character",
    "chat with characters", "anime chat", "fantasy chat", "visual novel",
    "fanfic", "fanfiction",
    # emotional / companionship framing
    "ai companion", "ai friend", "virtual friend", "emotional support",
    "talk to ai friend", "lonely", "mental companion",
    # explicit / NSFW leaning companion
    "nsfw", "uncensored chat", "adult ai chat", "ai sexting",
    # brand names — well-known companion platforms whose marketing copy
    # does not reliably contain the generic terms above
    "talkie",           # Talkie: Creative AI Community (50M+ installs)
    "chai",             # Chai: Chat AI Platform (10M+ installs)
    "mystic messenger", # Mystic Messenger — iconic otome / companion game
    "candy ai",         # Candy AI companion platform
    "yana",             # Yana: emotional-support AI companion
    "bimobimo",         # BIMOBIMO character chat app
    "pixelheart",       # PixelHeart AI girlfriend
    # near-brand compound phrases
    "ai girl",          # e.g. "Easy AI Girl" — distinct from generic "girl"
    "bestfriend",       # companion framing (e.g. ira)
]

# Signals that the app is a generic assistant / productivity LLM wrapper.
GENERAL_PURPOSE_TERMS = [
    "chatgpt", "gpt-4", "gpt 4", "gpt-3", "gpt 3", "gpt-5", "gpt 5",
    "ai assistant", "chat assistant", "personal assistant",
    "ask ai", "ai chatbot assistant",
    "productivity", "write emails", "summarize", "research assistant",
    "coding assistant", "homework assistant", "ai search",
    "ai helper", "smart assistant", "ai tutor", "virtual assistant",
    "gemini ai", "claude ai", "grok ai", "perplexity", "copilot",
    "deepseek", "bard ai",
]

# Signals that the app is task-specific (NOT companion, NOT general LLM).
TASK_SPECIFIC_TERMS = [
    # study / homework
    "homework help", "solve math", "math solver", "study ai", "exam prep",
    "essay writer", "essay generator", "quiz generator",
    # image / video / art
    "ai photo", "photo editor", "photo enhancer", "ai art generator",
    "image generator", "ai image", "ai wallpaper", "ai avatar",
    "face swap", "ai video", "video generator", "ai filter",
    "headshot", "portrait generator", "cartoon yourself",
    # voice / music
    "voice changer", "ai voice", "ai music", "song generator",
    "ai cover", "ai singer",
    # language / translation
    "translator", "translation", "language learning",
    # resume / biz tools
    "resume builder", "cv builder", "cover letter", "interview prep",
    "business plan", "ai legal", "ai doctor", "ai lawyer",
    # fitness / diet / horoscope
    "ai fitness", "workout", "diet plan", "calorie", "horoscope",
    "astrology", "palm reader", "dream interpret",
    # scanning / OCR / document
    "pdf chat", "chat with pdf", "document scanner", "ocr",
    # shopping / utility
    "shopping assistant", "qr code",
    # input-method / launcher / kids utilities
    "keyboard", "launcher", "for kids", "tutor for kids",
    "learning games", "kids learning",
]


def score_text(text: str, terms: list) -> int:
    """Count distinct keyword matches (case-insensitive, word-ish)."""
    t = text.lower()
    hits = 0
    for term in terms:
        if term in t:
            hits += 1
    return hits


def classify_app_type(app: dict) -> tuple:
    """Return (app_type, rationale)."""
    title = app.get("title", "") or ""
    summary = app.get("summary", "") or ""
    desc = app.get("description", "") or ""
    genre = app.get("genre", "") or ""
    # Weight the more-curated signals (title, summary) higher than description.
    weighted_blob = " ".join([title] * 3 + [summary] * 3 + [desc, genre])

    comp = score_text(weighted_blob, COMPANION_TERMS)
    gen = score_text(weighted_blob, GENERAL_PURPOSE_TERMS)
    task = score_text(weighted_blob, TASK_SPECIFIC_TERMS)

    # Decision rules (ordered by precedence):
    # 1. If task-specific dominates and companion/general are weak -> other.
    # 2. If both companion and general have meaningful signal -> mixed.
    # 3. Strong companion signal -> companion.
    # 4. Strong general signal -> general_purpose.
    # 5. Fallback -> other.
    rationale_bits = [f"comp={comp}", f"gen={gen}", f"task={task}"]

    if task >= 3 and comp < 2 and gen < 2:
        return "other", "task-specific dominant: " + ",".join(rationale_bits)

    if comp >= 2 and gen >= 2:
        return "mixed", "both companion & general signals: " + ",".join(rationale_bits)

    if comp >= 2:
        return "companion", "companion signals dominant: " + ",".join(rationale_bits)

    if gen >= 2:
        return "general_purpose", "general-purpose signals dominant: " + ",".join(rationale_bits)

    # weaker tie-breakers
    # Allow one incidental task signal alongside the companion signal —
    # this is needed for brand-name apps (e.g. Talkie has task=1 from
    # "creative" in the description, but is canonically a companion app).
    if comp == 1 and gen == 0 and task <= 1:
        return "companion", "weak companion signal only: " + ",".join(rationale_bits)
    if gen == 1 and comp == 0 and task == 0:
        return "general_purpose", "weak general signal only: " + ",".join(rationale_bits)
    if task >= 1 and comp == 0 and gen == 0:
        return "other", "weak task-specific signal only: " + ",".join(rationale_bits)

    return "other", "no dominant signal; defaulting to other: " + ",".join(rationale_bits)


# ---------- Platform normalisation ----------
# Both platforms are normalised into a common dict with these keys so the
# classifier and field-extraction helpers see a uniform schema.  Android
# fields map 1:1; iOS fields are translated from the app-store-scraper /
# Apple Search API naming conventions.

def _normalise_android(app: dict) -> dict:
    """Pass-through — Android JSON is already in the expected schema."""
    app["_platform"] = "android"
    return app


def _normalise_ios(app: dict) -> dict:
    """Map iOS (app-store-scraper / iTunes Search API) fields to the
    Android-style schema used by the rest of the pipeline."""
    return {
        # identity
        "appId": app.get("bundleId") or app.get("appId") or str(app.get("id", "")),
        "title": app.get("trackName") or app.get("title", ""),
        "summary": app.get("description", "")[:200],   # iOS has no short summary
        "description": app.get("description", ""),
        "developer": app.get("sellerName") or app.get("developer", ""),
        "developerEmail": app.get("developerEmail", ""),
        "developerWebsite": app.get("sellerUrl") or app.get("developerWebsite", ""),
        "privacyPolicy": app.get("privacyPolicyUrl") or app.get("privacyPolicy", ""),
        # taxonomy
        "genre": app.get("primaryGenreName") or app.get("genre", ""),
        "genreId": app.get("primaryGenreId") or app.get("genreId", ""),
        "contentRating": app.get("contentAdvisoryRating") or app.get("contentRating", ""),
        "contentRatingDescription": app.get("contentRatingDescription", ""),
        # pricing
        "free": app.get("free", app.get("price", 1) == 0),
        "price": app.get("price", ""),
        "priceText": app.get("formattedPrice") or app.get("priceText", ""),
        "currency": app.get("currency", ""),
        "offersIAP": app.get("offersIAP", True if app.get("iaps") else None),
        "IAPRange": app.get("IAPRange", ""),
        # popularity
        "installs": "",                         # iOS doesn't expose install counts
        "minInstalls": "",
        "score": app.get("averageUserRating") or app.get("score", ""),
        "ratings": app.get("userRatingCount") or app.get("ratings", ""),
        "reviews": app.get("reviews", ""),
        # release
        "released": app.get("releaseDate") or app.get("released", ""),
        "version": app.get("version", ""),
        "androidVersion": "",                   # not applicable
        "adSupported": "",
        "url": app.get("trackViewUrl") or app.get("url", ""),
        # internal
        "_platform": "ios",
    }


def load_source(path: Path, platform: str) -> list:
    """Load a JSON file and return a list of normalised app dicts."""
    with path.open(encoding="utf-8") as f:
        data = json.load(f)

    # Accept both {"results": [...]} and bare [...] layouts.
    if isinstance(data, list):
        apps = data
    elif isinstance(data, dict):
        # Try common wrapper keys
        for key in ("results", "apps", "data"):
            if key in data and isinstance(data[key], list):
                apps = data[key]
                break
        else:
            raise ValueError(f"Cannot find app list in {path}; expected a top-level list or a dict with a 'results'/'apps'/'data' key")
    else:
        raise ValueError(f"Unexpected top-level type in {path}")

    norm = _normalise_android if platform == "android" else _normalise_ios
    return [norm(a) for a in apps]


# ---------- Field extraction ----------
def extract_subscription_cost(app: dict) -> str:
    iap = app.get("IAPRange")
    if not iap:
        if app.get("offersIAP") is False:
            return "No in-app purchases (likely free)"
        return UNKNOWN
    platform = app.get("_platform", "android")
    store = "Play" if platform == "android" else "App Store"
    return f"{iap} (in-app purchase range; monthly tier not specified in {store} metadata)"


def extract_age_verification(app: dict) -> tuple:
    """Infer (required, method) from store content rating."""
    rating = (app.get("contentRating") or "").strip()
    if not rating:
        return UNKNOWN, UNKNOWN
    rating_lower = rating.lower()
    # Ratings that effectively impose age-gating
    if any(k in rating_lower for k in ["mature 17", "adults only 18", "17+", "18+"]):
        store = "Google Play" if app.get("_platform") == "android" else "App Store"
        return (
            "True",
            f"Self-declaration via {store} content rating ({rating}); "
            "in-app method not verified",
        )
    if "teen" in rating_lower or "everyone" in rating_lower or "4+" in rating_lower or "9+" in rating_lower or "12+" in rating_lower:
        return "False", f"No age gate implied by rating ({rating}); in-app not verified"
    return UNKNOWN, UNKNOWN


def best_web_url(app: dict) -> str:
    url = app.get("developerWebsite")
    if url and url.strip():
        return url.strip()
    return ""


def extract_subscription_hints(app: dict, app_type: str) -> dict:
    desc = (app.get("description") or "").lower()
    summary = (app.get("summary") or "").lower()
    blob = desc + " " + summary
    offers_iap = app.get("offersIAP")

    paywall_phrases = [
        "free messages", "limited messages", "daily messages", "message limit",
        "daily limit", "unlock unlimited", "upgrade to premium",
        "subscribe to continue", "vip members", "premium members",
        "limited free", "free trial then",
    ]
    unlimited_free_phrases = [
        "unlimited free", "chat unlimited for free", "completely free",
    ]

    if any(p in blob for p in paywall_phrases):
        sub_req_long = "True"
    elif any(p in blob for p in unlimited_free_phrases):
        sub_req_long = "False"
    elif offers_iap is False:
        sub_req_long = "False"
    else:
        sub_req_long = UNKNOWN

    if offers_iap is False:
        all_free = "True"
    elif any(p in blob for p in [
        "premium features", "subscribe to unlock", "vip features",
        "pro members", "premium members", "upgrade to premium",
    ]):
        all_free = "False"
    else:
        all_free = UNKNOWN

    feature_snippets = []
    feature_keywords = {
        "unlimited messages": "unlimited messaging",
        "unlimited chat": "unlimited messaging",
        "faster response": "faster responses",
        "priority": "priority access",
        "ad-free": "ad-free experience",
        "no ads": "ad-free experience",
        "premium character": "premium characters",
        "exclusive character": "exclusive characters",
        "voice call": "voice calls",
        "image generation": "image generation",
        "photo generation": "image generation",
        "memory": "extended memory",
        "longer memory": "extended memory",
    }
    for kw, label in feature_keywords.items():
        if kw in blob and label not in feature_snippets:
            feature_snippets.append(label)

    if feature_snippets:
        sub_features = "; ".join(feature_snippets)
    elif offers_iap is False:
        sub_features = "No subscription (no IAP offered)"
    else:
        sub_features = UNKNOWN

    return {
        "subscription_required_for_long_chat": sub_req_long,
        "all_features_available_without_subscription": all_free,
        "subscription_features": sub_features,
    }


def extract_languages(app: dict) -> str:
    # iOS JSON sometimes carries a languageCodesISO2A list
    langs = app.get("languageCodesISO2A") or app.get("languages")
    if langs and isinstance(langs, list):
        return ", ".join(sorted(set(langs)))
    return UNKNOWN


# ---------- Main pipeline ----------
def build_rows(apps: list) -> list:
    """Classify and extract fields for a list of normalised app dicts."""
    rows = []
    for app in apps:
        app_type, rationale = classify_app_type(app)
        age_req, age_method = extract_age_verification(app)
        sub_hints = extract_subscription_hints(app, app_type)
        web_url = best_web_url(app)

        row = {
            "platform": app.get("_platform", "android"),
            "appId": app.get("appId", ""),
            "title": app.get("title", ""),
            "summary": app.get("summary", ""),
            "developer": app.get("developer", ""),
            "developerEmail": app.get("developerEmail", ""),
            "developerWebsite": app.get("developerWebsite", ""),
            "privacyPolicy": app.get("privacyPolicy", ""),
            "genre": app.get("genre", ""),
            "genreId": app.get("genreId", ""),
            "contentRating": app.get("contentRating", ""),
            "contentRatingDescription": app.get("contentRatingDescription", ""),
            "free": app.get("free", ""),
            "price": app.get("price", ""),
            "priceText": app.get("priceText", ""),
            "currency": app.get("currency", ""),
            "offersIAP": app.get("offersIAP", ""),
            "IAPRange": app.get("IAPRange", ""),
            "installs": app.get("installs", ""),
            "minInstalls": app.get("minInstalls", ""),
            "score": app.get("score", ""),
            "ratings": app.get("ratings", ""),
            "reviews": app.get("reviews", ""),
            "released": app.get("released", ""),
            "version": app.get("version", ""),
            "androidVersion": app.get("androidVersion", ""),
            "adSupported": app.get("adSupported", ""),
            "url": app.get("url", ""),
            # task-required new fields
            "app_type": app_type,
            "app_type_rationale": rationale,
            "web_accessible": UNKNOWN,
            "web_url": web_url,
            "login_required": UNKNOWN,
            "login_methods": UNKNOWN,
            "age_verification_required": age_req,
            "age_verification_method": age_method,
            "subscription_required_for_long_chat": sub_hints["subscription_required_for_long_chat"],
            "all_features_available_without_subscription": sub_hints["all_features_available_without_subscription"],
            "subscription_features": sub_hints["subscription_features"],
            "subscription_cost": extract_subscription_cost(app),
            "languages_supported": extract_languages(app),
        }
        rows.append(row)
    return rows


def main():
    ap = argparse.ArgumentParser(
        description="Classify companion apps and produce evaluation CSV.",
        epilog="If no flags are given, defaults to --android google_play_apps_details.json",
    )
    ap.add_argument("--android", type=Path, metavar="JSON",
                    help="Android app-details JSON (google-play-scraper format)")
    ap.add_argument("--ios", type=Path, metavar="JSON",
                    help="iOS app-details JSON (app-store-scraper / iTunes Search API format)")
    ap.add_argument("-o", "--output", type=Path, default=Path("companion_apps_evaluation.csv"),
                    help="Output CSV path (default: companion_apps_evaluation.csv)")
    args = ap.parse_args()

    # Default: Android-only, backward-compatible
    if args.android is None and args.ios is None:
        args.android = Path("google_play_apps_details.json")

    all_apps = []
    if args.android:
        print(f"Loading Android source: {args.android}")
        all_apps.extend(load_source(args.android, "android"))
    if args.ios:
        print(f"Loading iOS source: {args.ios}")
        all_apps.extend(load_source(args.ios, "ios"))

    if not all_apps:
        print("No apps loaded — check your input files.", file=__import__("sys").stderr)
        raise SystemExit(1)

    rows = build_rows(all_apps)

    # Write CSV
    fieldnames = list(rows[0].keys())
    with args.output.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(rows)

    # Summary
    from collections import Counter
    type_dist = Counter(r["app_type"] for r in rows)
    plat_dist = Counter(r["platform"] for r in rows)
    print(f"\nWrote {len(rows)} rows to {args.output}")
    print(f"  platforms: {dict(plat_dist)}")
    print(f"  app_type:  {dict(type_dist)}")


if __name__ == "__main__":
    main()
