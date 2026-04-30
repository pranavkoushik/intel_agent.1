# Joveo Publisher Intelligence Agent

Automated weekday intelligence workflow for Joveo. Researches recent
publisher-related developments (layoffs, insolvency, funding, M&A,
partnerships, product launches), generates an AI summary with Gemini,
posts a curated digest to Slack, and remembers which URLs have already
been delivered so they are not posted again.

---

## What it does, in one sentence

Every weekday at 10:00 AM IST, the bot researches a scheduled batch of
job-board publishers, surfaces strategic news, and posts a Slack digest
with mandatory inclusion of any existential signal (insolvency, mass
layoff, acquisition, IPO).

---

## Architecture

```
Publisher-Intel/
├── api/
│   └── index.py            FastAPI app — all HTTP routes
├── app/
│   ├── __init__.py         Package init + logging setup
│   ├── config.py           pydantic-settings Settings class
│   ├── publishers.py       P0 / P1 / P2 lists + weekday rotation
│   ├── services.py         Tavily, Gemini, Slack, Sheets, dedup, filtering
│   └── scheduler.py        run_publisher_intel() — end-to-end orchestration
├── brief.py                Local CLI entrypoint (python brief.py)
├── requirements.txt        Python dependencies
├── vercel.json             Vercel function config + cron schedule
├── .env.example            Template for required environment variables
└── credentials.json        Local Google service-account fallback (gitignored)
```

---

## End-to-end pipeline

```
                    Vercel cron fires at 04:30 UTC, Mon-Fri
                                    ↓
                       api/index.py → cron() route
                       checks optional CRON_SECRET bearer
                                    ↓
                    app/scheduler.py → run_publisher_intel()
                                    ↓
   1. Pick today's batch         app/publishers.py: get_todays_publishers()
                                  Mon/Thu → 25 P0 publishers
                                  Tue/Wed/Fri → ~40 publishers from
                                    rotating P1/P2 batch (ISO week % 3)
                                  Sat/Sun → exit with skipped status
                                    ↓
   2. News fetch                 app/services.py
                                  Per publisher, THREE sources are queried:
                                  (a) Tavily — general web news, multilingual
                                      themed boolean (max 5).
                                  (b) Tavily — LinkedIn-restricted query for
                                      public posts/articles indexed by Google
                                      (max 3). Tavily proxies the request so
                                      your IP is never exposed to LinkedIn.
                                  (c) Google News RSS — free redundancy layer
                                      with different ranking from Tavily
                                      (max 3). Catches stories Tavily misses.
                                  All three merge; dedup downstream collapses
                                  overlap via title-similarity (0.85).
                                    ↓
   3. Drop stale-year URLs       quick_filter()
                                    ↓
   4. Two-phase dedup            deduplicate_news()
                                  Pass 1: URL-exact match
                                  Pass 2: title-similarity (difflib,
                                    threshold 0.85) drops the same story
                                    republished across outlets.
                                  Run BEFORE the date filter so we don't
                                  spend HTTP fetches on duplicates.
                                    ↓
   5. Filter previously sent     load_sent_urls() reads column A of the
                                  configured Google Sheet. Drops any URL
                                  already posted in a prior run.
                                    ↓
   6. Date filter                filter_recent_news()
                                  /{current_year}/ in URL → keep
                                  Else: Tavily metadata → HTML <meta> →
                                    snippet regex → drop if older than 7 days
                                  Aggregator/roundup pages always rejected.
                                  Most expensive step (HTTP fetches), so it
                                  runs on the smallest input.
                                    ↓
   7. Sticky split for critical  split_critical()
      events                      Items matching CRITICAL_KEYWORDS (insolvency,
                                  bankruptcy, mass layoffs, acquisitions, IPO,
                                  exec resignations, lawsuits, data breaches,
                                  multilingual variants) lead the list so the
                                  Gemini prompt sees them first. No cap.
                                    ↓
   8. Empty? → post fallback     "No impactful updates" Slack message and exit.
                                    ↓
   9. Generate brief             generate_brief() — Gemini 2.5 Flash Lite
                                  Prompt has a MANDATORY rule: any
                                  insolvency/bankruptcy/M&A/major-funding item
                                  must always be in the final 5.
                                  Output uses impact tags 🔥 ⚠️ 📈 🧠.
                                    ↓
   10. Post to Slack             post_to_slack()
                                  3 retries with exponential backoff (2s, 4s).
                                    ↓
   11. On HTTP 200 only:         save_sent_urls() appends to column A so
       persist URLs to Sheet     future runs won't re-post the same items.
                                    ↓
                        Return JSON status to Vercel
```

