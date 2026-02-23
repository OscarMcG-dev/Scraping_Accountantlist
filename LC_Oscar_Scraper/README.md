# Accounting Website Scraper

A Python-based web scraper designed to extract structured data from accounting firm websites in Australia, New Zealand, and the UK.

## Features

- **Async Crawling**: Uses crawl4ai for efficient, concurrent processing of 1000s of URLs
- **Semantic Extraction**: MiMo-V2-Flash LLM via OpenRouter for intelligent data extraction
- **Business Segmentation**: Automatically classifies firms into 5 business segments
- **Decision Maker Identification**: Extracts contact details for key decision makers
- **Phone Normalization**: Converts all phone numbers to E.164 format (AU: +61, NZ: +64, UK: +44)
- **Error Handling**: Isolates broken, out-of-scope, and low-confidence URLs
- **Attio-Ready CSV**: Exports data compatible with Attio CRM import

## Business Segments

1. **General Accounting (Including Tax)** - Full-service accounting firms
2. **Tax Specialist** - Tax-focused specialists
3. **Bookkeeping (No Income Tax)** - Bookkeeping only
4. **Other Accounting (No Tax)** - Other accounting services excluding tax
5. **Other Tax** - Tax-related but not traditional accounting firms

## Installation

### 1. Clone the repository

```bash
cd LC_Official_Scraper
```

### 2. Create virtual environment (recommended)

```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and add your OpenRouter API key:

```bash
OPENROUTER_API_KEY=your_actual_api_key_here
```

Get your free API key from [OpenRouter](https://openrouter.ai/)

## Usage

### Testing with Sample URLs

Run the test script with the 60 sample URLs provided:

```bash
python scripts/test_batch.py
```

This will:
- Process the sample URLs from `tests/sample_urls.txt`
- Generate test results in `data/output/`
- Show summary statistics and examples

### Processing Full URL List

1. Place your URLs in `data/input/urls.txt` (one URL per line)

2. Run the main scraper:

```bash
python scripts/run_scraper.py
```

3. Check the output files in `data/output/`:
   - `results.csv` - Successful extractions (import to Attio)
   - `out_of_scope_urls.csv` - URLs outside target market
   - `low_confidence_urls.csv` - Low confidence extractions
   - `broken_urls.txt` - Failed crawls for manual review

## Output Format

### results.csv (Attio-Ready)

| Column | Description |
|--------|-------------|
| `company_name` | Company name |
| `company_url` | Source URL |
| `office_phone` | Main company phone (E.164) |
| `office_email` | General company email |
| `associated_emails` | All emails (semicolon-delimited) |
| `associated_mobile_numbers` | All mobiles (E.164, semicolon) |
| `associated_info` | Supplementary data |
| `associated_location` | Office location(s) |
| `organisational_structure` | Hierarchy summary |
| `team` | Full team info |
| `description` | Company description |
| `business_segment` | Business category |
| `confidence_score` | LLM confidence (0-1) |
| `dm_1_name`, `dm_1_title`, `dm_1_phone_*`, `dm_1_email`, `dm_1_linkedin` | Decision maker 1 |
| `dm_2_...` | Decision maker 2 |
| `dm_3_...` | Decision maker 3 |

## Project Structure

```
LC_Official_Scraper/
├── src/
│   ├── config.py              # Settings from environment
│   ├── schemas.py             # Pydantic models
│   ├── phone_utils.py         # E.164 normalization
│   ├── crawler.py             # crawl4ai wrapper
│   ├── llm_extractor.py       # OpenRouter integration
│   ├── processor.py           # Main orchestration
│   └── export.py              # CSV export
├── scripts/
│   ├── run_scraper.py         # Main execution
│   └── test_batch.py          # Test script
├── tests/
│   ├── test_phone_utils.py    # Unit tests
│   └── sample_urls.txt        # 60 sample URLs
├── data/
│   ├── input/
│   │   └── urls.txt           # Full URL list
│   └── output/                # Generated files
├── requirements.txt
├── .env.example
├── .env                       # Create from .env.example
└── README.md
```

## Configuration

Edit `.env` to customize behavior:

```bash
# Crawler settings
MAX_CONCURRENT_CRAWLS=5          # Concurrent URL processing
PAGE_TIMEOUT=30000               # Page load timeout (ms)

# LLM settings
MAX_DECISION_MAKERS=3           # Max DMs per company
LLM_TEMPERATURE=0.0             # 0.0 = deterministic, 1.0 = creative
REASONING_ENABLED=false          # MiMo-V2-Flash reasoning mode

# Phone normalization
DEFAULT_COUNTRY=AU               # Default for parsing (AU/NZ/UK)

# Confidence threshold
MIN_CONFIDENCE_THRESHOLD=0.5     # Below this → low_confidence_urls.csv

# Rate limiting
DELAY_BETWEEN_REQUESTS=1.0      # Seconds between requests
```

## Technology Stack

| Component | Library | Purpose |
|-----------|---------|---------|
| Crawling | crawl4ai | Async crawling, JS handling |
| LLM | MiMo-V2-Flash (OpenRouter) | Semantic extraction |
| Schema | Pydantic | Validation |
| Phone Normalization | phonenumbers | E.164 parsing |
| Config | pydantic-settings | Environment settings |
| Output | pandas + CSV | Attio export |

## Troubleshooting

### OpenRouter Rate Limiting

If you hit rate limits on the free tier:
- Increase `DELAY_BETWEEN_REQUESTS` in `.env`
- Reduce `MAX_CONCURRENT_CRAWLS`
- Process URLs in smaller batches

### Slow Loading Sites

If some sites are timing out:
- Increase `PAGE_TIMEOUT` (default: 30000ms = 30s)
- Sites with heavy JavaScript may need 60s+

### Low Confidence Scores

If most results go to `low_confidence_urls.csv`:
- Lower `MIN_CONFIDENCE_THRESHOLD` to 0.3
- Check if sites are actually accounting firms (may need out-of-scope tuning)

### Broken URLs

Check `broken_urls.txt` for:
- Sites that are down
- Invalid URL formats
- Timeout/connection errors

Review manually and update if needed.

## Logs

- `scraper.log` - Main scraper logs
- `test_scraper.log` - Test run logs

Check logs for:
- Error messages
- Warnings about failed crawls
- LLM extraction issues

## Testing

Run unit tests:

```bash
pytest tests/
```

Test phone number normalization:

```bash
pytest tests/test_phone_utils.py -v
```

## License

This is a custom project for accounting firm data extraction.

## Support

For issues or questions, check the `IMPLEMENTATION_PLAN.md` for detailed architecture documentation.
