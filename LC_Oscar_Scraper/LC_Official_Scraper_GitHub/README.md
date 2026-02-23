# LC Official Scraper

Intelligent web scraper for Australian accounting firms with LLM-guided adaptive crawling and checkpoint/resume capabilities.

## Features

- **LLM-Guided Crawling**: Uses semantic understanding to find relevant pages (team, about, services)
- **Automatic Fallback**: Falls back to keyword-based discovery if LLM times out
- **Checkpoint System**: Save/restore crawl progress - resume from interruptions
- **Progress Tracking**: Real-time progress with ETA and speed statistics
- **HTTP/HTTPS Fallback**: Automatic protocol switching
- **Sales-Ready Output**: CSV with decision maker summaries and value propositions

## Installation

```bash
# Install dependencies
pip3 install -r requirements.txt

# Configure API key
cp .env.example .env
# Edit .env and add your OpenRouter API key
```

## Usage

### Run New Batch

```bash
python3 scripts/run_scraper_adaptive.py \
  --batch-file data/input/urls.txt \
  --strategy adaptive \
  --max-pages 3
```

### Resume from Checkpoint

```bash
# Resume from latest checkpoint
python3 scripts/run_scraper_adaptive.py --resume

# Resume from specific checkpoint
python3 scripts/run_scraper_adaptive.py --resume-from data/state/batch_20260106_120000.json
```

### CLI Options

| Option | Description | Default |
|---------|-------------|----------|
| `--batch-file` | Path to URLs file (one per line) | `data/input/urls.txt` |
| `--strategy` | `adaptive`, `greedy`, or `main_only` | `adaptive` |
| `--max-pages` | Max pages per website | 5 |
| `--checkpoint-name` | Name for checkpoint batch | `batch` |
| `--resume` | Resume from latest checkpoint | false |
| `--resume-from` | Resume from specific checkpoint file | - |
| `--skip-progress` | Disable progress tracking | false |

## Crawl Strategies

### Adaptive (Recommended)
- Uses LLM to analyze and prioritize links
- Finds team/people pages intelligently
- Includes semantic understanding of navigation
- Falls back to main_only on timeout

### Main_Only
- Fastest option (no LLM call)
- Uses keyword matching
- Best for simple websites

### Greedy
- Crawls all internal links
- Most thorough but slowest
- Use for comprehensive scraping

## Output Files

| File | Description |
|-------|-------------|
| `data/output/results.csv` | Successful extractions |
| `data/output/out_of_scope.csv` | Non-accounting sites |
| `data/output/low_confidence.csv` | Low confidence results |
| `data/output/broken_urls.txt` | Failed URLs |

## Checkpoint System

Checkpoints are saved to `data/state/`:

- Saved every 10 URLs processed
- Contains: URLs, results, progress, timestamps
- Can resume from any checkpoint
- Lists available with `--resume` flag

## Configuration

Edit `.env`:

```bash
# OpenRouter API
OPENROUTER_API_KEY=sk-or-v1-your-key
OPENROUTER_MODEL=xiaomi/mimo-v2-flash:free

# Crawler Settings
PAGE_TIMEOUT=90000              # Page timeout in ms
MAX_CONCURRENT_CRAWLS=10         # Parallel crawls
MAX_PAGES=3                    # Pages per website

# LLM Settings
MAX_DECISION_MAKERS=3
LLM_TEMPERATURE=0.0

# Confidence Threshold
MIN_CONFIDENCE_THRESHOLD=0.5
```

## Architecture

```
LC_Official_Scraper/
├── src/
│   ├── adaptive_crawler.py      # LLM-guided crawler
│   ├── adaptive_processor.py     # Main processor with fallback
│   ├── llm_extractor.py         # Company data extraction
│   ├── link_analyzer.py         # Link analysis (20 links max)
│   ├── failure_classifier.py     # Categorize failures
│   ├── checkpoint_manager.py     # Save/restore progress
│   ├── progress_tracker.py       # Real-time progress
│   ├── export.py               # CSV export
│   ├── schemas.py              # Pydantic models
│   ├── config.py               # Settings with validation
│   └── logger.py              # Structured logging
├── scripts/
│   └── run_scraper_adaptive.py  # Main CLI
├── data/
│   ├── input/                  # URL files
│   ├── output/                 # Results CSVs
│   ├── state/                  # Checkpoints
│   └── logs/                   # Structured logs
└── requirements.txt
```

## Performance

| Strategy | Avg Time | Speed |
|----------|-----------|--------|
| Adaptive | 15-30s/URL | 2-3 URLs/min |
| Main_Only | 5-10s/URL | 5-10 URLs/min |
| Greedy | 30-60s/URL | 1-2 URLs/min |

## Troubleshooting

### Timeout on Slow Sites
Automatic fallback to `main_only` strategy ensures data is still extracted.

### Checkpoint Resume
Use `--resume` to continue from last checkpoint after interruption.

### Low Confidence Results
Check `data/output/low_confidence.csv` and adjust `MIN_CONFIDENCE_THRESHOLD`.

## Requirements

- Python 3.10+
- OpenRouter API key (free tier available)
- crawl4ai
- openai (for OpenRouter)
- pydantic
- structlog

## License

Proprietary - LC Official