---

## HTTP endpoints

All endpoints live in [api/index.py](api/index.py) as a single FastAPI app.

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/cron` | GET, POST | Triggers the full pipeline. Fired by Vercel cron. Optional `Authorization: Bearer <CRON_SECRET>` header. Returns 503 if any required API key is missing. |
| `/api/health` | GET | Lightweight uptime probe. Returns `{status, service, timestamp}`. |
| `/api/schedule` | GET | Returns today's date, weekday, batch label, publisher count, and full publisher list — without triggering the pipeline. |

---

## Schedule

Configured in [vercel.json](vercel.json):

- Cron expression: `30 4 * * 1-5`
- 04:30 UTC = 10:00 AM IST
- Monday through Friday only

### Publisher coverage by weekday

| Day | Coverage | Notes |
|-----|----------|-------|
| Monday | P0 publishers (25) | Highest-priority list |
| Tuesday | P1/P2 Batch 1 | Rotates by ISO week |
| Wednesday | P1/P2 Batch 2 | Rotates by ISO week |
| Thursday | P0 publishers (25) | Same as Monday |
| Friday | P1/P2 Batch 3 | Rotates by ISO week |
| Sat / Sun | Skipped | No cron fires |

The full P1/P2 list is sorted alphabetically and split into 3 batches.
Which alphabetical slice runs on Tue/Wed/Fri rotates by ISO week number,
so the entire P1/P2 universe is covered roughly every three weeks without
querying the long list every day.

---

## Environment variables

Loaded by [app/config.py](app/config.py) via `pydantic-settings`. Field
names are case-insensitive when reading from the environment.

### Required

| Variable | Purpose |
|----------|---------|
| `SLACK_WEBHOOK_URL` | Incoming webhook for the digest channel |
| `GEMINI_API_KEY` | Authentication for Google GenAI SDK |
| `TAVILY_API_KEY` | Authentication for Tavily search |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Service-account JSON blob for Sheets access (Vercel) |

### Optional (with defaults)

| Variable | Default | Purpose |
|----------|---------|---------|
| `GEMINI_MODEL` | `gemini-2.5-flash-lite` | Which Gemini model generates the brief |
| `GOOGLE_SHEET_NAME` | `Joveo Intel Logs` | Spreadsheet name for the URL ledger |
| `GOOGLE_WORKSHEET_NAME` | `Sheet1` | Tab name inside the spreadsheet |
| `TAVILY_MAX_RESULTS` | `5` | General news results per publisher per run |
| `TAVILY_LINKEDIN_MAX_RESULTS` | `3` | LinkedIn-restricted results per publisher per run |
| `GOOGLE_NEWS_MAX_RESULTS` | `3` | Google News RSS results per publisher (set `0` to disable) |
| `TAVILY_SEARCH_DEPTH` | `advanced` | Tavily search mode |
| `NEWS_LOOKBACK_DAYS` | `7` | Date window for the lookback filter |
| `SLACK_RETRIES` | `3` | Number of Slack post attempts |
| `SLACK_TIMEOUT` | `20` | Seconds per Slack request |
| `TITLE_SIMILARITY_THRESHOLD` | `0.85` | Higher = stricter (fewer dedup drops) |
| `CRON_SECRET` | _(unset)_ | If set, `/api/cron` requires `Authorization: Bearer <value>` |
| `LOG_LEVEL` | `INFO` | Standard Python logging level |

---

## Local development

```bash
# Install dependencies
pip install -r requirements.txt

