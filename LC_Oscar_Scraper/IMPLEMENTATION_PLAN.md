# Accounting Website Scraper - Implementation Plan

## Overview
Scrape 1000s of accounting firm websites (AU/NZ/UK) to extract structured CRM-ready data including decision makers, contact details, and business segmentation.

## Key Requirements
- Extract phone numbers (mobile priority), emails, team details for decision makers
- Normalize to E.164 format (AU: +61, NZ: +64, UK: +44)
- Identify business segments: General Accounting, Tax Specialist, Bookkeeping, Other Accounting, Other Tax
- Handle broken/out-of-scope URLs (isolate for manual review)
- Permissive decision maker identification (senior-sounding titles)
- CSV output compatible with Attio CRM

---

## Technology Stack

| Component | Library | Purpose |
|-----------|---------|---------|
| Crawling | crawl4ai | Async crawling, JS handling, content cleaning |
| LLM | MiMo-V2-Flash (OpenRouter free) | Semantic extraction, decision maker ID |
| Schema | Pydantic | Validation, structure, enforcement |
| Phone Normalization | phonenumbers | E.164 parsing/formatting |
| Config | pydantic-settings | Environment-based settings |
| Output | pandas + CSV | Attio-ready export |

---

## Pipeline Flow

```
URLs → Error Check → crawl4ai (clean + discover bio pages)
→ MiMo-V2-Flash (extract + classify + segment)
→ Pydantic validation + E.164 normalization
→ CSV export (successful) + Error lists (broken/out-of-scope)
```

---

## Business Segments

1. **General Accounting (Including Tax)** - Full-service firms doing tax
2. **Tax Specialist** - Tax-focused specialists
3. **Bookkeeping (No Income Tax)** - Bookkeeping only, no tax services
4. **Other Accounting (No Tax)** - Other accounting services excluding tax
5. **Other Tax** - Tax-related but not traditional accounting firm

---

## Error Handling Strategy

### Broken/Malformed URLs
- Connection timeouts, DNS failures, HTTP 404/500
- Site taken down (hosting errors)
- Malformed URLs (invalid format)
- **Output**: `data/output/broken_urls.txt`

### Out-of-Scope URLs
- Obvious: non-accounting businesses (grass trimming, retail, etc.)
- Borderline: accounting software, fintech, advisory-only firms
- **Output**: `data/output/out_of_scope_urls.csv` (URL, company_name, reason, confidence_score)

### Low Confidence Extractions
- LLM confidence < 0.5
- Missing critical fields (company_name)
- **Output**: `data/output/low_confidence_urls.csv`

---

## Project Structure

```
LC_Official_Scraper/
├── src/
│   ├── __init__.py
│   ├── config.py              # Pydantic Settings
│   ├── schemas.py             # Data models (CompanyData, DecisionMaker)
│   ├── phone_utils.py         # E.164 normalization
│   ├── crawler.py             # crawl4ai wrapper
│   ├── llm_extractor.py       # OpenRouter/LLM integration
│   ├── processor.py           # Orchestration (main logic)
│   └── export.py              # CSV export, error lists
├── scripts/
│   ├── run_scraper.py         # Main execution script
│   └── test_batch.py          # Test with sample URLs
├── tests/
│   ├── __init__.py
│   ├── test_phone_utils.py    # Unit tests
│   └── sample_urls.txt        # 60 sample URLs
├── data/
│   ├── input/
│   │   └── urls.txt           # Full URL list
│   └── output/                # Generated files
│       ├── results.csv        # Successful extractions
│       ├── broken_urls.txt    # Failed crawls
│       ├── out_of_scope_urls.csv
│       └── low_confidence_urls.csv
├── requirements.txt
├── .env.example
├── .env                       # Create from .env.example
├── README.md
└── IMPLEMENTATION_PLAN.md
```

---

## CSV Schema (Attio-Ready)

### Successful Extractions (results.csv)

| Column | Description | Example |
|--------|-------------|---------|
| `company_name` | Company name | "McGuire & Associates" |
| `company_url` | Source URL | "https://example.com" |
| `office_phone` | Main company phone (E.164) | "+61-2-1234-5678" |
| `office_email` | General company email | "info@example.com" |
| `associated_emails` | All emails (semicolon-delimited) | "info@...; contact@...; john@..." |
| `associated_mobile_numbers` | All mobiles (E.164, semicolon) | "+61-412-345-678; +61-423-456-789" |
| `associated_info` | Raw supplementary data | "LinkedIn: ..., Address: ..." |
| `associated_location` | Office location(s) | "Sydney, NSW" |
| `organisational_structure` | Hierarchy summary | "Partners: 3, Directors: 2, Senior: 5" |
| `team` | Full team info | "John (Partner), Jane (Director)..." |
| `description` | Company description | "Full-service accounting firm..." |
| `business_segment` | Business category | "General Accounting (Including Tax)" |
| `confidence_score` | LLM confidence (0-1) | 0.87 |
| `dm_1_name`, `dm_1_title`, `dm_1_phone_office`, `dm_1_phone_mobile`, `dm_1_phone_direct`, `dm_1_email`, `dm_1_linkedin` | Decision maker 1 | |
| `dm_2_...` | Decision maker 2 | |
| `dm_3_...` | Decision maker 3 | |

### Out-of-Scope URLs (out_of_scope_urls.csv)

| Column | Description |
|--------|-------------|
| `company_url` | URL that was out of scope |
| `company_name` | Extracted name if available |
| `reason` | Why out of scope |
| `confidence_score` | LLM confidence |

