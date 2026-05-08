"""Service layer: Google News RSS fetch, Gemini analysis, Slack delivery, Sheets tracking."""

from __future__ import annotations

import datetime
import difflib
import json
import logging
import re
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import gspread
import requests
from bs4 import BeautifulSoup
from google import genai
from google.oauth2.service_account import Credentials

from .config import Settings

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent

GOOGLE_SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]


# ── Google Sheets ────────────────────────────────────────────────────────────

def _get_service_account_info(settings: Settings) -> dict[str, Any]:
    # Prefer the env-var JSON blob (used on Vercel). Fall back to local
    # credentials.json so dev workflows keep working without re-config.
    raw_json = settings.google_service_account_json
    if raw_json:
        info = json.loads(raw_json)
    else:
        credentials_path = REPO_ROOT / "credentials.json"
        if not credentials_path.exists():
            raise ValueError(
                "GOOGLE_SERVICE_ACCOUNT_JSON not set and credentials.json not found"
            )
        with credentials_path.open(encoding="utf-8") as f:
            info = json.load(f)

    private_key = info.get("private_key")
    if isinstance(private_key, str):
        # When JSON is copied into an env var, newlines are often escaped.
        info["private_key"] = private_key.replace("\\n", "\n")

    return info


def _get_sheet(settings: Settings):
    credentials = Credentials.from_service_account_info(
        _get_service_account_info(settings),
        scopes=GOOGLE_SCOPES,
    )
    client = gspread.authorize(credentials)
    workbook = client.open(settings.google_sheet_name)
    if settings.google_worksheet_name:
        return workbook.worksheet(settings.google_worksheet_name)
    return workbook.sheet1


def load_sent_items(settings: Settings) -> tuple[set[str], list[str]]:
    """Load the ledger as (sent URLs, normalized sent titles).

    Titles are returned pre-normalized so the ledger filter can compare with
    `difflib.SequenceMatcher` directly.
    """
    try:
        sheet = _get_sheet(settings)
        urls = set(sheet.col_values(1))
        titles = [normalize_title(t) for t in sheet.col_values(2) if t]
        return urls, titles
    except Exception:
        logger.exception("Failed to load sent items from Sheets")
        return set(), []


def save_sent_items(items: list[tuple[str, str]], settings: Settings) -> None:
    """Append (url, title) rows to the ledger, skipping URLs already present."""
    try:
        sheet = _get_sheet(settings)
        existing = set(sheet.col_values(1))
        new_rows = [[url, title] for url, title in items if url and url not in existing]
        if new_rows:
            sheet.append_rows(new_rows)
            logger.info("Saved %d new items to Sheets", len(new_rows))
    except Exception:
        logger.exception("Failed to save sent items to Sheets")


def filter_ledger(
    news: list[dict],
    sent_urls: set[str],
    sent_titles: list[str],
    settings: Settings,
) -> list[dict]:
    """Drop items whose URL matches the ledger or whose title is near-duplicate
    of a previously-sent title. Title check protects against Google News RSS
    redirect URLs that change between runs even for the same article."""
    threshold = settings.title_similarity_threshold
    fresh: list[dict] = []
    for item in news:
        if item.get("url") in sent_urls:
            continue
        title_norm = normalize_title(item.get("title", ""))
        if title_norm and any(
            difflib.SequenceMatcher(None, title_norm, t).ratio() >= threshold
            for t in sent_titles
        ):
            continue
        fresh.append(item)
    return fresh


# ── Google News RSS news fetch (sole source) ─────────────────────────────────

_GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search"

# Google News redirect URLs occasionally embed soft hyphens (U+00AD) and other
# invisible Unicode in the base64 payload. Slack's <URL|label> parser drops the
# whole hyperlink when it sees them, leaving the raw URL visible in the digest.
# Strip Unicode "control" (Cc) and "format" (Cf) categories — soft hyphens,
# zero-width spaces, BOMs, and ASCII control bytes all live here.
_INVISIBLE_CATEGORIES = {"Cc", "Cf"}

def _sanitize_url(url: str) -> str:
    cleaned = "".join(c for c in url if unicodedata.category(c) not in _INVISIBLE_CATEGORIES)
    return cleaned.strip()


