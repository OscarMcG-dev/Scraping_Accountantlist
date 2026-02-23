# Adaptive Crawling Design Document

## Overview

This document describes the redesigned crawling approach that replaces brittle hard-coded URL patterns with intelligent, LLM-guided page discovery. This new approach is designed to handle the wide variety of accounting websites that have varying complexity and non-standard URL structures.

---

## The Problem with the Old Approach

### Limitations of Hard-Coded Patterns

The previous crawler used 13 hard-coded URL patterns:
```python
team_patterns = [
    "/team", "/about/team", "/our-people", "/our-team",
    "/staff", "/meet-the-team", "/partners", "/directors",
    "/about-us", "/about/our-team", "/people", "/meetyourteam",
    "/our-staff", "/team-members", "/our-partners"
]
```

**Issues:**
- **Brittle**: Only matches URLs that follow these exact patterns
- **Fails on non-standard sites**: Many small accounting firms use unconventional URLs like `/who-we-are`, `/our-great-team`, `/team-membership`, or even nonsensical patterns
- **Ignores link context**: Doesn't consider the actual link text or page structure
- **Cannot adapt**: Static patterns cannot understand semantic meaning

---

## The New Adaptive Approach

### Core Design Philosophy

Instead of looking for specific URL patterns, we use **semantic understanding**:
1. Extract all internal links from the main page
2. Use an LLM to analyze each link's **context** (URL, text, position)
3. Intelligently classify and prioritize links based on their likely content
4. Crawl only the most valuable pages

### Key Innovations

#### 1. Semantic Link Analysis

The LLM analyzes links considering:
- **Link text**: "Our Team", "Meet the Staff", "Who We Are"
- **URL structure**: Even non-standard URLs
- **Context**: Navigation bar, footer, sidebar placement
- **Intent**: What content is the link likely pointing to?

#### 2. Multi-Stage Crawling

**Stage 1: Discovery**
- Crawl main page
- Extract ALL internal links with full metadata
- Preserve navigation elements to capture link context

**Stage 2: Analysis**
- Send link data to LLM
- LLM classifies links into categories:
  - Team/People links (highest priority)
  - About/Company information
  - Service descriptions
  - Contact information
- LLM provides crawling priority order

**Stage 3: Intelligent Crawling**
- Crawl pages in priority order (team pages first)
- Combine content from all crawled pages
- Feed comprehensive content to extraction LLM

#### 3. Three Crawling Strategies

| Strategy | Description | Best For |
|----------|-------------|----------|
| **adaptive** | LLM-guided discovery | Most websites, unknown structures |
| **greedy** | Crawl all internal links | Simple sites, maximum coverage |
| **main_only** | Crawl only main page | Fast testing, well-organized main pages |

---

## Architecture

### New Components

```
src/
├── link_analyzer.py       # LLM-based link classification
├── adaptive_crawler.py    # Intelligent crawler with multi-stage discovery
└── adaptive_processor.py  # Orchestrates adaptive scraping
```

### Flow Diagram

```
User Input
    ↓
[AdaptiveProcessor]
    ↓
[AdaptiveCrawler.crawl_intelligently()]
    ↓
┌─────────────────────────────────────┐
│ Stage 1: Discovery                  │
│ - Crawl main page (keep nav/footer)  │
│ - Extract all internal links         │
│ - Capture link text and context     │
└─────────────────────────────────────┘
    ↓
┌─────────────────────────────────────┐
│ Stage 2: Analysis                   │
│ [LinkAnalyzer.analyze_links()]      │
│ - Send links to LLM                 │
│ - Classify by intent                │
│ - Generate priority order           │
└─────────────────────────────────────┘
    ↓
┌─────────────────────────────────────┐
│ Stage 3: Crawling                   │
│ - Crawl prioritized pages           │
│ - Combine all markdown content      │
└─────────────────────────────────────┘
    ↓
[LLMExtractor] → Extract business data
    ↓
[Export] → CSV files
```

---

## Technical Details

### Link Analysis (link_analyzer.py)

**Input:**
```python
{
    "company_url": "https://example-accounting.com.au",
    "internal_links": [
        {"url": "/our-team", "text": "Meet Our Team"},
        {"url": "/services", "text": "What We Do"},
        {"url": "/about-us", "text": "About Us"}
    ]
}
```

**LLM Classification System Prompt:**
The LLM is instructed to:
- Look at LINK TEXT and CONTEXT more than URL structure
- Be flexible with naming conventions (team, people, staff, partners, etc.)
- Identify navigation structure
- Handle unusual URL schemes intelligently
- Prioritize pages likely containing individual profiles

