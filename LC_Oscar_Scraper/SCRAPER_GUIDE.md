# LC_Oscar_Scraper -- Agent Guide

This document explains how the scraper works, what lives where, and what you can change. It is written for an agent or team member who needs to make modifications or debug issues in Cursor without prior context on the project.

---

## What it does

LC_Oscar_Scraper takes a list of business website URLs, crawls each site intelligently (finding team/about/contact pages via LLM-guided link discovery), then uses an LLM to extract structured firmographic data -- company info, decision maker names/titles/contact details, and factual descriptions optimized for cold-call context. Output is a set of CSVs ready for CRM import.

It was built for Australian accounting firms but the architecture is generic enough for any professional services vertical.

---

## Project structure

```
LC_Oscar_Scraper/
├── scripts/
│   ├── run_scraper_adaptive.py    # CLI entry point -- this is what you run
│   └── test_adaptive.py           # Test utility for single URLs or strategy comparison
│
├── src/
│   ├── __init__.py                # Package init
│   ├── config.py                  # Settings (pydantic-settings, loads from .env)
│   ├── schemas.py                 # Pydantic data models (CompanyData, DecisionMaker, etc.)
│   ├── adaptive_crawler.py        # Web crawler -- crawl4ai + LLM-guided link discovery
│   ├── link_analyzer.py           # LLM-based link classification (team/about/service/contact)
│   ├── llm_extractor.py           # LLM data extraction -- the extraction prompts live here
│   ├── adaptive_processor.py      # Orchestrator -- ties crawling, extraction, checkpointing
│   ├── checkpoint_manager.py      # Saves/loads state for resumable batch processing
│   ├── export.py                  # CSV export (flattens CompanyData into rows)
│   ├── failure_classifier.py      # Categorizes crawl errors (DNS, timeout, SSL, 403, etc.)
│   ├── phone_utils.py             # Phone normalization to E.164 format (AU/NZ/UK)
│   ├── progress_tracker.py        # Real-time progress display with ETA
│   └── logger.py                  # Structured logging (structlog, falls back to stdlib)
│
├── data/
│   ├── input/
│   │   ├── urls.txt               # Main URL list (one per line, # for comments)
│   │   └── sample_urls.txt        # Sample URLs for testing
│   ├── output/                    # Output CSVs land here
│   ├── state/                     # Checkpoint JSON files (for resume)
│   └── logs/                      # Log files (created at runtime)
│
├── .env                           # API keys and configuration (see table below)
└── requirements.txt               # Python dependencies
```

Files named `*_og.py` are old backups -- ignore them.

---

## How to run it

```bash
# Activate the venv first
source venv/bin/activate   # or wherever your venv lives

# Full batch run
python3 scripts/run_scraper_adaptive.py --batch-file data/input/urls.txt

# Resume an interrupted run from the latest checkpoint
python3 scripts/run_scraper_adaptive.py --resume

# Resume from a specific checkpoint file
python3 scripts/run_scraper_adaptive.py --resume-from data/state/batch_20260114_085841.json

# Quick run: homepage only, fewer pages
python3 scripts/run_scraper_adaptive.py --strategy main_only --max-pages 1

# Test a single URL (no checkpointing, prints to stdout)
python3 scripts/test_adaptive.py --url https://example-accounting.com.au

# Compare all three crawl strategies on sample URLs
python3 scripts/test_adaptive.py --compare --sample-count 5
```

### CLI flags for `run_scraper_adaptive.py`

| Flag | Default | Description |
|------|---------|-------------|
| `--batch-file <path>` | `data/input/urls.txt` | Path to URL list file |
| `--strategy <str>` | `adaptive` | Crawl strategy: `adaptive`, `greedy`, or `main_only` |
| `--max-pages <int>` | `5` | Max pages to crawl per site (including homepage) |
| `--checkpoint-name <str>` | `batch` | Name prefix for checkpoint files |
| `--resume` | off | Resume from the latest checkpoint |
| `--resume-from <path>` | none | Resume from a specific checkpoint file |
| `--skip-progress` | off | Disable real-time progress tracking |