# Per-language RSS query specs. Each entry pairs a region-targeted set of
# Google News parameters (hl/gl/ceid drive language + region of results) with
# a native-tongue keyword boolean. Running one sub-query per language gives EU
# publishers' local-language coverage that an English-only query misses, and
# Google News RSS is free so the extra fan-out has no per-call cost.
_RSS_LANGUAGE_QUERIES: list[tuple[str, dict[str, str], str]] = [
    (
        "en",
        {"hl": "en-US", "gl": "US", "ceid": "US:en"},
        'intitle:"{pub}" (insolvency OR bankruptcy OR layoffs OR funding OR acquisition '
        "OR merger OR launch OR hiring OR partnership OR closure OR shutdown "
        'OR lawsuit OR "data breach" OR resigns)',
    ),
    (
        "de",
        {"hl": "de", "gl": "DE", "ceid": "DE:de"},
        'intitle:"{pub}" (insolvenz OR pleite OR entlassungen OR stellenabbau OR '
        "übernahme OR fusion OR einstellungen OR partnerschaft OR kooperation)",
    ),
    (
        "fr",
        {"hl": "fr", "gl": "FR", "ceid": "FR:fr"},
        'intitle:"{pub}" (faillite OR licenciements OR "plan social" OR rachat OR '
        "fusion OR embauche OR partenariat OR lancement)",
    ),
    (
        "es",
        {"hl": "es", "gl": "ES", "ceid": "ES:es"},
        'intitle:"{pub}" (quiebra OR despidos OR adquisición OR fusión OR contratación '
        "OR alianza OR lanzamiento)",
    ),
    (
        "it",
        {"hl": "it", "gl": "IT", "ceid": "IT:it"},
        'intitle:"{pub}" (fallimento OR licenziamenti OR acquisizione OR fusione OR '
        "assunzioni OR partnership OR lancio)",
    ),
    (
        "nl",
        {"hl": "nl", "gl": "NL", "ceid": "NL:nl"},
        'intitle:"{pub}" (faillissement OR ontslagen OR overname OR fusie OR '
        "aanwerving OR samenwerking)",
    ),
    (
        "pl",
        {"hl": "pl", "gl": "PL", "ceid": "PL:pl"},
        'intitle:"{pub}" (upadłość OR zwolnienia OR przejęcie OR fuzja OR zatrudnia '
        "OR partnerstwo)",
    ),
]


def fetch_google_news_rss(publishers: list[str], settings: Settings) -> list[dict]:
    """Per publisher, run one Google News RSS query per supported language.

    Free, no API key. Each sub-query targets a specific language/region via
    Google News' hl/gl/ceid parameters so we surface coverage from the local
    press of each EU market. Results all flow into the same downstream
    pipeline (split_critical, filter_recent_news, deduplicate_news), so cross-
    language coverage of the same story collapses on title similarity.
    """
    if settings.google_news_max_results <= 0:
        return []

    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        days=settings.news_lookback_days
    )
    all_results: list[dict] = []

    for pub in publishers:
        for lang, lang_params, query_template in _RSS_LANGUAGE_QUERIES:
            query = query_template.format(pub=pub)
            params = {"q": query, **lang_params}
            url = f"{_GOOGLE_NEWS_RSS_URL}?{urllib.parse.urlencode(params)}"

            try:
                response = requests.get(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (compatible; JoveoIntelBot/1.0)"
                    },
                    timeout=20,
                )
                if response.status_code != 200:
                    logger.warning(
                        "Google News RSS returned %d for %s [%s]",
                        response.status_code, pub, lang,
                    )
                    continue

                root = ET.fromstring(response.text)
                channel = root.find("channel")
                if channel is None:
                    continue

                count = 0
                for item in channel.findall("item"):
                    title_el = item.find("title")
                    link_el = item.find("link")
                    desc_el = item.find("description")
                    pub_date_el = item.find("pubDate")

                    if title_el is None or link_el is None:
                        continue

                    # Filter to lookback window using RSS pubDate (RFC 822 format).
                    published_iso: str | None = None
                    if pub_date_el is not None and pub_date_el.text:
                        try:
                            dt = parsedate_to_datetime(pub_date_el.text)
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=datetime.timezone.utc)
                            if dt < cutoff:
                                continue
                            published_iso = dt.isoformat()
                        except (TypeError, ValueError):
                            # If we can't parse the date, let filter_recent_news
                            # handle it downstream rather than dropping silently.
                            pass

                    all_results.append({
                        "title": title_el.text or "",
                        "url": _sanitize_url(link_el.text or ""),
                        "content": (desc_el.text or "") if desc_el is not None else "",
                        "published_date": published_iso,
                    })
                    count += 1
                    if count >= settings.google_news_max_results:
                        break

                logger.info(
                    "Fetched %d Google News results for %s [%s]", count, pub, lang,
                )
            except Exception:
                logger.exception("Google News RSS failed for %s [%s]", pub, lang)

    return all_results


