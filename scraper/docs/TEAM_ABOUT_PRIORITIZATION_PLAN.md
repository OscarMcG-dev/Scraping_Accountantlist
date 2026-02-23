# Plan: Better prioritization of team/about pages — IMPLEMENTED

**Goal:** Ensure team, about-us, our-people, partners, and similar pages are crawled more reliably so decision-maker extraction improves.

**Status:** Options A and C implemented. See changes in `website_enricher.py`.

---

## Current behaviour

- **Link triage:** After the homepage is crawled, `internal_links` are either:
  - Sent to the **LLM** (`_llm_triage_links`), which picks up to `max_crawl_subpages - 1` URLs, or
  - Passed to **keyword prioritization** (`_prioritize_links`), which returns team URLs first, then contact URLs.
- **Contact guarantee:** If the chosen list has no contact-like URL, the first contact URL from `_get_contact_urls()` is **prepended**. So at least one contact page is crawled when available.
- **Team:** There is no equivalent “guarantee at least one team page”. When LLM triage is on, the model might pick service/contact pages and skip team/about. When LLM is off, `_prioritize_links` already puts team first.

So the gap is: **when LLM triage is used, team/about pages can be dropped** in favour of other links the model prefers.

---

## Options (pick one or combine)

### Option A – Guarantee at least one team page when present (recommended)

- After computing `priority_urls` (from LLM or keyword fallback), compute **team URLs** the same way as in `_prioritize_links`: links whose path or link text matches `TEAM_PATH_KEYWORDS` / `TEAM_TEXT_KEYWORDS`.
- If there is at least one team URL in `internal_links` and **none** of them appear in `priority_urls`, **prepend** the first team URL:  
  `priority_urls = [first_team_url] + [u for u in priority_urls if u != first_team_url]`.
- Then apply the existing contact guarantee (prepend contact if missing).
- **Pros:** Small change, no new config, always crawls at least one team page when the site has one. **Cons:** Uses one sub-page slot for team even when the LLM might have chosen something else.

### Option B – Reserve first N slots for team URLs

- After LLM or keyword prioritization, get a list of **team URLs** (same keyword logic).
- Build final list: `team_urls[:N] + priority_urls` (deduplicated), then trim to `max_pages - 1`. For example `N = 2`: first two slots are always team when available, rest are LLM/keyword picks.
- **Pros:** Strong guarantee that several team pages are crawled. **Cons:** Can push out contact or other useful pages if `max_crawl_subpages` is small; may need a second pass to ensure at least one contact (already implemented).

### Option C – Strengthen the LLM prompt only

- In `get_default_crawl_prompts()`, make the link-triage system prompt more explicit: e.g. “You **must** include at least one team, about-us, our-people, partners, or leadership page when such a link exists in the list; prefer these for the first slots.”
- **Pros:** No change to merge logic; model does the right thing more often. **Cons:** Not a hard guarantee; model can still ignore the instruction.

### Option D – Keyword-first merge (team + contact, then LLM)

- Compute **team_urls** and **contact_urls** from `internal_links` (like today).
- Build a “must-crawl” list: e.g. `team_urls[:2] + contact_urls[:1]` (deduped).
- Pass the **remaining** links (excluding must-crawl) to the LLM with a smaller `max_picks` (e.g. `max_sub - len(must_crawl)`).
- Final list: `must_crawl + llm_picks`, trimmed to `max_pages - 1`.
- **Pros:** Team and contact are guaranteed when available; LLM fills remaining slots. **Cons:** More logic; need to ensure `max_picks` stays sensible when many team/contact URLs exist.

---

## Recommendation

- **Implement Option A** first: guarantee at least one team page when present, reusing existing keyword lists and the same prepend pattern used for contact. Low risk and addresses the main gap.
- **Optionally** add Option C (tighter prompt) so the LLM also tends to choose team pages earlier.
- If results are still short on DMs, consider Option B with `N = 1` or `2` (reserve 1–2 slots for team).

---

## Implementation notes (for Option A)

- In `website_enricher.py`, add a helper `_get_team_urls(links, base_url)` that returns URLs matching team path/text keywords (mirror `_get_contact_urls` and the team branch in `_prioritize_links`).
- In `_do_crawl`, after building `priority_urls` and **before** the existing contact prepend:
  - `team_urls = self._get_team_urls(internal_links, url)`
  - If `team_urls` and no URL in `priority_urls` is in `team_urls` (or `_is_team_url`), then  
    `priority_urls = [team_urls[0]] + [u for u in priority_urls if u != team_urls[0]]`
- Then keep the existing contact guarantee and `priority_urls = priority_urls[: max_pages - 1]`.

No new config or env vars required. Reuse `TEAM_PATH_KEYWORDS` and `TEAM_TEXT_KEYWORDS` (and optionally add `about`, `about-us` if not already covered).