**Output:**
```python
{
    "team_links": ["/our-team", "/partners"],
    "about_links": ["/about-us", "/company-overview"],
    "service_links": ["/services"],
    "contact_links": ["/contact"],
    "priority_order": ["/our-team", "/partners", "/about-us"],
    "reasoning": "Identified team pages by link text indicating staff profiles..."
}
```

### Adaptive Crawler (adaptive_crawler.py)

**Key Features:**

1. **Context-Preserving Discovery Config:**
```python
self.discovery_config = CrawlerRunConfig(
    page_timeout=self.settings.page_timeout,
    remove_overlay_elements=True,
    # Keep navigation, footer to extract links with context
)
```

2. **Intelligent Link Normalization:**
```python
def _normalize_url(base_url: str, link: str) -> Optional[str]:
    # Handles relative URLs
    # Filters out javascript: mailto: tel: URLs
    # Validates URL format
```

3. **Controlled Concurrency:**
```python
semaphore = asyncio.Semaphore(3)  # Limit concurrent sub-page crawls
```

4. **Comprehensive Result Building:**
```python
{
    "url": "https://example.com",
    "strategy": "adaptive",
    "main_page": "...",
    "sub_pages": {"https://example.com/team": "...", ...},
    "pages_crawled": 4,
    "markdown": "combined content from all pages"
}
```

---

## Example Scenarios

### Scenario 1: Standard Website

**URL:** `https://standard-accounting.com.au`

**Old Approach:**
- Finds `/team` ✅
- Crawls team page
- Extracts data

**New Adaptive Approach:**
- Discovers 20 internal links
- LLM identifies: `/team` (team), `/about-us` (about), `/services` (services)
- Prioritizes: `/team`, `/about-us`, `/services`
- Crawls 3 pages (including main)
- Same result, but more thorough

### Scenario 2: Non-Standard URL Structure

**URL:** `https://creative-accountants.com.au`

**Old Approach:**
- Searches for `/team`, `/our-people`, etc.
- Actual team page is `/the-people-who-help-you` ❌
- Fails to find team page
- Only extracts from main page (limited info)

**New Adaptive Approach:**
- Discovers 15 internal links
- LLM analyzes:
  - `/the-people-who-help-you` with text "Meet the Team" → **team**
  - `/what-we-do` with text "Our Services" → **services**
  - `/our-journey` with text "About Us" → **about**
- Prioritizes: `/the-people-who-help-you`, `/what-we-do`, `/our-journey`
- Crawls 3 pages
- Successfully extracts comprehensive data ✅

### Scenario 3: Minimalist Website

**URL:** `https://simple-accounting.com.au`

**Old Approach:**
- Searches for `/team`, etc.
- No matching URL found
- Only extracts from main page

**New Adaptive Approach:**
- Discovers 5 internal links
- LLM analyzes and finds no team-specific links
- Returns: `team_links: []`, `priority_order: []`
- Gracefully falls back to main page only
- Same result, but with better handling

### Scenario 4: Complex Website with Unconventional URLs

**URL:** `https://modern-accountants.com.au`

**Actual URLs:**
- `/staff-directory/john-smith-123` (individual profiles)
- `/our-story-mission-values` (about)
- `/tax-accounting-advisory-sydney` (services)
- `/get-in-touch-sydney-office` (contact)

**Old Approach:**
- Searches for `/team`, `/about-us`, etc.
- Fails to match any pattern ❌

**New Adaptive Approach:**
- Discovers 50+ internal links
- LLM analyzes link text and patterns:
  - `/staff-directory/*` → team (pattern recognition)
  - `/our-story-*` → about
  - `/tax-accounting-*` → services
- Prioritizes: All staff directory pages first
- Crawls 5 pages (main + 4 staff profiles)
- Extracts detailed decision maker info ✅

---

## Benefits

### 1. Flexibility
- Handles any URL structure
- Adapts to different naming conventions
- Works with both simple and complex websites

### 2. Robustness
- Fallback to pattern matching if LLM fails
- Graceful degradation when no team pages exist
- No single point of failure

### 3. Coverage
- Discovers pages hard-coded patterns would miss
- Prioritizes based on semantic value
- Captures content from multiple relevant pages

### 4. Intelligence
- Understands intent, not just patterns
- Learns from link text and context
- Makes smarter decisions about what to crawl

### 5. Maintainability
- No need to constantly add new patterns
- LLM prompt can be updated centrally
- Adapts automatically to new website patterns

---

## Usage

### Running with Adaptive Strategy

```bash
# Test a single URL
python scripts/test_adaptive.py --url "https://example-accounting.com.au" --strategy adaptive --max-pages 5

# Test a batch of URLs
python scripts/test_adaptive.py --strategy adaptive --max-pages 3 --sample-count 10

# Compare all strategies
python scripts/test_adaptive.py --compare --sample-count 5
```