# ── Date extraction ──────────────────────────────────────────────────────────

def fetch_article_date(url: str) -> datetime.datetime | None:
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        res = requests.get(url, headers=headers, timeout=20)
        soup = BeautifulSoup(res.text, "html.parser")
        meta_tags = [
            {"property": "article:published_time"},
            {"name": "article:published_time"},
            {"property": "og:published_time"},
            {"name": "pubdate"},
            {"name": "publish-date"},
        ]
        for tag in meta_tags:
            meta = soup.find("meta", tag)
            if meta and meta.get("content"):
                try:
                    return datetime.datetime.fromisoformat(
                        meta["content"].replace("Z", "+00:00")
                    )
                except ValueError:
                    continue
    except Exception:
        logger.debug("Failed to fetch date from %s", url)
    return None


def extract_date_from_text(text: str) -> datetime.datetime | None:
    if not text:
        return None
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if not match:
        return None
    try:
        return datetime.datetime.fromisoformat(match.group())
    except ValueError:
        return None


# ── Critical-signal classification (sticky bypass) ───────────────────────────

# Hard-fail / existential signals. Items matching these are surfaced first in
# the Gemini prompt context and the prompt's MANDATORY rule guarantees they
# make the final 5-item digest, so a P0 going under is never missed.
CRITICAL_KEYWORDS = [
    # English distress
    "insolvency", "insolvent", "bankruptcy", "bankrupt",
    "files for chapter", "chapter 11", "chapter 7",
    "liquidation", "receivership", "administration",
    "shuts down", "shutting down", "shut down",
    "ceases operations", "ceasing operations", "cease operations",
    "closes doors", "closes its doors",
    "going out of business", "going under",
    "winds down", "winding down",
    "exits market", "pulls out of",
    "mass layoff", "mass layoffs", "lays off", "laying off",
    # Major M&A
    "acquired by", "acquires", "acquisition of",
    "merger with", "merges with", "merges into",
    "taken private", "sells to",
    # Major funding (high-impact positive)
    "series a", "series b", "series c", "series d", "series e",
    "ipo", "goes public", "files to go public",
    # German distress
    "insolvenz", "insolvenzantrag", "pleite", "zahlungsunfähig",
    "stellenabbau", "personalabbau", "massenentlassungen", "entlassungen",
    "geschäftsaufgabe", "betriebsschließung",
    # French distress
    "faillite", "redressement judiciaire", "cessation de paiements",
    "liquidation judiciaire", "plan social", "licenciements",
    "suppressions de postes", "cessation d'activité",
    # Spanish distress
    "quiebra", "concurso de acreedores", "insolvencia",
    "despidos", "despidos masivos", "recortes de personal",
    "reducciones de plantilla", "cesa actividad",
    # Italian distress
    "fallimento", "amministrazione straordinaria",
    "licenziamenti", "licenziamenti di massa", "cessa l'attività",
    # Dutch distress
    "faillissement", "ontslagen", "massaontslag", "bedrijfssluiting",
    # Polish distress
    "bankructwo", "upadłość", "zwolnienia", "zwolnienia grupowe",
    "likwidacja", "redukcja zatrudnienia",
    # Executive departures (English)
    "ceo resigns", "ceo steps down", "ceo departs", "ceo replaced",
    "ceo to step down", "ceo fired", "ceo ousted",
    "founder departs", "founder steps down", "co-founder departs",
    "president resigns", "cfo resigns", "cfo steps down",
    # Executive departures (multilingual — EU CEOs often resign in local press)
    "tritt zurück", "rücktritt",            # German
    "démissionne", "démission",             # French
    "dimite", "dimisión",                   # Spanish
    "si dimette", "dimissioni",             # Italian
    "treedt af", "stapt op",                # Dutch
    "rezygnuje", "ustępuje",                # Polish
    # Legal / regulatory
    "class action", "lawsuit", "files suit", "sued for",
    "regulatory fine", "ftc investigation", "doj investigation",
    "under investigation", "antitrust", "settlement reached",
    "subpoena",
    # Cyber / data incidents
    "data breach", "security breach", "ransomware attack",
    "data leak", "credentials leaked",
]