# Configure .env (copy .env.example as a starting point)
cp .env.example .env
# ...then fill in SLACK_WEBHOOK_URL, GEMINI_API_KEY, TAVILY_API_KEY, and
# either GOOGLE_SERVICE_ACCOUNT_JSON or place credentials.json at the repo root.

# Run the full pipeline once
python brief.py

# Or serve the FastAPI app and hit the endpoints
uvicorn api.index:app --reload
curl http://localhost:8000/api/health
curl http://localhost:8000/api/schedule
curl -X POST http://localhost:8000/api/cron
```

If `GOOGLE_SERVICE_ACCOUNT_JSON` is empty, the code falls back to a local
`credentials.json` file at the repo root. This fallback is convenient for
local dev only — Vercel deployments should always use the environment
variable.

---

## Production deployment (Vercel)

1. Push the repo to a Vercel project.
2. Set environment variables in the Vercel dashboard (at minimum the four
   required keys above; set `CRON_SECRET` to protect the cron endpoint).
3. The cron schedule in `vercel.json` registers automatically.
4. Verify with `curl https://<your-app>.vercel.app/api/health`.

`credentials.json` is excluded from the deploy bundle by `vercel.json` and
ignored by `.gitignore`, so the local fallback file never ships with the
deployed function.

---

## Key design decisions

### Critical events bypass any ranking
The pipeline used to apply a top-N keyword-scoring cap that could rank
out high-impact items (e.g., a German-language insolvency story scoring
zero on English-only keywords). The current pipeline detects existential
signals via `CRITICAL_KEYWORDS` (multilingual: insolvency, bankruptcy,
chapter 11, mass layoffs, acquisitions, IPO, Series A-E, plus the same in
DE / FR / ES / IT / NL / PL) and surfaces them first in the Gemini prompt
context. A `MANDATORY` rule in the prompt then guarantees they make the
final 5-item digest.

### No keyword-based ranking cap
All items that survive the date filter and dedup pass go to Gemini. The
extra context is cheap (Gemini handles ranking well, and per-run token
cost is negligible) and removes a class of "important news ranked out
before Gemini saw it" failures.

### Title-similarity dedup
Same story across multiple outlets used to burn 3+ slots in the digest.
A second dedup pass uses `difflib.SequenceMatcher` on normalized titles
with a configurable threshold (default `0.85`) so duplicate stories
collapse to one item.

### Sent URLs persist only after a successful Slack post
If Slack delivery fails, no URLs are written to the Sheet. The next run
can therefore retry the same items rather than treating them as already
sent.

### Multilingual Tavily query
The Tavily query string covers layoff/insolvency/hiring/partnership
phrases in eight languages so European publishers (Joblift, Stellenanzeigen,
Pracuj.pl, etc.) surface non-English coverage that English-only queries
miss.

### LinkedIn coverage without scraping
Direct scraping of LinkedIn would violate ToS and get IPs banned within
hours. Instead, the bot runs a second Tavily query per publisher restricted
to `site:linkedin.com/posts`, `/pulse`, and `/company` paths. Tavily makes
the actual outbound request, so the bot's IP is never exposed to LinkedIn.
This catches public LinkedIn announcements (CEO posts, company-page news,
public articles) without any auth-walled content. The dedup step collapses
any overlap when the same story is also covered by mainstream press.

### Cross-source redundancy via Google News RSS
Tavily is the primary news source, but a single search vendor is a single
point of failure. Google News RSS is queried in parallel as a free
redundancy layer (no API key required) — different ranking and indexing
than Tavily, so it catches stories Tavily misses and vice versa. Results
collapse with Tavily's via the same title-similarity dedup, so duplicates
across the two sources don't burn slots in the digest. Set
`GOOGLE_NEWS_MAX_RESULTS=0` to disable if needed.

---

## Intended outcome

A repeatable weekday intelligence workflow that:

- researches a scheduled publisher cohort
- surfaces strategic and existential updates
- converts them into a concise Slack digest
- avoids duplicate reporting (URL + title-similarity)
- maintains a reliable ledger of already-posted articles
- never silently misses a P0 going under
