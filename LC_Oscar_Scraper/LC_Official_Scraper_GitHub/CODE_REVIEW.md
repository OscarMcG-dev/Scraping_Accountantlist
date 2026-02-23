# LC Official Scraper - Code Review

## Executive Summary

**Overall Assessment: 7/10** - Solid foundation with good architecture, but has typical vibe-coding artifacts that should be cleaned before presenting to leadership.

---

## Critical Issues (Fix Before Demo)

### 1. Duplicate Content in Prompts

**`src/llm_extractor.py:156-167`** - Decision maker identification instructions are duplicated verbatim.

**`src/llm_extractor.py:265-266`** - `confidence_score` listed twice in extraction prompt.

**`src/llm_extractor.py:255-256`** - Numbering skips (6 appears twice, goes 5, 6, 6, 7...).

**Impact:** Wastes tokens, looks unprofessional, may confuse LLM.

---

### 2. Synchronous LLM Calls in Async Code

**`src/link_analyzer.py:70-82`** - Uses synchronous `client.chat.completions.create()` inside what should be async flow.

**`src/llm_extractor.py:54-67`** - Same issue - `extract()` is `async def` but the OpenAI call is synchronous.

**Impact:** Blocks the event loop, negating concurrency benefits. Use `await client.chat.completions.acreate()` or wrap in `asyncio.to_thread()`.

---

### 3. Unused Code

**`src/adaptive_crawler.py:381-400`** - `_combine_markdown()` method is never called (superseded by `_build_crawl_result()`).

**`src/crawler.py`** and **`src/processor.py`** - Entire files appear unused (adaptive versions replaced them).

---

## Code Quality Issues

### 4. Error Swallowing

```python
# src/llm_extractor.py:98
return LLMExtractionResult(confidence_score=0.0)
```

Validation errors return empty result with 0 confidence - no way to distinguish "validation failed" from "site truly has no data." Consider adding an error field or separate exception path.

---

### 5. Hardcoded Magic Numbers

| Location | Value | Issue |
|----------|-------|-------|
| `link_analyzer.py:105` | `20` | Links limit for LLM analysis |
| `adaptive_crawler.py:143` | `20` | Links limit for basic discovery |
| `llm_extractor.py:238` | `25000` | Content truncation limit |
| `adaptive_crawler.py:280` | `3` | Sub-page concurrency semaphore |

These should be in `config.py` for tuning without code changes.

---

### 6. Inconsistent Phone Validation

**`schemas.py:49-51`** - Validates E.164 but then returns original `v` (with spaces/hyphens) instead of cleaned `phone`.

```python
# Current (buggy):
return v  # Returns "+61 2 1234 5678"

# Should be:
return phone  # Returns "+6121234567"
```

---

### 7. Import in Function Body

**`src/adaptive_crawler.py:177-178`**
```python
async def crawl_main_page(self, url: str):
    import time  # Should be at module level
```

---

## Architecture Improvements

### 8. Consider Dependency Injection

Currently each component creates its own OpenAI client:
- `LLMExtractor.__init__()` creates client
- `LinkAnalyzer.__init__()` creates client

Better: Create one client in `Settings` or pass shared client to components.

---

### 9. Rate Limiting is Per-URL, Not Global

**`src/adaptive_processor.py:295`**
```python
await asyncio.sleep(self.settings.delay_between_requests)
```

With 10 concurrent crawls, you're making 10 requests immediately, then all wait 1s. Consider a proper rate limiter or token bucket.

---

### 10. Missing Retry Logic for LLM Calls

`link_analyzer.py` and `llm_extractor.py` have no retry logic for transient API failures. The `MAX_RETRIES` setting in config is never used for LLM calls.

---

## Scalability Concerns

### 11. Memory: All Results in Memory

`process_batch()` accumulates all results in lists. For 10,000+ URLs, consider streaming to disk or batched exports.

---

### 12. Browser Instance Churn

**`adaptive_crawler.py:179, 278, 413, 445, 465`** - Creates new `AsyncWebCrawler` for each operation. Consider connection pooling or keeping browser alive across crawls.

---

### 13. No Deduplication Across Sessions

If you run the scraper twice with overlapping URLs, you'll get duplicates in output. Consider URL deduplication in CSVExporter.

---

## Quick Wins (Polish)

| Issue | File:Line | Fix |
|-------|-----------|-----|
| Remove dead code | `crawler.py`, `processor.py` | Delete files or add deprecation warning |
| Fix prompt duplicates | `llm_extractor.py` | Remove duplicate sections |
| Add `__all__` exports | All `src/*.py` | Explicit public API |
| Type hints on all functions | Throughout | Some missing return types |
| Remove debug logs in production | `llm_extractor.py:72-82` | Move to `logger.debug` or remove |

---

## Testing Gaps

- No unit tests for schemas validation edge cases
- No integration tests for full pipeline
- No mock tests for LLM responses
- `test_adaptive.py` is more of a manual runner than automated tests

---

## Cost Optimization

### LLM Token Usage

1. **Link Analysis** sends 20 links to LLM every time - consider caching common patterns
2. **Extraction prompt** is ~1000 tokens just for system prompt - could be trimmed
3. Using `xiaomi/mimo-v2-flash:free` (good!) but no fallback if rate-limited

---

## Recommended Priority Order

1. **Fix duplicate prompts** (5 min) - Most visible "vibe coding" smell
2. **Fix async/sync mismatch** (30 min) - Real performance bug
3. **Remove dead code** (10 min) - Cleaner codebase
4. **Move magic numbers to config** (20 min) - Professional touch
5. **Fix phone validation bug** (5 min) - Actual bug

---

## What's Good

- Clean separation of concerns (crawler, extractor, exporter)
- Pydantic validation throughout
- Checkpoint/resume system is well-designed
- Fallback strategies (adaptive -> main_only, HTTPS -> HTTP)
- Structured logging
- Clear CLI interface with helpful options
- Good README and progress tracking

The architecture is solid - this is better than most vibe-coded projects. The issues are mostly surface-level cleanup rather than fundamental design problems.