def is_critical_item(item: dict) -> bool:
    text = (item.get("title", "") + " " + item.get("content", "")).lower()
    return any(kw in text for kw in CRITICAL_KEYWORDS)


def split_critical(news: list[dict]) -> tuple[list[dict], list[dict]]:
    """Separates existential/high-impact items from the rest."""
    critical: list[dict] = []
    regular: list[dict] = []
    for item in news:
        (critical if is_critical_item(item) else regular).append(item)
    return critical, regular


# ── Filtering & dedup ────────────────────────────────────────────────────────

_AGGREGATOR_PATTERNS = [
    "mass-layoffs", "layoff-tracker", "layoffs-tracker", "job-cuts",
    "job-losses", "companies-that", "company-list", "list-of", "roundup",
    "weekly-roundup", "monthly-roundup", "latest-updates", "industry-updates",
    "market-update",
]


def is_aggregator_page(url: str) -> bool:
    url_lower = url.lower()
    return any(p in url_lower for p in _AGGREGATOR_PATTERNS)


def is_current_year_url(url: str) -> bool:
    return f"/{datetime.datetime.now().year}/" in url


def quick_filter(news: list[dict]) -> list[dict]:
    current_year = datetime.datetime.now().year
    old_years = list(range(2012, current_year - 1))
    filtered: list[dict] = []
    for item in news:
        url = item.get("url", "")
        if any(str(year) in url for year in old_years):
            continue
        if any(f"/{year}/" in url for year in old_years):
            continue
        filtered.append(item)
    return filtered


def filter_recent_news(results: list[dict], settings: Settings) -> list[dict]:
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        days=settings.news_lookback_days
    )
    filtered: list[dict] = []

    for item in results:
        url = item.get("url", "")
        if is_aggregator_page(url):
            continue

        # Current-year article paths are a reliable freshness signal.
        if is_current_year_url(url):
            filtered.append(item)
            continue

        pub_date: datetime.datetime | None = None

        if item.get("published_date"):
            try:
                pub_date = datetime.datetime.fromisoformat(item["published_date"])
            except ValueError:
                pub_date = None

        if not pub_date:
            pub_date = fetch_article_date(url)

        if not pub_date:
            pub_date = extract_date_from_text(item.get("content", ""))

        if pub_date is None:
            continue

        if pub_date.tzinfo is None:
            pub_date = pub_date.replace(tzinfo=datetime.timezone.utc)

        if pub_date < cutoff:
            continue

        filtered.append(item)

    return filtered


def normalize_title(title: str) -> str:
    # Lowercase + collapse non-alphanumerics so headline variations like
    # "ZipRecruiter Raises $300M — TechCrunch" and "ZipRecruiter raises 300M | Reuters"
    # collapse to the same comparable string.
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def deduplicate_news(results: list[dict], settings: Settings) -> list[dict]:
    # First pass: drop exact URL duplicates. Critical items appear earlier in
    # the input list, so URL-first preserves them.
    seen_urls: set[str] = set()
    url_unique: list[dict] = []
    for item in results:
        url = item.get("url", "")
        if url in seen_urls:
            continue
        seen_urls.add(url)
        url_unique.append(item)

    # Second pass: drop near-duplicate titles. Same story across Reuters,
    # Bloomberg, TechCrunch shows up as different URLs but very similar titles.
    # Without this, one viral story can burn 3+ of Gemini's 5 output slots.
    deduped: list[dict] = []
    kept_titles: list[str] = []
    threshold = settings.title_similarity_threshold
    for item in url_unique:
        title_norm = normalize_title(item.get("title", ""))
        if not title_norm:
            deduped.append(item)
            continue

        is_dup = any(
            difflib.SequenceMatcher(None, title_norm, kept).ratio() >= threshold
            for kept in kept_titles
        )
        if is_dup:
            continue

        kept_titles.append(title_norm)
        deduped.append(item)

    return deduped


# ── Gemini brief generation ──────────────────────────────────────────────────