### Integration in Production

```python
from src.adaptive_processor import AdaptiveScraperProcessor

# Create processor with adaptive strategy
processor = AdaptiveScraperProcessor(
    settings=settings,
    crawl_strategy="adaptive",  # or "greedy" or "main_only"
    max_pages=5  # Total pages including main page
)

# Process URLs
results = await processor.process_batch(urls)
```

---

## Configuration

### Environment Variables

```bash
# Crawl Strategy (default: adaptive)
# Options: adaptive, greedy, main_only
CRAWL_STRATEGY=adaptive

# Maximum Pages to Crawl (default: 5)
MAX_PAGES=5

# Rate Limiting (default: 1 second between requests)
DELAY_BETWEEN_REQUESTS=1.0
```

### Choosing a Strategy

| Strategy | Use When... |
|----------|-------------|
| **adaptive** | Most websites, unknown structures, want best balance |
| **greedy** | Small simple sites, want maximum coverage, don't care about LLM cost |
| **main_only** | Testing, well-organized main pages, want speed |

---

## Performance Considerations

### LLM Cost vs. Value

**Adaptive Strategy:**
- **Cost:** 1 LLM call per website for link analysis + 1 for extraction
- **Value:** Intelligent page selection, high-quality results
- **Trade-off:** Extra cost for significantly better discovery

**Greedy Strategy:**
- **Cost:** 1 LLM call for extraction only
- **Value:** Crawl more pages, potentially more data
- **Trade-off:** May crawl irrelevant pages

**Main Only Strategy:**
- **Cost:** 1 LLM call for extraction
- **Value:** Fastest, no extra LLM cost
- **Trade-off:** May miss team/bio page data

### Concurrency

- Main page: Single crawl
- Sub-pages: Up to 3 concurrent (configurable via semaphore)
- Batch processing: Configurable via `MAX_CONCURRENT_CRAWLS` (default: 10)

---

## Future Enhancements

### Potential Improvements

1. **Learning from Results:**
   - Track which pages are most valuable
   - Learn patterns from successful extractions
   - Build knowledge base of website structures

2. **Multi-Level Discovery:**
   - After crawling team pages, discover individual profile links
   - Dynamically expand crawling depth based on findings

3. **Content-Aware Crawling:**
   - Check if decision maker info already found
   - Stop crawling if sufficient data obtained
   - Optimize crawl depth dynamically

4. **Website Type Classification:**
   - Classify website complexity first
   - Adjust strategy based on website type
   - Simple sites → main_only, complex → adaptive

5. **Retry with Alternative Strategy:**
   - If adaptive fails with low confidence
   - Automatically retry with greedy strategy
   - Combine results for best outcome

---

## Migration from Old Approach

### Steps to Migrate

1. **Install new dependencies:**
```bash
pip install -r requirements.txt
```

2. **Update imports:**
```python
# Old
from src.crawler import AccountingWebsiteCrawler
from src.processor import ScraperProcessor

# New
from src.adaptive_crawler import AdaptiveWebsiteCrawler
from src.adaptive_processor import AdaptiveScraperProcessor
```

3. **Update instantiation:**
```python
# Old
processor = ScraperProcessor(settings)

# New
processor = AdaptiveScraperProcessor(
    settings=settings,
    crawl_strategy="adaptive",
    max_pages=5
)
```

4. **Keep existing code compatible:**
- The old `crawler.py` and `processor.py` remain untouched
- Can switch back by importing old modules
- Gradual migration possible

---

## Testing

### Test Scenarios

1. **Standard URL Patterns:** Verify basic functionality
2. **Non-Standard URLs:** Test unconventional structures
3. **Minimalist Sites:** Test fallback behavior
4. **Complex Sites:** Test multi-page discovery
5. **Broken Sites:** Test error handling

### Running Tests

```bash
# Test single URL
python scripts/test_adaptive.py --url <URL>

# Test batch
python scripts/test_adaptive.py --sample-count 10

# Compare strategies
python scripts/test_adaptive.py --compare --sample-count 5
```

---

## Conclusion

The new adaptive crawling approach addresses the fundamental limitations of hard-coded URL patterns by introducing intelligent, LLM-guided page discovery. This provides:

- **Flexibility** to handle diverse website structures
- **Robustness** through semantic understanding
- **Coverage** of pages traditional patterns miss
- **Maintainability** without constant pattern updates

For accounting websites with varying complexity and non-standard URL patterns, this approach significantly improves the quality and reliability of data extraction.
