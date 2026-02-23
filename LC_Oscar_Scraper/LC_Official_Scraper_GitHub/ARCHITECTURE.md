# LC Official Scraper - Architecture & Design Documentation

A production-ready web scraper for accounting firms in Australia, New Zealand, and the UK, featuring intelligent LLM-guided crawling and comprehensive data extraction.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Core Components](#core-components)
3. [Data Flow](#data-flow)
4. [Design Decisions](#design-decisions)
5. [Current Features](#current-features)
6. [Configuration](#configuration)
7. [Usage](#usage)
8. [Project Structure](#project-structure)
9. [Recent Improvements](#recent-improvements)

---

## Architecture Overview

The scraper uses a **layered architecture** that separates concerns across crawling, extraction, validation, and export:

```
┌─────────────────────────────────────────────────────────────┐
│                     CLI Entry Point                     │
│              (run_scraper_adaptive.py)                     │
└─────────────────────────┬───────────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────────────┐
│          AdaptiveScraperProcessor (Orchestrator)           │
│  • Batch processing with concurrency control                │
│  • Checkpoint/resume management                             │
│  • Progress tracking                                       │
└─────────────┬─────────────────┬───────────────────────┘
              │                 │
┌─────────────▼─────────┐ ┌─▼─────────────────────┐
│ AdaptiveWebsiteCrawler  │ │   LLMExtractor        │
│ (Intelligent crawling)  │ │ (Data extraction)    │
└──────────┬─────────────────┘ └──┬───────────────────┘
       │                      │
┌──────────▼──────────┐    ┌─────▼─────────────┐
│  LinkAnalyzer       │    │  Settings/Config   │
│  (LLM-guided         │    │  (Configuration)   │
│  discovery)          │    └──────────────────┘
└─────────────────────────┘
```

### Architecture Principles

1. **Separation of Concerns**: Each module has a single responsibility
2. **Async-First**: All I/O operations use asyncio for concurrency
3. **Fault Tolerance**: Fallback strategies at multiple levels
4. **Resumability**: State persistence for long-running jobs
5. **Explicit Public APIs**: `__all__` exports define module boundaries

---

## Core Components

### 1. AdaptiveWebsiteCrawler (`src/adaptive_crawler.py`)

**Purpose**: Intelligent website crawling with semantic page discovery

**Key Features**:
- LLM-guided link classification (vs brittle URL patterns)
- Three crawling strategies: `adaptive`, `greedy`, `main_only`
- HTTP/HTTPS fallback for accessibility
- Configurable concurrency for sub-page crawling
- Navigation-aware link extraction (keeps nav/footer for context)

**Crawling Strategies**:

| Strategy | Description | Use Case |
|----------|-------------|-----------|
| `adaptive` | LLM analyzes and prioritizes links by relevance | Default, handles complex sites |
| `greedy` | Crawls all discovered internal links | Comprehensive but slower |
| `main_only` | Only crawls main page | Fastest, minimal data |

**Dependencies**:
- `crawl4ai`: AsyncWebCrawler for JavaScript rendering
- `LinkAnalyzer`: For intelligent link prioritization

---

### 2. LinkAnalyzer (`src/link_analyzer.py`)

**Purpose**: Semantic classification of website links using LLM

**Key Features**:
- Analyzes up to 20 links per request (configurable via `max_links_for_llm_analysis`)
- Classifies links into: team/about/service/contact
- Provides priority order for crawling
- Fallback to pattern-based matching if LLM fails
- Retry logic with exponential backoff for transient failures

**Output Schema**:
```python
{
    "team_links": [...],      # Individual profiles/staff pages
    "about_links": [...],     # Company info/history
    "service_links": [...],   # Service descriptions
    "contact_links": [...],   # Contact forms/info
    "priority_order": [...]     # Optimal crawling order
    "reasoning": "..."        # LLM's rationale
}
```

---

### 3. LLMExtractor (`src/llm_extractor.py`)

**Purpose**: Structured data extraction from crawled content using LLM

**Key Features**:
- Extracts: company info, decision makers, business segment, sales insights
- Configurable content truncation (default: 25,000 chars via `max_content_length_for_llm`)
- Normalizes phone numbers to E.164 format
- Retry logic with exponential backoff
- Distinguishes validation failures from genuine empty results via `extraction_error` field

**Extraction Schema** (`LLMExtractionResult`):
```python
{
    # Company Information
    "company_name": str,
    "office_phone": str,          # E.164 format
    "office_email": str,
    "associated_emails": List[str],
    "associated_mobile_numbers": List[str],  # E.164 format
    "associated_location": str,
    "description": str,
    "edited_description": str,     # Sales-ready insights

    # Structure & Team
    "organisational_structure": str,
    "team": str,

    # Classification
    "business_segment": str,         # One of 5 categories
    "out_of_scope": bool,
    "out_of_scope_reason": str,

    # Decision Makers
    "decision_makers": [{
        "name": str,
        "title": str,
        "decision_maker_summary": str,  # Sales context
        "phone_office": str,
        "phone_mobile": str,
        "phone_direct": str,
        "email": str,
        "linkedin": str
    }],

    # Quality Metrics
    "confidence_score": float,      # 0.0 to 1.0
    "extraction_error": Optional[str] # If validation/LLM failed
}
```

---

### 4. AdaptiveScraperProcessor (`src/adaptive_processor.py`)

**Purpose**: Orchestrates batch processing with fault tolerance

**Key Features**:
- Concurrent processing with semaphore-based rate limiting
- Checkpoint/resume support for long-running jobs
- Progress tracking with ETA calculation
- Automatic fallback strategy (adaptive → main_only)
- Result categorization: successful/out_of_scope/low_confidence

**Processing Flow**:
1. Validates URL format
2. Attempts adaptive crawl (LLM-guided)
3. Falls back to main-only if adaptive fails
4. Extracts data with LLM
5. Normalizes phone numbers
6. Categorizes result based on confidence and scope

---

### 5. Supporting Modules

#### Schemas (`src/schemas.py`)
- Pydantic models for data validation
- BusinessSegment enum (5 categories)
- Phone/email validation with regex
- Type safety throughout pipeline

#### Configuration (`src/config.py`)
- Environment-based settings via pydantic-settings
- API key validation
- Configurable crawl parameters
- Shared OpenAI client factory via `get_openai_client()`

#### Export (`src/export.py`)
- CSV export for CRM import
- Separate files: results, out_of_scope, low_confidence, broken_urls
- Pandas-based for reliability

#### CheckpointManager (`src/checkpoint_manager.py`)
- JSON state persistence
- Resume from any interruption point
- Progress tracking across sessions
- Checkpoints every N URLs (configurable)

#### ProgressTracker (`src/progress_tracker.py`)
- Real-time progress logging
- ETA calculation
- Statistics (success rate, errors, etc.)

#### FailureClassifier (`src/failure_classifier.py`)
- Categorizes crawl failures with actionable insights
- HTTP status code analysis
- Timeout and connection error handling

#### Logger (`src/logger.py`)
- Structured JSON logging
- Separate log levels
- Timestamped entries

#### Phone Utils (`src/phone_utils.py`)
- E.164 normalization
- Country code inference (AU/NZ/UK)
- phonenumbers library for validation

---

## Data Flow

```
1. Load URLs from file
         │
         ▼
2. Initialize CheckpointManager (load or new)
         │
         ▼
3. For each URL (with concurrency control):
         │
         ├─► AdaptiveWebsiteCrawler.crawl_intelligently()
         │     ├─► Extract internal links
         │     ├─► LinkAnalyzer.analyze_links() [optional]
         │     └─► Crawl prioritized pages
         │
         ├─► LLMExtractor.extract()
         │     └─► Get structured data
         │
         ├─► Normalize phone numbers
         │
         └─► Categorize result (success/low_confidence/out_of_scope)
         │
         ├─► Update checkpoint
         └─► Update progress tracker
         │
         ▼
4. Export results to CSV (3 files)
         │
         ▼
5. Log summary statistics
```

---

## Design Decisions

### 1. LLM-Guided Crawling vs Hardcoded Patterns

**Decision**: Use LLM for link classification instead of regex patterns

**Rationale**:
- Accounting websites often use non-standard URL structures
- Semantic understanding of link text is more reliable than URL patterns
- Handles variations like `/our-people`, `/team`, `/staff`, `/meet-us`, etc.
- LLM can prioritize based on context (nav placement, text content)

**Trade-off**: Slower per-request, but higher success rate and data quality

---

### 2. Dependency Injection for OpenAI Client

**Decision**: Share single AsyncOpenAI client across components

**Rationale**:
- Reduces connection overhead (no per-component clients)
- Easier mocking for testing
- Consistent API behavior across components
- Better resource management

**Implementation**:
```python
# In AdaptiveScraperProcessor
llm_client = settings.get_openai_client()
self.crawler = AdaptiveWebsiteCrawler(settings, llm_client)
self.llm_extractor = LLMExtractor(settings, llm_client)
```

---

### 3. Retry Logic with Exponential Backoff

**Decision**: Implement retry for transient LLM API failures

**Rationale**:
- Rate limits and network issues are transient
- Exponential backoff reduces API pressure
- Existing `max_retries` (3) and `retry_delay` (2s) config was unused
- Improves reliability without user intervention

**Implementation**:
```python
async def retry_with_backoff(func, max_retries, retry_delay, *args, **kwargs):
    for attempt in range(max_retries + 1):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            if attempt < max_retries:
                delay = retry_delay * (2 ** attempt)  # Exponential
                await asyncio.sleep(delay)
            else:
                raise
```

---

### 4. Error Distinguishing via `extraction_error` Field

**Decision**: Add dedicated field to distinguish validation failures from empty data

**Rationale**:
- Previously returned `confidence_score=0.0` for both cases
- Now callers can distinguish between:
  - Site truly has no data (`extraction_error=None`)
  - Validation/LLM failed (`extraction_error` has value)
- Better debugging and error tracking

---

### 5. Magic Numbers in Configuration

**Decision**: Move hardcoded values to configurable settings

**Rationale**:
- Enables tuning without code changes
- Different environments can use different values
- Clear documentation of intent

**Migrated Values**:

| Setting | Default | Description |
|---------|---------|-------------|
| `max_links_for_llm_analysis` | 20 | Links to send to LLM |
| `max_links_for_basic_discovery` | 20 | Links for fallback discovery |
| `max_content_length_for_llm` | 25000 | Content chars for LLM |
| `sub_page_concurrency_limit` | 3 | Concurrent sub-page crawls |

---

### 6. Async Throughout

**Decision**: Use async/await for all I/O operations

**Rationale**:
- Network operations (crawling, LLM API) are inherently async
- Concurrent processing significantly improves throughput
- asyncio is Python's native async framework

**Concurrency Control**:
- Main processing: `max_concurrent_crawls` (default: 5)
- Sub-page crawling: `sub_page_concurrency_limit` (default: 3)
- Rate limiting: `delay_between_requests` (default: 1s)

---

### 7. Checkpoint/Resume Architecture

**Decision**: JSON-based state persistence with automatic checkpointing

**Rationale**:
- Long-running jobs may be interrupted
- Resume capability prevents wasted work
- Checkpointing every N URLs (default: 10)
- Accumulated results preserved across sessions

**Checkpoint Schema**:
```json
{
    "session_id": "uuid",
    "start_time": "ISO-8601",
    "total_urls": 100,
    "current_index": 45,
    "processed_urls": ["url1", ...],
    "successful": [...],  # Pydantic models
    "out_of_scope": [...],
    "low_confidence": [...],
    "broken": ["url1", ...],
    "completed": false
}
```

---

### 8. Fallback Strategy (Adaptive → Main Only)

**Decision**: Automatic fallback if adaptive strategy fails

**Rationale**:
- Some sites have navigation issues or complex JS
- Main page often contains essential data
- Prevents complete failure for problematic sites
- Graceful degradation of data quality

**Implementation**:
```python
try:
    crawl_result = await self.crawler.crawl_intelligently(url, strategy="adaptive")
except Exception:
    logger.warning(f"Adaptive crawl failed for {url}, trying main_only")
    crawl_result = await self.crawler.crawl_intelligently(url, strategy="main_only")
```

---

## Current Features

### ✅ Implemented

1. **LLM-Guided Intelligent Crawling**
   - Semantic link classification
   - Priority-based crawling
   - Handles non-standard URL patterns

2. **Structured Data Extraction**
   - Company information (name, phone, email, location)
   - Decision makers with contact details
   - Business segment classification
   - Sales-ready insights

3. **Robust Error Handling**
   - Retry logic with exponential backoff
   - HTTP/HTTPS fallback
   - Graceful degradation strategies
   - Error distinguishing (validation vs empty data)

4. **Production-Ready Features**
   - Checkpoint/resume support
   - Progress tracking with ETA
   - CSV export for CRM import
   - Concurrent processing with rate limiting
   - Structured JSON logging

5. **Phone Number Normalization**
   - E.164 format conversion
   - Country code inference (AU/NZ/UK)
   - Mobile vs landline handling

6. **Quality Control**
   - Confidence scoring (0.0 to 1.0)
   - Out-of-scope detection
   - Low-confidence separation
   - Validation error tracking

7. **Explicit Public APIs**
   - `__all__` exports in all modules
   - Clear module boundaries
   - Easier for external imports

8. **Shared Resource Management**
   - Single OpenAI client instance
   - Dependency injection pattern
   - Better resource utilization

---

## Configuration

### Environment Variables

```bash
# Required
OPENROUTER_API_KEY=sk-or-...

# Optional (with defaults)
OPENROUTER_MODEL=xiaomi/mimo-v2-flash:free
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
MAX_CONCURRENT_CRAWLS=5
PAGE_TIMEOUT=30000
DEFAULT_COUNTRY=AU
MAX_DECISION_MAKERS=3
LLM_TEMPERATURE=0.0
REASONING_ENABLED=false
MIN_CONFIDENCE_THRESHOLD=0.5
OUTPUT_DIR=data/output
MAX_RETRIES=3
RETRY_DELAY=2.0
DELAY_BETWEEN_REQUESTS=1.0
MAX_LINKS_FOR_LLM_ANALYSIS=20
MAX_LINKS_FOR_BASIC_DISCOVERY=20
MAX_CONTENT_LENGTH_FOR_LLM=25000
SUB_PAGE_CONCURRENCY_LIMIT=3
```

### Business Segment Categories

1. **General Accounting (Including Tax)** - Full-service accounting firms
2. **Tax Specialist** - Tax-focused firms (advisory/compliance)
3. **Bookkeeping (No Income Tax)** - Bookkeeping-only services
4. **Other Accounting (No Tax)** - Forensic, payroll, etc.
5. **Other Tax** - Tax software, return preparers, etc.

---

## Usage

### Basic Execution

```bash
# Default: adaptive strategy, 5 max pages
python scripts/run_scraper_adaptive.py --batch-file data/input/urls.txt

# Greedy strategy (all links)
python scripts/run_scraper_adaptive.py --strategy greedy --max-pages 10

# Main page only (fastest)
python scripts/run_scraper_adaptive.py --strategy main_only

# Resume from checkpoint
python scripts/run_scraper_adaptive.py --resume
```

### CLI Arguments

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--batch-file` | str | `data/input/urls.txt` | URLs file (one per line) |
| `--strategy` | choice | `adaptive` | `adaptive`, `greedy`, `main_only` |
| `--max-pages` | int | 5 | Max pages per website |
| `--checkpoint-name` | str | `batch` | Checkpoint batch name |
| `--resume` | flag | false | Resume from latest checkpoint |
| `--resume-from` | str | null | Resume from specific file |
| `--skip-progress` | flag | false | Disable progress tracking |

---

## Project Structure

```
LC_Official_Scraper/
├── src/                          # Main package
│   ├── __init__.py
│   ├── config.py                  # Configuration (Settings class)
│   ├── schemas.py                 # Pydantic models
│   ├── logger.py                  # Structured logging
│   │
│   ├── adaptive_crawler.py          # Intelligent web crawling
│   ├── link_analyzer.py           # LLM-guided link analysis
│   ├── llm_extractor.py            # Data extraction
│   ├── adaptive_processor.py       # Batch orchestration
│   │
│   ├── phone_utils.py              # Phone normalization
│   ├── failure_classifier.py      # Error categorization
│   ├── checkpoint_manager.py       # State persistence
│   ├── progress_tracker.py         # Progress/ETA tracking
│   ├── export.py                  # CSV export
│   │
│   └── __all__ exports
│
├── scripts/
│   ├── run_scraper_adaptive.py  # Main CLI entry point
│   └── test_adaptive.py          # Manual testing
│
├── data/
│   ├── input/                    # Input URLs files
│   │   ├── sample_urls.txt
│   │   └── production_urls.txt
│   ├── output/                   # Exported CSV files
│   └── state/                    # Checkpoint JSON files
│
├── .env                         # Environment variables
├── requirements.txt               # Python dependencies
├── CODE_REVIEW.md              # Improvement tracking
├── ARCHITECTURE.md            # This file
└── README.md                   # Original usage guide
```

---

## Recent Improvements (from CODE_REVIEW.md)

### Completed Issues

1. **Error Swallowing Fix** - Added `extraction_error` field to distinguish validation failures
2. **Magic Numbers to Config** - Migrated 4 hardcoded values to configuration
3. **Retry Logic** - Implemented exponential backoff for LLM API calls
4. **Dependency Injection** - Shared AsyncOpenAI client across components
5. **`__all__` Exports** - Explicit public API definitions for all modules
6. **Debug Log Cleanup** - Removed verbose debug logging, kept essential logs
7. **Type Hints** - Added return types to async functions

---

## Technical Stack

- **Python 3.9+**
- **Async Framework**: asyncio
- **Web Crawling**: crawl4ai (AsyncWebCrawler)
- **LLM API**: OpenRouter (xiaomi/mimo-v2-flash:free)
- **Data Validation**: Pydantic
- **Configuration**: pydantic-settings
- **Data Export**: Pandas
- **Phone Validation**: phonenumbers

---

## Known Limitations & Future Improvements

### Limitations

1. **Browser Instance Churn** - Creates new AsyncWebCrawler per operation (Issue #12)
2. **Per-URL Rate Limiting** - Not global across concurrent crawls (Issue #9)
3. **Memory Accumulation** - All results in memory (Issue #11)
4. **No URL Deduplication** - Duplicates across sessions (Issue #13)

### Potential Future Enhancements

1. **Browser Connection Pooling** - Reuse browser instances
2. **Global Rate Limiter** - Token bucket or semaphore with timing
3. **Streaming Exports** - Batch export to reduce memory
4. **URL Deduplication** - Track processed URLs globally
5. **Unit Tests** - Add coverage for schemas and utilities
6. **Integration Tests** - End-to-end testing
7. **Mock LLM Responses** - For consistent testing

---

## Public API (`__all__` Exports)

Each module explicitly exports its public API:

```python
# src/schemas.py
__all__ = ["BusinessSegment", "DecisionMaker", "CompanyData", "LLMExtractionResult", "OutOfScopeRecord", "LowConfidenceRecord"]

# src/config.py
__all__ = ["Settings"]

# src/llm_extractor.py
__all__ = ["LLMExtractor", "retry_with_backoff"]

# src/link_analyzer.py
__all__ = ["LinkAnalyzer", "retry_with_backoff"]

# src/adaptive_crawler.py
__all__ = ["AdaptiveWebsiteCrawler"]

# src/adaptive_processor.py
__all__ = ["AdaptiveScraperProcessor"]

# src/phone_utils.py
__all__ = ["normalize_to_e164"]

# src/export.py
__all__ = ["CSVExporter"]

# src/failure_classifier.py
__all__ = ["FailureClassifier"]

# src/checkpoint_manager.py
__all__ = ["CheckpointManager"]

# src/progress_tracker.py
__all__ = ["ProgressTracker"]

# src/logger.py
__all__ = ["get_logger", "log_crawl_start", "log_crawl_success", "log_crawl_failure", "log_http_fallback", "log_llm_extraction"]
```

---

## Performance Characteristics

| Strategy | Avg Time | Speed |
|----------|-----------|--------|
| Adaptive | 15-30s/URL | 2-3 URLs/min |
| Main_Only | 5-10s/URL | 5-10 URLs/min |
| Greedy | 30-60s/URL | 1-2 URLs/min |

**Note**: Actual performance depends on website complexity and network conditions.