BRIEF_PROMPT_TEMPLATE = """
You are the Joveo Publisher Intelligence Agent.

Today is {today}.

ALLOWED PUBLISHERS (CRITICAL — read first):
You may ONLY select items where the headline is genuinely ABOUT one of these
publishers as the primary subject:
{publishers_list}

Discard any item where the publisher name is incidental — e.g. a tangentially-
related company with a similar name, a bank, a sports league, a movie, a
historical reference (a year like "1840"), or a person who happens to share
the name. If unsure, DISCARD. Off-topic items do not become eligible just
because they sound dramatic.

Below is REAL-TIME news data collected from the web:

{context}

TASK:
From this data, select the TOP 5 most impactful updates relevant to Joveo.
PRIORITY (read carefully):
- Items mentioning insolvency, bankruptcy, mass layoffs, shutdown,
  acquisition/merger, or major funding for a publisher are MANDATORY.
  These must always be included.
- Fill remaining slots (up to 5 total) with the next highest-impact items.
- If you have more than 5 mandatory items, keep the 5 most recent/severe.

OUTPUT FORMAT:

📡 *Joveo Publisher Intel*
📅 {today}

━━━━━━━━━━━━━━━━━━

For each item:

[Impact Emoji] *[Publisher Name]*
[One sentence insight explaining what happened + why it matters to Joveo]

🔗 <URL|Read article>
(Add a blank line)
(Repeat up to 5 items, each separated by a blank line)

━━━━━━━━━━━━━━━━━━

📊 _Coverage: {coverage_label}_
🔎 _Source: Google News RSS_

---

IMPACT TAG RULES:
- Use 🔥 for high-impact (funding, major product launches, large layoffs, acquisitions)
- Use ⚠️ for risk signals (declining hiring, layoffs, revenue pressure)
- Use 📈 for growth signals (expansion, hiring surge, new markets)
- Use 🧠 for strategic/product updates

---

FORMATTING RULES:
- Render the URL as a Slack hyperlink using EXACTLY this format: <URL|Read article>
  (e.g. <https://example.com/story|Read article>). Do NOT output the raw URL.
- Keep each item visually separated
- Keep it clean and scannable
- Ensure there is a blank line between each item
- Do NOT cluster items together
- Keep formatting clean and readable

RULES:
- Only use the provided data. Order items by impact (highest first) and date (latest to oldest)
- No hallucination
- Max 5 items (Only important ones) - give less if 5 are not very important
- One sentence each
- Each selected item MUST be primarily about a publisher in the ALLOWED
  PUBLISHERS list above. If no items qualify, return fewer than 5 — or none.

STRICT OUTPUT RULES:
- Output MUST start directly with: :satellite_antenna: *Joveo Publisher Intel*
- DO NOT include any introduction, explanation, or apology
- DO NOT mention lack of data
- DO NOT say "Unfortunately" or similar
- If no valid items exist, return an empty string
- Show the name of P0/P1/P2 publisher for which the news is relevant in the output.

IMPORTANT:
- Focus on important news from the LAST {lookback_days} DAYS
- Ignore any news older than {lookback_days} days, even if provided.
"""


def generate_brief(
    news_data: list[dict],
    coverage_label: str,
    publishers: list[str],
    settings: Settings,
) -> str | None:
    today = datetime.date.today().strftime("%A, %d %B %Y")
    context = "\n\n".join(
        f"TITLE: {item.get('title', 'N/A')}\n"
        f"URL: {item.get('url', 'N/A')}\n"
        f"CONTENT: {item.get('content', 'N/A')[:300]}"
        for item in news_data
    )
    publishers_list = ", ".join(publishers)

    prompt = BRIEF_PROMPT_TEMPLATE.format(
        today=today,
        context=context,
        coverage_label=coverage_label,
        publishers_list=publishers_list,
        lookback_days=settings.news_lookback_days,
    )

    try:
        client = genai.Client(api_key=settings.gemini_api_key)
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=prompt,
        )
        return response.text.strip() if response.text else None
    except Exception:
        logger.exception("Gemini generation failed")
        return None


# ── Slack delivery ───────────────────────────────────────────────────────────

def post_to_slack(message: str, settings: Settings) -> bool:
    """Post to Slack with exponential backoff between retries."""
    for attempt in range(1, settings.slack_retries + 1):
        try:
            resp = requests.post(
                settings.slack_webhook_url,
                json={"text": message},
                timeout=settings.slack_timeout,
            )
            if resp.status_code == 200:
                logger.info("Slack message delivered (attempt %d)", attempt)
                return True
            logger.warning("Slack returned %d on attempt %d", resp.status_code, attempt)
        except Exception:
            logger.exception("Slack post attempt %d failed", attempt)

        if attempt < settings.slack_retries:
            time.sleep(2 ** attempt)

    logger.error("All %d Slack delivery attempts exhausted", settings.slack_retries)
    return False