### CLI flags for `test_adaptive.py`

| Flag | Default | Description |
|------|---------|-------------|
| `--url <url>` | none | Test a single URL |
| `--strategy <str>` | `adaptive` | Crawl strategy |
| `--max-pages <int>` | `5` | Max pages to crawl |
| `--compare` | off | Compare all strategies on sample URLs |
| `--sample-count <int>` | `5` | Number of sample URLs for batch/comparison tests |

---

## Data flow

```
URLs (text file, one per line)
  │
  ▼
run_scraper_adaptive.py -- loads URLs, initializes checkpoint
  │
  ▼
adaptive_processor.py -- orchestrates per-URL processing
  │
  │  For each URL (concurrency controlled by semaphore):
  │
  ├── 1. Validate URL format
  │
  ├── 2. CRAWL with adaptive_crawler.py
  │       │
  │       ├── Crawl homepage (discovery_config: keeps nav/footer for link context)
  │       │
  │       ├── Extract internal links with text context
  │       │
  │       ├── link_analyzer.py -- asks LLM to classify links:
  │       │     team_links, about_links, service_links, contact_links
  │       │     Returns priority_order (team first, then about/service/contact)
  │       │     Falls back to keyword matching if LLM fails
  │       │
  │       ├── Crawl top ~4 prioritized sub-pages (base_config: strips nav/footer)
  │       │
  │       └── If adaptive fails entirely -> fallback to main_only strategy
  │
  ├── 3. EXTRACT with llm_extractor.py
  │       │
  │       ├── Combine homepage + sub-page markdown into one string
  │       ├── Truncate to 25,000 chars
  │       ├── Call OpenRouter API (system prompt + user prompt)
  │       ├── Parse JSON response -> LLMExtractionResult
  │       └── Normalize phone numbers via phone_utils.py
  │
  ├── 4. CATEGORIZE the result:
  │       ├── out_of_scope=true -> OutOfScopeRecord
  │       ├── confidence < threshold -> LowConfidenceRecord
  │       └── otherwise -> CompanyData (success)
  │
  └── 5. UPDATE checkpoint_manager.py (saves every 10 URLs)
  │
  ▼
export.py -- writes output CSVs
  ├── results.csv            (successful extractions)
  ├── out_of_scope_urls.csv  (not accounting firms)
  ├── low_confidence_urls.csv (below confidence threshold)
  └── broken_urls.txt         (could not crawl at all)
```

---

## Module-by-module guide

### `src/config.py` -- Settings

All configuration loads from `.env` via `pydantic-settings`. The `Settings` class validates types and ranges.

| Setting | Default | What it controls |
|---------|---------|-----------------|
| `OPENROUTER_API_KEY` | *(required)* | API key for LLM calls via OpenRouter |
| `OPENROUTER_MODEL` | `arcee-ai/trinity-large-preview:free` | Which LLM model to use |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | OpenRouter endpoint |
| `MAX_CONCURRENT_CRAWLS` | `5` | Parallel crawls (1--50) |
| `PAGE_TIMEOUT` | `30000` | Page load timeout in ms (5000--300000) |
| `DEFAULT_COUNTRY` | `AU` | Phone number default country (AU/NZ/UK/US) |
| `MAX_DECISION_MAKERS` | `3` | Max DMs to extract per firm (1--10) |
| `LLM_TEMPERATURE` | `0.0` | LLM creativity (keep at 0 for extraction) |
| `REASONING_ENABLED` | `false` | Enable LLM reasoning mode (model-dependent) |
| `MIN_CONFIDENCE_THRESHOLD` | `0.5` | Below this, result goes to low_confidence CSV |
| `OUTPUT_DIR` | `data/output` | Directory for output CSVs |
| `MAX_RETRIES` | `3` | Retry count for failed crawls/LLM calls (0--10) |
| `RETRY_DELAY` | `2.0` | Base retry delay in seconds (0--60) |
| `DELAY_BETWEEN_REQUESTS` | `1.0` | Rate limiting between URLs in seconds (0--60) |