---

## Implementation Details

### 1. Configuration (src/config.py)

```python
from pydantic import BaseModel
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # OpenRouter
    openrouter_api_key: str
    openrouter_model: str = "xiaomi/mimo-v2-flash:free"

    # Crawl4AI
    max_concurrent_crawls: int = 5
    page_timeout: int = 30000

    # Phone normalization
    default_country: str = "AU"

    # LLM
    max_decision_makers: int = 3
    llm_temperature: float = 0.0
    reasoning_enabled: bool = False

    # Output
    output_dir: str = "data/output"

    class Config:
        env_file = ".env"
```

### 2. Business Segmentation (LLM Task)

The LLM must classify each website into one of 5 segments based on:
- Services offered (tax, bookkeeping, advisory)
- Firm positioning
- Client mentions
- Description and team profiles

### 3. crawl4ai Configuration

```python
browser_config = BrowserConfig(
    headless=True,
    viewport_width=1920,
    viewport_height=1080,
)

crawler_config = CrawlerRunConfig(
    page_timeout=30000,
    remove_overlay_elements=True,
    excluded_tags=["nav", "footer", "aside", "header"],
    remove_forms=True,
)
```

### 4. Error Detection

Broken URLs:
- ConnectionError, TimeoutError
- HTTP status codes >= 400
- Empty/invalid HTML response
- crawl4ai result.success = False

Out-of-scope:
- LLM classification + explicit "out_of_scope" flag
- Non-accounting keywords detected
- Software/vendor language (not service provider)

---

## Testing Strategy

### Phase 1: Unit Tests
- Phone normalization for AU/NZ/UK formats
- Pydantic schema validation
- E.164 format validation

### Phase 2: Sample URL Testing (60 URLs)
1. Run: `python scripts/test_batch.py`
2. Review:
   - Accuracy of decision maker identification
   - Business segment classification
   - Phone normalization
   - Error detection (broken/out-of-scope)
3. Adjust prompts based on results

### Phase 3: Full Batch Processing
1. Load all URLs to `data/input/urls.txt`
2. Run: `python scripts/run_scraper.py`
3. Monitor logs, check error lists
4. Review `results.csv` before Attio import

---

## LLM Prompts (Key Instructions)

### System Prompt

```
You are an expert at analyzing accounting firm websites.

Tasks:
1. Extract company information (name, phone, email, location, description)
2. Identify decision makers (senior-sounding titles: Partner, Principal, Director, etc.)
3. Classify business segment:
   - General Accounting (Including Tax): Full-service accounting with tax
   - Tax Specialist: Tax-focused specialists
   - Bookkeeping (No Income Tax): Bookkeeping only, no tax services
   - Other Accounting (No Tax): Other accounting excluding tax
   - Other Tax: Tax-related but not traditional accounting firm
4. Identify if out of scope:
   - Obvious: Non-accounting businesses
   - Borderline: Accounting software, fintech, advisory-only firms
5. Normalize phone numbers to E.164 format (+61 AU, +64 NZ, +44 UK)
6. Extract contact details for decision makers
7. Assess confidence (0.0 to 1.0)

Be permissive with decision makers, accurate with segments.
Return valid JSON matching the provided schema.
```

### User Prompt (simplified)

```
Extract and classify this accounting firm website:

URL: {url}

Content:
{all_content}

Extract:
1. Company details
2. Business segment (5 options)
3. Out-of-scope detection (yes/no + reason)
4. Decision makers (up to 3) with contact details
5. All emails and mobile numbers
6. Organizational structure and team
7. Confidence score

Return valid JSON.
```

---

## Known Challenges & Mitigations

| Challenge | Mitigation |
|-----------|------------|
| Rate limiting on OpenRouter free tier | Test limits, implement delays, batch processing |
| Diverse team page layouts | LLM semantic extraction vs rigid patterns |
| Phone attribution (person vs company) | LLM context linking + pattern classification |
| Slow JavaScript sites | Increased timeouts, wait_for conditions |
| Bot detection | Conservative concurrency, delays between requests |
| Out-of-scope detection | LLM classification + confidence thresholds |
| Broken URL isolation | Comprehensive error handling + separate files |

---

## Next Steps

1. Create project structure and all files
2. Install: `pip install -r requirements.txt`
3. Configure: Copy `.env.example` to `.env`, add OpenRouter API key
4. Prepare test URLs: Already provided (60 URLs)
5. Run tests: `pytest tests/`
6. Run test batch: `python scripts/test_batch.py`
7. Review results, adjust prompts if needed
8. Full deployment: Load all URLs, run `python scripts/run_scraper.py`
9. Import to Attio: Upload CSV files

---

## Appendix: crawl4ai Integration

### Why crawl4ai is ideal:
- Async crawling with `arun_many()` for concurrent processing
- Content cleaning via `excluded_tags` removes nav/footer/marketing noise
- Link discovery for bio page navigation
- Markdown generation for LLM-friendly output
- JavaScript handling for modern SPA accounting sites
- Session management if authentication ever needed

### Key features used:
- `AsyncWebCrawler` - Main crawler with async support
- `BrowserConfig` - Browser settings (headless, viewport)
- `CrawlerRunConfig` - Crawl behavior (timeout, exclusions)
- `arun_many()` - Concurrent URL processing
- `links["internal"]` - Link discovery for bio pages
- `result.markdown` - Clean markdown for LLM
