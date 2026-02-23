# Enhancement Plan for LC Official Scraper

**Status:** Updated - Ready for Implementation
**Last Updated:** 2026-01-06 (Based on user feedback)
**Purpose:** Polish core functionality and add operational features for production use

---

## User-Decided Configurations

| Feature | Decision | Rationale |
|----------|---------|------------|
| Email Notifications | Skip for now | Focus on core features first |
| Real-time Webhooks | Include | Good UX enhancement |
| Resume Behavior | Re-process failures | Don't lose data from issues |
| Duplicate Handling | Flag first, manual merge | User control over data quality |
| Export Formats | CSV + JSON | Support both use cases |
| Log Retention | 7 days with verbose toggle | Balance storage vs debugging |
| Bot Protection | Prioritize user agent | More effective than delay alone |
| Acquired Businesses | Flag (don't skip) | Sales team should see them |
| LLM Model | Keep current, add GUI | xiaomi/mimo-v2-flash:free works well |
| Settings GUI | Add soon | Easy editing without code changes |

---

## Implementation Phases (Revised)

### Phase 1: Core Robustness & Intelligence (Week 1)
**Updated to include critical items moved from later phases:**

- [ ] **1.1 HTTP/HTTPS Fallback** (🔴 High Impact, Low Effort)
  - Try HTTPS first
  - Auto-fallback to HTTP on SSL/certificate errors
  - Log which URL version succeeded

- [ ] **1.2 Failure Classification** (🔴 High Impact, Medium Effort)
  - Detect failure types: DNS, timeout, 403, 404, etc.
  - Classify severity: permanent, temporary, unknown
  - Provide suggested actions
  - Export to `failed_urls.csv` with classification details

- [ ] **1.3 Structured Logging** (🔴 High Impact, Low Effort)
  - Implement `structlog` for JSON logging
  - Separate log files: scraper.log, errors.log, audit.log, performance.log
  - Configurable log level (DEBUG, INFO, WARNING, ERROR)
  - Verbose toggle for detailed request/response logging

- [ ] **1.4 Config Validation** (🟡 Medium Impact, Low Effort - MOVED UP)
  - Validate API key format on startup
  - Check directory paths exist/writable
  - Validate numerical ranges (timeout, max_pages, etc.)
  - Validate enum values (crawl_strategy, business_segment options)
  - Show clear error messages with fixes

- [ ] **1.5 Sales-Ready Description Field** (🔴 High Impact, Medium Effort - MOVED UP)
  - Add `edited_description` field to schema
  - Update LLM prompt to extract sales intelligence:
    - Tech stack & software (Xero, MYOB, QuickBooks, AI tools)
    - Firm characteristics (size, sole vs multi-partner, efficiency focus)
    - Target market (SMEs, startups, high-net-worth, industries)
    - Value propositions and differentiators
  - Keep original `description` intact for reference
  - Remove marketing fluff, focus on actionable insights

- [ ] **1.6 Dependency Cleanup** (🟢 Low Impact, Low Effort - MOVED UP)
  - Review `requirements.txt`
  - Remove unused imports
  - Final list: crawl4ai, openai, pydantic, pydantic-settings, pandas, phonenumbers, python-dotenv, pytest

**Deliverable:** More reliable scraper with better error handling and sales intelligence

---

### Phase 2: Business Acquisition & Folding Detection (Week 2)

- [ ] **2.1 Acquisition Pattern Detection** (🟡 Medium Impact, Medium Effort)
  - Text patterns: "acquired by", "merged with", "now part of"
  - Auto-redirect detection (301/302 to different domain)
  - JavaScript redirect detection
  - Content analysis: "please visit [new business]", "services now provided by"

- [ ] **2.2 Schema Updates for Acquisition Info** (🟡 Medium Impact, Low Effort)
  - Add fields to `CompanyData`:
    - `business_status`: "active" | "acquired" | "folded" | "moved" | "unknown"
    - `acquired_by`: string (who acquired them)
    - `acquisition_notes`: string (details about acquisition)
    - `acquisition_url`: string (new business URL)
  - Update CSV export to include new fields

- [ ] **2.3 Acquisition Flagging Logic** (🟡 Medium Impact, Medium Effort)
  - Flag as "acquired" in results
  - Include new business details if found
  - Still export to main CSV (don't skip)
  - Log for review

**Deliverable:** Valuable intelligence about business status changes

---

### Phase 3: Operations & User Experience (Week 3)

- [ ] **3.1 Progress Tracking & ETA** (🟡 Medium Impact, Low Effort)
  - Progress bar with percentage
  - Current URL being processed
  - Success/failure totals counters
  - Estimated time remaining
  - Real-time updates (every 10 items or 5 seconds)

- [ ] **3.2 Resume Capability** (🟡 Medium Impact, Medium Effort)
  - Save checkpoint file: `.checkpoint.json`
  - Track: processed URLs, last successful index
  - On restart: skip to last processed + 1
  - Option to re-process failures
  - CLI flag: `--resume` or `--restart`

- [ ] **3.3 Bot Protection Strategy** (🟡 Medium Impact, Medium Effort)
  - Detect 403 Forbidden errors
  - On 403: Change user agent first
  - If still 403: Add delay (5-10s) and retry
  - Rotate user agents: list of 3-5 real browser strings
  - Log protection attempts

- [ ] **3.4 Webhook Notifications** (🟢 Low Impact, Medium Effort)
  - POST webhook URL on batch start
  - POST webhook URL on batch end (with summary)
  - POST webhook URL on error threshold (>X% failures)
  - Configurable: `WEBHOOK_URL` env var
  - Simple JSON payload with event type and data

**Deliverable:** Better UX with progress visibility and notifications

---

### Phase 4: Polish & Additional Features (Week 4)

- [ ] **4.1 Duplicate Detection** (🟢 Low Impact, Medium Effort)
  - Same company name + different URLs
  - Same phone number across entries
  - Redirect chains to same domain
  - Flag with `duplicate: true` field
  - Option to auto-merge or manual review

- [ ] **4.2 Multi-Format Export** (🟢 Low Impact, Low Effort)
  - Export to CSV (default)
  - Export to JSON via `--format json` flag
  - Same data structure, different serialization
  - Update export module to handle both

- [ ] **4.3 Settings GUI** (🟢 Low Impact, High Effort)
  - Simple CLI or web interface
  - Edit settings without code:
    - OpenRouter model (change model slug)
    - LLM prompt template (add/remove instructions)
    - Crawling strategy (default, max_pages)
    - Confidence threshold slider
    - Log level selector
    - Verbose logging toggle
  - Save to `.scraper_settings.json`
  - Hot-reload config changes

- [ ] **4.4 Log Retention & Rotation** (🟢 Low Impact, Low Effort)
  - Keep logs for 7 days (configurable)
  - Daily rotation for each log file
  - Delete old logs automatically
  - Configurable via `LOG_RETENTION_DAYS` env var

- [ ] **4.5 Audit Trail** (🟢 Low Impact, Low Effort)
  - Log all major events with timestamps
  - Event types: batch_start, batch_end, url_start, url_end, error
  - Include duration_ms and context
  - Save to `audit.jsonl` (JSON lines for easy parsing)
  - Simple audit viewer script

- [ ] **4.6 Statistics Dashboard** (🟢 Low Impact, High Effort)
  - Track metrics per batch:
    - Total URLs, successful, failed, out_of_scope, low_confidence
    - Success rate percentage
    - Average confidence score
    - Average pages crawled per URL
    - Total pages crawled
    - Total time elapsed
  - Persist stats to `statistics.json`
  - Generate simple HTML report
  - Trend analysis over time

**Deliverable:** Production-ready tool with extras

---

## Updated Implementation Priority Matrix

| Phase | Item | Priority | Impact | Effort | Status |
|--------|------|----------|--------|--------|
| **Phase 1** | | | | |
| 1.1 | HTTP/HTTPS Fallback | 🔴 High | High | Low | Ready |
| 1.2 | Failure Classification | 🔴 High | High | Medium | Ready |
| 1.3 | Structured Logging | 🔴 High | High | Low | Ready |
| 1.4 | Config Validation | 🟡 Medium | Medium | Low | Ready |
| 1.5 | Sales-Ready Description | 🔴 High | Medium | Low | Ready |
| 1.6 | Dependency Cleanup | 🟢 Low | Low | Low | Ready |
| **Phase 2** | | | | |
| 2.1 | Acquisition Detection | 🟡 Medium | Medium | Medium | Ready |
| 2.2 | Acquisition Schema | 🟡 Medium | Low | Low | Ready |
| 2.3 | Acquisition Flagging | 🟡 Medium | Medium | Low | Ready |
| **Phase 3** | | | | |
| 3.1 | Progress Tracking | 🟡 Medium | Low | Medium | Ready |
| 3.2 | Resume Capability | 🟡 Medium | Medium | Medium | Ready |
| 3.3 | Bot Protection Strategy | 🟡 Medium | Medium | Medium | Ready |
| 3.4 | Webhook Notifications | 🟢 Low | Low | Medium | Ready |
| **Phase 4** | | | | |
| 4.1 | Duplicate Detection | 🟢 Low | Medium | Medium | Ready |
| 4.2 | Multi-Format Export | 🟢 Low | Low | Low | Ready |
| 4.3 | Settings GUI | 🟢 Low | High | High | Ready |
| 4.4 | Log Retention | 🟢 Low | Low | Low | Ready |
| 4.5 | Audit Trail | 🟢 Low | Low | Low | Ready |
| 4.6 | Statistics Dashboard | 🟢 Low | High | High | Ready |

---

## File Structure Changes

```
LC_Official_Scraper/
├── src/
│   ├── config.py              # [UPDATE] Add settings validation
│   ├── schemas.py              # [UPDATE] Add acquisition fields, sales_description
│   ├── adaptive_crawler.py     # [UPDATE] HTTP fallback, failure classification
│   ├── adaptive_processor.py   # [UPDATE] Progress tracking, resume, webhook support
│   ├── llm_extractor.py        # [UPDATE] Sales intelligence prompt
│   ├── export.py               # [UPDATE] Multi-format export
│   └── logger.py              # [NEW] Structured logging framework
├── scripts/
│   ├── run_scraper.py         # [UPDATE] Add resume flag, format flag, webhook support
│   └── settings_gui.py        # [NEW] Settings editor GUI
├── data/
│   ├── .checkpoint.json        # [NEW] Resume checkpoint file
│   ├── audit.jsonl             # [NEW] Audit trail (JSON lines)
│   ├── statistics.json          # [NEW] Metrics persistence
│   └── logs/                  # [NEW] Structured log directory
│       ├── scraper.log
│       ├── errors.log
│       ├── audit.log
│       └── performance.log
└── .scraper_settings.json     # [NEW] GUI-edited settings
```

---

## Implementation Order (Phase 1)

Recommended order for Phase 1 implementation:

1. **Dependency Cleanup** (15 min)
   - Update requirements.txt
   - Verify all imports are used
   - Quick win, clean slate

2. **Config Validation** (30 min)
   - Add validation function to src/config.py
   - Call on startup in scripts
   - Clear error messages

3. **Structured Logging** (45 min)
   - Create src/logger.py with structlog
   - Update all modules to use structured logger
   - Configurable via environment
   - Add verbose toggle

4. **HTTP/HTTPS Fallback** (20 min)
   - Add try_url_with_fallback() to adaptive_crawler.py
   - Use in crawl_intelligently()
   - Log which protocol succeeded

5. **Failure Classification** (60 min)
   - Create failure_classifier.py module
   - Classify exceptions by type
   - Generate FailureClassification dataclass
   - Export to failed_urls.csv with details

6. **Sales-Ready Description** (45 min)
   - Update src/schemas.py to add edited_description
   - Update LLM prompt in llm_extractor.py
   - Extract sales intelligence only
   - Test with existing scraped data

**Total Phase 1 Estimate:** ~3.5 hours

---

## Ready to Start?

Phase 1 is ready for implementation!

**Which item should we tackle first?**

My recommendation: Start with **1.1 HTTP/HTTPS Fallback** - quick win, immediately improves success rate, and builds momentum for the rest of Phase 1.

Or would you like to:
1. Start with a different item?
2. Review the plan further?
3. Adjust priorities?
4. Something else?

Let me know and we'll get coding! 🚀