**To change the LLM model**: Edit `OPENROUTER_MODEL` in `.env`. The model must support JSON output (`response_format: json_object`). Browse available models at https://openrouter.ai/models.

**To change extraction behavior**: The prompts are in `llm_extractor.py` -- see that section below.

---

### `src/schemas.py` -- Data models

The core data structures. If you need to add a field to the output, this is where you start.

- **`BusinessSegment`**: Class with 5 segment constants:
  - General Accounting (Including Tax)
  - Tax Specialist
  - Bookkeeping (No Income Tax)
  - Other Accounting (No Tax)
  - Other Tax

- **`DecisionMaker`**: Individual DM with `name`, `title`, `decision_maker_summary`, `phone_office`, `phone_mobile`, `phone_direct`, `email`, `linkedin`. Phone and email fields have validators -- permissive (warns but doesn't reject) to avoid losing leads.

- **`CompanyData`**: The main output record. All company-level fields plus a list of `DecisionMaker` objects and `confidence_score`.

- **`LLMExtractionResult`**: Extends the same fields as `CompanyData` but adds `out_of_scope` and `out_of_scope_reason`. Includes validators to handle LLMs returning dicts instead of strings for `organisational_structure` and `out_of_scope`.

- **`OutOfScopeRecord`** / **`LowConfidenceRecord`**: Records that didn't pass validation.

**To add a new output field:**
1. Add it to `CompanyData` in `schemas.py`
2. Add it to the `LLMExtractionResult` model
3. Update the extraction prompt in `llm_extractor.py` to ask for it
4. Map it in `_company_to_dict()` in `export.py` for CSV output

---

### `src/adaptive_crawler.py` -- Web crawling

Uses `crawl4ai` (headless Chromium via Playwright) to crawl websites. Key class: `AdaptiveWebsiteCrawler`.

**How it works:**

1. **Homepage crawl** (`discovery_config`): Loads the main page with nav/footer intact (to extract link context).
2. **Link extraction**: Collects all internal links with their anchor text.
3. **Link analysis**: Sends links to `link_analyzer.py` for LLM classification, or uses keyword fallback.
4. **Sub-page crawl** (`base_config`): Crawls up to `max_pages - 1` prioritized pages with nav/footer stripped.
5. **Concurrency**: Sub-pages crawled concurrently with a semaphore (limit: 3).

**Three crawl strategies:**
- `adaptive` (default): LLM picks which links to follow. Falls back to keyword matching if LLM fails.
- `greedy`: Crawls all internal links (slow, expensive, thorough).
- `main_only`: Homepage only (fast, cheap, less data).

**Fallback behavior**: If the adaptive or greedy strategy fails entirely for a URL, `adaptive_processor.py` will retry with `main_only`.

**HTTPS/HTTP fallback**: The `try_url_with_fallback()` method tries HTTPS first, then falls back to HTTP on failure. This handles sites with expired certs or no HTTPS.

**To change browser settings**: Edit `self.browser_config` and `self.base_config` / `self.discovery_config` in the constructor.

**To change which links get prioritized**: Edit `_basic_link_discovery()` for keyword fallback, or `link_analyzer.py` for the LLM-guided approach.

---

### `src/link_analyzer.py` -- Link classification

Sends up to 20 internal links (URL + anchor text) to the LLM, asking it to classify them into categories and return a `priority_order`.

**Categories returned:**
- `team_links`: Staff profiles, team listings
- `about_links`: Company info, history
- `service_links`: Service descriptions
- `contact_links`: Contact pages
- `priority_order`: All URLs in recommended crawl order
- `reasoning`: LLM's explanation

**Fallback**: If the LLM call fails, `_fallback_analysis()` uses keyword matching on URL + anchor text:
- Team: team, people, staff, partner, director, our, meet, profile
- About: about, company, story, mission, overview, history
- Service: service, what we do, expertise
- Contact: contact, reach, get in touch

**To change link prioritization**: Edit the system prompt in `_get_system_prompt()` or the keywords in `_fallback_analysis()`.

---

### `src/llm_extractor.py` -- LLM extraction (the important one)

This is where the extraction prompts live. Key class: `LLMExtractor`.

**Two key methods that control output quality:**

- **`_get_system_prompt()`**: Defines the LLM's role, what to extract, how to format it, and the expected JSON schema. This is the single most impactful thing you can change.

- **`_build_extraction_prompt()`**: Constructs the per-URL user prompt with the crawled content. Truncates to 25,000 chars.

**The system prompt instructs the LLM to:**
- Extract company name, phones, emails, location, factual description
- Find decision makers (Partner, Director, Principal, etc.) -- permissive on titles, excludes juniors
- Write a pipe-separated `edited_description` with factual firmographic data for cold calling
- Write a `decision_maker_summary` with factual bullet points per DM (qualifications, years, responsibilities)
- Classify business segment (one of 5 categories)
- Normalize phone numbers to E.164
- Flag `out_of_scope` if not an accounting firm
- Report `confidence_score` (0.0 to 1.0)

**Current response format**: `json_object` (not strict JSON Schema). The LLM returns freeform JSON which is then validated by Pydantic. This means the LLM can occasionally return unexpected structures -- the `LLMExtractionResult` validators handle common deviations (e.g., `organisational_structure` as a dict instead of string).

**Phone normalization**: After LLM extraction, `normalize_llm_output()` runs all phone numbers through `phone_utils.normalize_to_e164()`.

**To change what gets extracted**: Edit the system prompt and JSON schema in `_get_system_prompt()`. Update `schemas.py` if you add/remove fields, and `export.py` for CSV mapping.

**To change the tone/style of descriptions**: Edit the `EDITED_DESCRIPTION` and `DECISION_MAKER_SUMMARY` sections in the system prompt.

---

### `src/adaptive_processor.py` -- Orchestrator

Key class: `AdaptiveScraperProcessor`. Ties everything together.

**Per-URL flow:**
1. Validate URL format (`_is_valid_url()`)
2. Crawl with `_crawl_with_fallback()` -- tries configured strategy first, falls back to `main_only`
3. Extract with `llm_extractor.extract()` -- combines main page + sub-page content
4. Normalize phone numbers
5. Categorize: success / out_of_scope / low_confidence
6. Update checkpoint

**Batch processing:**
- Concurrency controlled by `asyncio.Semaphore(settings.max_concurrent_crawls)`
- Rate limiting via `asyncio.sleep(settings.delay_between_requests)` between URLs
- Real-time progress tracking via `ProgressTracker`
- Results accumulated and returned as tuple: `(successful, out_of_scope, low_confidence, broken_urls)`

---

### `src/checkpoint_manager.py` -- Resume support

Key class: `CheckpointManager`.

**How it works:**
- Saves to `data/state/{checkpoint_name}_{timestamp}.json` every 10 URLs (configurable via `checkpoint_interval`).
- The checkpoint file contains: all URLs, processed URLs, results (as dicts), current index, timestamps.
- On resume: loads checkpoint, skips already-processed URLs, merges new results with accumulated ones at export time.

**Checkpoint JSON structure:**
```json
{
  "session_id": "20260114_085841",
  "total_urls": 100,
  "urls": ["https://..."],
  "processed_urls": ["https://..."],
  "successful_urls": ["https://..."],
  "out_of_scope_urls": [],
  "low_confidence_urls": [],
  "broken_urls": [],
  "results": [{"company_name": "...", ...}],
  "out_of_scope_records": [],
  "low_confidence_records": [],
  "current_index": 45,
  "start_time": "2026-01-14T08:58:41",
  "last_update": "2026-01-14T09:15:22",
  "completed": false
}
```

**Static utility methods:**
- `CheckpointManager.list_checkpoints()` -- list all checkpoints in `data/state/`
- `CheckpointManager.get_latest_checkpoint()` -- get the most recent one
- `CheckpointManager.load_checkpoint(path)` -- load a specific checkpoint

**To change checkpoint frequency**: Edit `self.checkpoint_interval` in the constructor (default: 10).

---

### `src/export.py` -- CSV output

Key class: `CSVExporter`.

Flattens `CompanyData` objects into CSV rows. Decision makers are expanded into numbered columns (`dm_1_name`, `dm_1_title`, ... `dm_3_linkedin`) -- up to 3 DMs per row.

**Output files:**
- `results.csv`: Successful extractions (the main output)
- `out_of_scope_urls.csv`: Firms flagged as not accounting practices
- `low_confidence_urls.csv`: Extractions below the confidence threshold
- `broken_urls.txt`: URLs that couldn't be crawled (plain text, one per line)

**To change CSV columns**: Edit `_company_to_dict()` in `export.py`.

---

### `src/phone_utils.py` -- Phone normalization

Normalizes Australian (+61), New Zealand (+64), and UK (+44) phone numbers to E.164 format using the `phonenumbers` library. Also classifies numbers as mobile vs office based on number ranges.

Key functions:
- `normalize_to_e164(phone, default_country)` -- main entry point
- `classify_phone_type(phone)` -- returns "mobile" or "office"
- `extract_phone_numbers(text, default_country)` -- finds phones in free text

---

### `src/failure_classifier.py` -- Error categorization

Key class: `FailureClassifier` with a static `classify(url, error)` method.

Classifies crawl failures using regex pattern matching on error messages:

| Category | Severity | Typical cause |
|----------|----------|---------------|
| `dns` | permanent | Domain doesn't exist |
| `connection` | temporary | Server down |
| `timeout` | temporary | Slow site |
| `ssl` | temporary | Expired certificate |
| `403_forbidden` | unknown | Bot protection |
| `404_not_found` | permanent | Page doesn't exist |
| `429_rate_limited` | temporary | Rate limited |
| `redirect_loop` | permanent | Misconfigured site |
| `maintenance` | temporary | Site in maintenance |
| `unknown` | unknown | Catch-all |

---

### `src/progress_tracker.py` -- Progress display

Key class: `ProgressTracker`. Tracks URLs processed, success/failure counts, calculates speed (URLs/min) and ETA. Logs every 10 URLs.

---

### `src/logger.py` -- Logging

Uses `structlog` for JSON-formatted logging if available, falls back to stdlib `logging` otherwise.

Creates separate log files at runtime:
- `data/logs/scraper.log` -- all messages
- `data/logs/errors.log` -- errors only

Helper functions for structured event logging:
- `log_crawl_start()`, `log_crawl_success()`, `log_crawl_failure()`
- `log_llm_extraction()`
- `log_http_fallback()`

---

## Output CSV columns

### `results.csv`

| Column | Description | Example |
|--------|-------------|---------|
| `company_name` | Firm name | "Smith & Associates" |
| `company_url` | Source URL | "https://example.com" |
| `office_phone` | Office phone (E.164) | "+61298765432" |
| `office_email` | Office email | "info@example.com" |
| `associated_emails` | Other emails (`;`-separated) | "john@ex.com; jane@ex.com" |
| `associated_mobile_numbers` | Mobiles (`;`-separated) | "+61412345678" |
| `associated_info` | Memberships, software, niches | "CAANZ. Xero, MYOB." |
| `associated_location` | Address/suburb | "Dee Why, NSW" |
| `organisational_structure` | Firm structure summary | "3 partners, 12 staff" |
| `team` | Team description | "15 staff including 3 partners" |
| `description` | Factual company description | "Tax and SMSF services..." |
| `edited_description` | Pipe-separated firmographic brief | "Dee Why NSW \| Tax, SMSF \| Xero" |
| `business_segment` | One of 5 segments | "General Accounting (Including Tax)" |
| `confidence_score` | 0.0--1.0 | 0.85 |
| `dm_1_name` | Decision maker 1 name | "John Smith" |
| `dm_1_title` | DM1 title | "Partner" |
| `dm_1_decision_maker_summary` | DM1 factual profile | "CA, CPA. 12 yrs. Ex-PwC." |
| `dm_1_phone_office` | DM1 office phone | "+61298765432" |
| `dm_1_phone_mobile` | DM1 mobile | "+61412345678" |
| `dm_1_phone_direct` | DM1 direct line | "" |
| `dm_1_email` | DM1 email | "john@example.com" |
| `dm_1_linkedin` | DM1 LinkedIn | "https://linkedin.com/in/..." |
| `dm_2_*` | Same fields for DM2 | |
| `dm_3_*` | Same fields for DM3 | |

### `out_of_scope_urls.csv`

| Column | Description |
|--------|-------------|
| `company_url` | URL flagged as out of scope |
| `company_name` | Company name (if found) |
| `reason` | Why it was flagged |
| `confidence_score` | Confidence in the classification |

### `low_confidence_urls.csv`

| Column | Description |
|--------|-------------|
| `company_url` | URL with low confidence |
| `company_name` | Company name (if found) |
| `confidence_score` | Score (below threshold) |
| `reason` | Reason for low confidence |

---

## Common tasks

### Add a new field to extraction

1. Add the field to `CompanyData` in `src/schemas.py`
2. Add the same field to `LLMExtractionResult` in `src/schemas.py`
3. Add it to the JSON schema in `_get_system_prompt()` in `src/llm_extractor.py`
4. Map it in `_company_to_dict()` in `src/export.py`

### Change the LLM model

Edit `OPENROUTER_MODEL` in `.env`. Browse models at https://openrouter.ai/models. The model must support `response_format: {"type": "json_object"}`.

### Change extraction quality/style

Edit `_get_system_prompt()` in `src/llm_extractor.py`. This is the single most impactful change you can make. The current prompt is tuned for factual firmographic data (no marketing language). Key sections to modify:
- `EDITED_DESCRIPTION`: Controls the pipe-separated brief format
- `DECISION_MAKER_SUMMARY`: Controls the per-DM profile format
- `ASSOCIATED_INFO`: Controls what supplementary data gets captured

### Process a different vertical (not accounting)

1. Update the system prompt in `llm_extractor.py` to reference the new vertical
2. Update `BusinessSegment` in `schemas.py` with new segment categories
3. Update keywords in `link_analyzer.py` `_fallback_analysis()` if relevant page patterns differ
4. Update the out-of-scope detection rules in the system prompt

### Debug a specific URL

```bash
python3 scripts/test_adaptive.py --url https://example.com --strategy adaptive --max-pages 5
```

This processes one URL without checkpointing and prints detailed results to stdout.

### Check progress of a running batch

Look at the latest JSON file in `data/state/` -- it contains all progress stats, processed URLs, and accumulated results. The `current_index` and `total_urls` fields show progress.

### Compare crawl strategies

```bash
python3 scripts/test_adaptive.py --compare --sample-count 10
```

Runs all three strategies (main_only, adaptive, greedy) on the same URLs and prints a comparison table.

---

## Dependencies

```
pydantic>=2.0.0
pydantic-settings>=2.0.0
phonenumbers
openai>=1.0.0
pandas>=2.0.0
crawl4ai>=0.7.0
structlog>=24.0.0
python-dotenv
```

Requires **Python 3.10+** (for async/await, `match` statements, type unions).

Also requires **Playwright Chromium** for `crawl4ai`:
```bash
playwright install chromium
```

---

## Relationship to the `scraper/` project

There is a separate `scraper/` directory at the project root that contains a newer, purpose-built pipeline for scraping `accountantlist.com.au` and enriching Attio records. That pipeline reuses concepts from this codebase (phone normalization, crawl4ai, LLM extraction) but has a different architecture:
- **Phase 1**: Directory harvest (httpx + BeautifulSoup, no browser needed)
- **Phase 2**: Website enrichment (crawl4ai + strict JSON Schema structured output)
- **Phase 3**: Attio deduplication and Attio-ready CSV export

The two codebases are independent -- changes to one don't affect the other. The `scraper/` pipeline uses `x-ai/grok-4.1-fast` with strict JSON Schema output instead of freeform `json_object` mode.
