"""
Phase 2: Crawl firm websites and extract structured data via LLM.
Phase 2b: Targeted web search for decision makers when crawl yields none.

Uses crawl4ai for adaptive crawling and OpenRouter for LLM extraction.
Improvements over v1:
  - Strict JSON Schema structured output (no more freeform JSON)
  - Contact page discovery (contact-us, get-in-touch)
  - Retry with exponential backoff on transient errors
  - HTTPS -> HTTP fallback for older sites
  - Smart content truncation (team/about pages prioritized)
  - Conditional web search fallback via xAI native search
"""
import asyncio
import json
import logging
from typing import Optional, List, Dict, Any
from urllib.parse import urljoin, urlparse

from openai import OpenAI

try:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
    CRAWL4AI_AVAILABLE = True
except ImportError:
    CRAWL4AI_AVAILABLE = False

from config import Settings
from models import (
    EnrichmentData, DecisionMaker,
    LLMEnrichmentResponse, get_enrichment_json_schema,
    LLMWebSearchResponse, get_web_search_json_schema,
)
from phone_utils import normalize_to_e164
from checkpoint import Checkpoint

logger = logging.getLogger(__name__)

TEAM_PATH_KEYWORDS = [
    "team", "our-people", "people", "staff", "our-team",
    "meet-the", "about-us", "who-we-are", "directors",
    "partners", "leadership", "our-firm", "our-story",
]
TEAM_TEXT_KEYWORDS = [
    "our team", "our people", "meet the", "about us", "who we are",
    "the team", "our staff", "our directors", "our partners",
    "leadership", "meet our",
]
CONTACT_PATH_KEYWORDS = [
    "contact", "contact-us", "get-in-touch", "reach-us", "find-us",
    "enquiry", "enquiries",
]
CONTACT_TEXT_KEYWORDS = [
    "contact us", "contact", "get in touch", "reach us", "find us",
    "enquiry", "enquiries",
]
NEGATIVE_PATH_KEYWORDS = [
    "blog", "news", "faq", "privacy", "terms", "disclaimer", "sitemap",
    "careers", "jobs", "login", "portal", "client-portal", "book-online",
    "testimonial", "review", "case-study",
]

MAX_CONTENT_CHARS = 25_000
PRIORITY_PAGE_BUDGET = 0.40  # 40% of char budget reserved for team/about/contact pages


class WebsiteEnricher:
    """Crawl firm websites and extract structured data."""

    def __init__(self, settings: Settings):
        if not CRAWL4AI_AVAILABLE:
            raise ImportError(
                "crawl4ai is required for website enrichment. "
                "Install with: pip install crawl4ai"
            )
        self.settings = settings
        self.llm_client = OpenAI(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
        )
        self.browser_config = BrowserConfig(
            headless=True, viewport_width=1920, viewport_height=1080,
        )
        self._enrichment_schema = get_enrichment_json_schema()
        self._web_search_schema = get_web_search_json_schema()
        self._crawler_pool: List[AsyncWebCrawler] = []
        self._pool_semaphore: Optional[asyncio.Semaphore] = None
        self._pool_queue: Optional[asyncio.Queue] = None

    async def start_pool(self, size: int = 4) -> None:
        """Pre-warm a pool of browser contexts for concurrent crawling."""
        if self._crawler_pool:
            return
        logger.info(f"Starting browser pool with {size} contexts...")
        self._pool_queue = asyncio.Queue()
        for i in range(size):
            crawler = AsyncWebCrawler(config=self.browser_config)
            await crawler.__aenter__()
            self._crawler_pool.append(crawler)
            self._pool_queue.put_nowait(crawler)
        logger.info(f"Browser pool ready ({size} contexts)")

    async def stop_pool(self) -> None:
        """Shut down all pooled browser contexts."""
        for crawler in self._crawler_pool:
            try:
                await crawler.__aexit__(None, None, None)
            except Exception:
                pass
        self._crawler_pool.clear()
        self._pool_queue = None

    async def enrich(self, website_url: str, firm_name: str) -> Optional[EnrichmentData]:
        """Crawl a firm website and extract structured data.

        Falls back to web search (Phase 2b) when:
        - The crawl fails entirely (site down, DNS error, timeout)
        - The crawl succeeds but finds no named decision makers
        """
        crawl_result = await self._crawl_site_with_retry(website_url, firm_name=firm_name)
        if not crawl_result:
            if self.settings.web_search_enabled:
                logger.info(f"Crawl failed for {website_url}, falling back to web search")
                return await self._web_search_enrichment(firm_name, website_url)
            return None

        enrichment = await self._extract_with_llm_retry(website_url, firm_name, crawl_result)

        if (
            enrichment
            and not enrichment.decision_makers
            and not enrichment.out_of_scope
            and self.settings.web_search_enabled
        ):
            logger.info(f"No DMs from crawl for {firm_name}, trying web search")
            search_dms = await self._search_for_decision_makers(firm_name, website_url)
            if search_dms:
                enrichment.decision_makers = search_dms
                logger.info(f"  Web search found {len(search_dms)} DM(s)")

        return enrichment

    # ------------------------------------------------------------------
    # Crawling
    # ------------------------------------------------------------------

    async def _crawl_site_with_retry(
        self, url: str, firm_name: str = "", max_retries: int = 2,
    ) -> Optional[Dict[str, str]]:
        """Crawl with retry + HTTPS/HTTP fallback."""
        if not url.startswith("http"):
            url = "https://" + url

        for attempt in range(max_retries + 1):
            result, err_type = await self._crawl_site(url, firm_name=firm_name)
            if result:
                return result

            if err_type == "dns":
                logger.warning(f"Domain does not resolve: {url} -- skipping")
                return None

            if attempt == 0 and url.startswith("https://"):
                http_url = "http://" + url[len("https://"):]
                logger.info(f"HTTPS failed ({err_type}), trying HTTP: {http_url}")
                result, http_err = await self._crawl_site(
                    http_url, firm_name=firm_name,
                )
                if result:
                    return result
                if http_err == "dns":
                    return None

            if attempt < max_retries:
                delay = self.settings.retry_delay * (2 ** attempt)
                logger.info(f"Crawl retry {attempt + 1}/{max_retries} for {url} in {delay:.0f}s")
                await asyncio.sleep(delay)

        return None

    _DNS_MARKERS = ("ERR_NAME_NOT_RESOLVED", "DNS_PROBE")
    _CONN_MARKERS = ("ERR_CONNECTION_REFUSED", "ERR_CONNECTION_RESET", "ERR_CONNECTION_TIMED_OUT")
    _TLS_MARKERS = ("ERR_SSL", "ERR_CERT", "ERR_TLS")

    def _classify_error(self, err_str: str) -> str:
        """Classify a crawl error into a category for retry/fallback logic."""
        if any(m in err_str for m in self._DNS_MARKERS):
            return "dns"
        if any(m in err_str for m in self._CONN_MARKERS):
            return "connection"
        if any(m in err_str for m in self._TLS_MARKERS):
            return "tls"
        return "other"

    async def _crawl_site(
        self, url: str, max_pages: int = 4, firm_name: str = "",
    ) -> tuple:
        """
        Crawl main page + discovered sub-pages.
        Returns (pages_dict_or_none, error_type).
        Error types: "dns", "connection", "tls", "other", or "" on success.

        Uses the browser pool when available; falls back to creating a
        fresh context when the pool is empty or not started.
        """
        pooled_crawler = None
        try:
            if self._pool_queue is not None:
                pooled_crawler = await self._pool_queue.get()
                crawler_ctx = pooled_crawler
            else:
                crawler_ctx = None

            if crawler_ctx:
                return await self._do_crawl(crawler_ctx, url, max_pages, firm_name)
            else:
                async with AsyncWebCrawler(config=self.browser_config) as crawler:
                    return await self._do_crawl(crawler, url, max_pages, firm_name)
        except Exception as e:
            err_type = self._classify_error(str(e))
            if err_type == "dns":
                logger.warning(f"DNS resolution failed for {url}")
            else:
                logger.error(f"Crawl error for {url} ({err_type}): {e}")
            return None, err_type
        finally:
            if pooled_crawler is not None and self._pool_queue is not None:
                self._pool_queue.put_nowait(pooled_crawler)

    async def _do_crawl(
        self, crawler: Any, url: str, max_pages: int, firm_name: str,
    ) -> tuple:
        """Execute the actual crawl logic using a given crawler instance."""
        hard_timeout = self.settings.page_timeout / 1000 + 10

        discovery_config = CrawlerRunConfig(
            page_timeout=self.settings.page_timeout,
            remove_overlay_elements=True,
        )
        try:
            main = await asyncio.wait_for(
                crawler.arun(url, config=discovery_config),
                timeout=hard_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(f"Hard timeout ({hard_timeout:.0f}s) on {url}")
            return None, "other"

        if not main.success:
            err_text = str(getattr(main, "error_message", ""))
            err_type = self._classify_error(err_text)
            logger.warning(f"Failed to crawl {url} ({err_type})")
            return None, err_type

        pages = {"main": main.markdown or ""}

        internal_links = self._extract_internal_links(main, url)

        if self.settings.llm_link_triage and len(internal_links) > 3 and firm_name:
            priority_urls = self._llm_triage_links(
                internal_links, firm_name, max_picks=max_pages - 1,
            )
            if not priority_urls:
                priority_urls = self._prioritize_links(internal_links, url)
        else:
            priority_urls = self._prioritize_links(internal_links, url)

        content_config = CrawlerRunConfig(
            page_timeout=self.settings.page_timeout,
            remove_overlay_elements=True,
            excluded_tags=["nav", "footer", "aside", "header"],
            remove_forms=True,
        )
        for sub_url in priority_urls[:max_pages - 1]:
            try:
                result = await asyncio.wait_for(
                    crawler.arun(sub_url, config=content_config),
                    timeout=hard_timeout,
                )
                if result.success and result.markdown:
                    pages[sub_url] = result.markdown
            except asyncio.TimeoutError:
                logger.debug(f"Hard timeout on sub-page {sub_url}")
            except Exception as e:
                logger.debug(f"Sub-page crawl failed {sub_url}: {e}")

        return pages, ""

    # ------------------------------------------------------------------
    # Link discovery and prioritization
    # ------------------------------------------------------------------

    def _extract_internal_links(self, result: Any, base_url: str) -> List[Dict[str, str]]:
        """Extract internal links with text context."""
        links_data = getattr(result, "links", {}) or {}
        internal = links_data.get("internal", [])
        enriched = []
        seen = set()

        for link in internal:
            if isinstance(link, dict):
                href = link.get("href", link.get("url", ""))
                text = link.get("text", link.get("content", "")).strip()
            else:
                href, text = str(link), ""

            href = href.split("#")[0]
            if not href or href in seen:
                continue

            abs_url = self._to_absolute(base_url, href)
            if abs_url and abs_url not in seen:
                seen.add(abs_url)
                enriched.append({"url": abs_url, "text": text.lower()})

        return enriched

    @staticmethod
    def _path_segments(path: str) -> List[str]:
        """Split a URL path into individual segments for keyword matching."""
        return [seg for seg in path.strip("/").split("/") if seg]

    def _prioritize_links(self, links: List[Dict[str, str]], base_url: str) -> List[str]:
        """Prioritize links: team/about pages first, then contact. Service pages excluded."""
        team, contact = [], []
        base_parsed = urlparse(base_url)
        base_domain = base_parsed.netloc.replace("www.", "")

        for link in links:
            text = link["text"]
            url_lower = link["url"].lower()
            path = urlparse(url_lower).path
            segments = self._path_segments(path)

            link_domain = urlparse(url_lower).netloc.replace("www.", "")
            if link_domain and link_domain != base_domain:
                continue

            if any(neg in seg for seg in segments for neg in NEGATIVE_PATH_KEYWORDS):
                continue

            if any(seg in TEAM_PATH_KEYWORDS for seg in segments) or \
               any(kw in text for kw in TEAM_TEXT_KEYWORDS):
                team.append(link["url"])
            elif any(seg in CONTACT_PATH_KEYWORDS for seg in segments) or \
                 any(kw in text for kw in CONTACT_TEXT_KEYWORDS):
                contact.append(link["url"])

        return team + contact

    def _to_absolute(self, base: str, href: str) -> Optional[str]:
        if not href or href.startswith(("javascript:", "mailto:", "tel:")):
            return None
        if href.startswith("http"):
            return href
        try:
            return urljoin(base, href)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # LLM-based link triage
    # ------------------------------------------------------------------

    def _llm_triage_links(
        self, links: List[Dict[str, str]], firm_name: str, max_picks: int = 3,
    ) -> List[str]:
        """Ask the LLM to pick which sub-pages are most likely to have DM info.

        Cheap and fast: sends just the link list (URL + anchor text), not page
        content.  Returns ordered list of URLs to crawl.
        """
        if not links:
            return []

        link_list = "\n".join(
            f"  {i+1}. URL: {l['url']}  |  text: \"{l['text']}\""
            for i, l in enumerate(links[:30])
        )

        try:
            response = self.llm_client.chat.completions.create(
                model=self.settings.openrouter_model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You select which pages on an accounting firm's website "
                            "are most likely to contain names, titles, and contact "
                            "details of senior staff (partners, directors, principals).\n\n"
                            "Return JSON: {\"urls\": [\"url1\", \"url2\", ...]}\n"
                            "Order by likelihood. Max {max} URLs.\n\n"
                            "GOOD pages: team, people, about-us, our-firm, staff, "
                            "directors, contact, meet-the-team\n"
                            "BAD pages: service descriptions, blog posts, tax guides, "
                            "FAQs, privacy policy, client portals, booking pages, "
                            "industry info, login pages.\n"
                            "If no page is likely to have DM info, return {\"urls\": []}"
                        ).format(max=max_picks),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Firm: {firm_name}\n"
                            f"Pick up to {max_picks} pages most likely to have "
                            f"decision maker info:\n\n{link_list}"
                        ),
                    },
                ],
                temperature=0.0,
                max_tokens=500,
                response_format={"type": "json_object"},
            )

            content = response.choices[0].message.content
            data = json.loads(content)

            urls = []
            if isinstance(data, list):
                urls = data
            elif isinstance(data, dict):
                for key in ("urls", "pages", "selected"):
                    if key in data and isinstance(data[key], list):
                        urls = data[key]
                        break
                if not urls:
                    seen = set()
                    for k, v in data.items():
                        for item in (k, v):
                            if isinstance(item, str) and item.startswith("http") \
                               and item not in seen:
                                seen.add(item)
                                urls.append(item)

            valid = [u for u in urls if isinstance(u, str) and u.startswith("http")]
            logger.info(f"LLM triage selected {len(valid)} pages for {firm_name}")
            return valid[:max_picks]

        except Exception as e:
            logger.debug(f"LLM link triage failed for {firm_name}: {e}")
            return []

    # ------------------------------------------------------------------
    # Content assembly with smart truncation
    # ------------------------------------------------------------------

    def _build_combined_content(self, pages: Dict[str, str]) -> str:
        """
        Assemble crawled pages into a single string for the LLM, with smart
        truncation that prioritizes team/about/contact pages over the main page.
        """
        priority_pages = {}
        main_content = pages.get("main", "")
        other_pages = {}

        for page_url, content in pages.items():
            if page_url == "main":
                continue
            segments = self._path_segments(urlparse(page_url.lower()).path)
            if any(seg in TEAM_PATH_KEYWORDS + CONTACT_PATH_KEYWORDS for seg in segments):
                priority_pages[page_url] = content
            else:
                other_pages[page_url] = content

        priority_budget = int(MAX_CONTENT_CHARS * PRIORITY_PAGE_BUDGET)
        priority_text = ""
        for page_url, content in priority_pages.items():
            chunk = f"=== {page_url} ===\n{content}\n\n"
            if len(priority_text) + len(chunk) > priority_budget:
                remaining = priority_budget - len(priority_text)
                if remaining > 200:
                    priority_text += chunk[:remaining] + "\n... (truncated)\n\n"
                break
            priority_text += chunk

        remaining_budget = MAX_CONTENT_CHARS - len(priority_text)

        main_text = f"=== Main Page ===\n{main_content}\n\n"
        other_text = ""
        for page_url, content in other_pages.items():
            other_text += f"=== {page_url} ===\n{content}\n\n"

        non_priority = main_text + other_text
        if len(non_priority) > remaining_budget:
            non_priority = non_priority[:remaining_budget] + "\n... (truncated)"

        return priority_text + non_priority

    # ------------------------------------------------------------------
    # LLM extraction with structured output + retry
    # ------------------------------------------------------------------

    async def _extract_with_llm_retry(
        self, url: str, firm_name: str, pages: Dict[str, str],
        max_retries: int = 2,
    ) -> Optional[EnrichmentData]:
        """Call LLM with retry on transient errors."""
        for attempt in range(max_retries + 1):
            result = await self._extract_with_llm(url, firm_name, pages)
            if result is not None:
                return result
            if attempt < max_retries:
                delay = self.settings.retry_delay * (2 ** attempt)
                logger.info(f"LLM retry {attempt + 1}/{max_retries} for {url} in {delay:.0f}s")
                await asyncio.sleep(delay)
        return None

    async def _extract_with_llm(
        self, url: str, firm_name: str, pages: Dict[str, str]
    ) -> Optional[EnrichmentData]:
        """Send crawled content to LLM for structured extraction."""
        combined = self._build_combined_content(pages)

        system_prompt = self._get_system_prompt()
        user_prompt = (
            f"Extract structured data from this accounting firm website.\n\n"
            f"Firm name (from directory): {firm_name}\n"
            f"URL: {url}\n\n"
            f"Website Content:\n{combined}\n\n"
            f"Be thorough but accurate. If information is missing, leave fields as empty strings or empty arrays."
        )

        try:
            response = self.llm_client.chat.completions.create(
                model=self.settings.openrouter_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=self.settings.llm_temperature,
                max_tokens=4000,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "enrichment_data",
                        "strict": True,
                        "schema": self._enrichment_schema,
                    },
                },
            )

            content = response.choices[0].message.content
            data = json.loads(content)
            llm_response = LLMEnrichmentResponse(**data)
            return self._to_enrichment_data(llm_response)

        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error for {url}: {e}")
        except Exception as e:
            logger.error(f"LLM extraction failed for {url}: {e}")

        return None

    def _to_enrichment_data(self, resp: LLMEnrichmentResponse) -> EnrichmentData:
        """Convert validated LLM response into EnrichmentData, normalizing phones."""
        dms = []
        for dm in resp.decision_makers[:self.settings.max_decision_makers]:
            if not dm.name:
                continue
            dms.append(DecisionMaker(
                name=dm.name,
                title=dm.title,
                summary=dm.decision_maker_summary,
                phone_office=normalize_to_e164(dm.phone_office),
                phone_mobile=normalize_to_e164(dm.phone_mobile),
                phone_direct=normalize_to_e164(dm.phone_direct),
                email=dm.email or None,
                linkedin=dm.linkedin or None,
            ))

        mobiles = [
            m for m in
            (normalize_to_e164(p) for p in resp.associated_mobile_numbers)
            if m
        ]

        org_raw = resp.organisational_structure.lower()
        org_mapped = None
        if "solo" in org_raw or "sole" in org_raw:
            org_mapped = "Solo practice"
        elif any(w in org_raw for w in ("enterprise", "large", "big 4")):
            org_mapped = "Enterprise"
        elif "franchise" in org_raw:
            org_mapped = "Franchised firm"
        elif org_raw:
            org_mapped = "SMB"

        return EnrichmentData(
            description=resp.description,
            edited_description=resp.edited_description,
            office_phone=normalize_to_e164(resp.office_phone) or None,
            office_email=resp.office_email or None,
            associated_emails=[e for e in resp.associated_emails if e],
            associated_mobiles=mobiles,
            associated_info=resp.associated_info,
            organisational_structure=org_mapped,
            linkedin=resp.linkedin or None,
            facebook=resp.facebook or None,
            decision_makers=dms,
            confidence_score=resp.confidence_score,
            out_of_scope=resp.out_of_scope,
            out_of_scope_reason=resp.out_of_scope_reason or None,
        )

    def _get_system_prompt(self) -> str:
        return (
            "You are a data analyst extracting factual firmographic data from "
            "Australian accounting firm websites. Your output will be read by "
            "sales reps during cold calls — it must be instantly useful.\n\n"

            "CRITICAL RULES:\n"
            "- Report ONLY facts stated on the website. Never invent or embellish.\n"
            "- Strip ALL marketing language. No adjectives like 'trusted', 'leading', "
            "'passionate', 'dedicated', 'expert', 'boutique', 'client-focused'.\n"
            "- If information is not on the site, leave the field as an empty string.\n\n"

            "DESCRIPTION: Write a factual summary of the firm. State what services "
            "they offer, where they are located, and who they serve. Do not copy "
            "taglines or mission statements.\n\n"

            "EDITED_DESCRIPTION: This is the field the rep reads while the phone is "
            "ringing. Use pipe-separated bullet points. Include:\n"
            "  - Suburb/city and state\n"
            "  - Core services (tax compliance, SMSF, audit, bookkeeping, BAS, etc.)\n"
            "  - Accounting software they use (Xero, MYOB, QuickBooks, Sage)\n"
            "  - Team size if stated\n"
            "  - Client types or industry niches they serve\n"
            "  - Professional body memberships (CAANZ, CPA, NTAA, IPA)\n"
            "Example: 'Dee Why NSW | Tax, SMSF, audit, BAS | Xero, MYOB | ~8 staff | "
            "Medical & trades clients | CAANZ, CPA members'\n\n"

            "DECISION MAKERS: Extract senior staff — Partner, Principal, Director, "
            "Managing Director, Senior Partner, Tax Partner, Audit Partner, Founder, "
            "Owner, Manager. Be permissive with senior titles. Exclude receptionists, "
            "admin staff, juniors, and graduates. "
            f"Extract up to {self.settings.max_decision_makers} decision makers.\n\n"

            "DECISION_MAKER_SUMMARY: For each person, write factual bullet points a "
            "rep can reference in conversation. Include:\n"
            "  - Qualifications (CA, CPA, NTAA fellow, BBus, etc.)\n"
            "  - Years at firm or in industry, if stated\n"
            "  - Specific responsibilities (e.g. 'heads SMSF division')\n"
            "  - Prior firms (e.g. 'ex-PwC')\n"
            "  - Industry specializations\n"
            "Do NOT write flowing prose. Use short factual fragments separated by '. '.\n"
            "Example: 'CA, CPA. 12 yrs at firm. Heads tax compliance. Ex-Deloitte. "
            "Specialises in medical practices.'\n\n"

            "ASSOCIATED_INFO: List factual supplementary details: professional body "
            "memberships, tax agent registration number, software stack (including "
            "add-on tools like Dext, Hubdoc, WorkflowMax), industry niches.\n\n"

            "PHONE NORMALIZATION: Australian numbers must be +61XXXXXXXXX format. "
            "New Zealand +64, UK +44.\n\n"

            "OUT OF SCOPE: Set out_of_scope to true if the business is NOT an "
            "accounting firm (e.g. completely unrelated business)."
        )

    # ------------------------------------------------------------------
    # Phase 2b: Web search fallback for decision maker discovery
    # ------------------------------------------------------------------

    _SEARCH_SITE_TARGETS = [
        "linkedin.com/in",
        "cpaaustralia.com.au",
        "charteredaccountantsanz.com",
        "ipa.com.au",
    ]

    def _build_search_query(self, firm_name: str, domain: str) -> str:
        """Build a targeted search query for finding DMs at an accounting firm."""
        site_clause = " OR ".join(f"site:{s}" for s in self._SEARCH_SITE_TARGETS[:3])
        return (
            f'"{firm_name}" ({site_clause}) '
            f"partner OR director OR principal OR founder accountant"
        )

    async def _search_for_decision_makers(
        self, firm_name: str, website_url: str,
    ) -> List[DecisionMaker]:
        """Use web search to find decision makers when the crawl found none."""
        domain = urlparse(
            website_url if website_url.startswith("http") else f"https://{website_url}"
        ).netloc.replace("www.", "")

        query = self._build_search_query(firm_name, domain)
        system_prompt = self._get_web_search_system_prompt(firm_name, domain)

        try:
            response = self.llm_client.chat.completions.create(
                model=self.settings.web_search_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": query},
                ],
                temperature=0.0,
                max_tokens=2000,
                extra_body={
                    "plugins": [{
                        "id": "web",
                        "max_results": self.settings.web_search_max_results,
                    }],
                },
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "web_search_dm",
                        "strict": True,
                        "schema": self._web_search_schema,
                    },
                },
            )

            content = response.choices[0].message.content
            data = json.loads(content)
            ws_resp = LLMWebSearchResponse(**data)
            return self._web_search_to_dms(ws_resp)

        except json.JSONDecodeError as e:
            logger.error(f"Web search JSON parse error for {firm_name}: {e}")
        except Exception as e:
            logger.error(f"Web search failed for {firm_name}: {e}")

        return []

    async def _web_search_enrichment(
        self, firm_name: str, website_url: str,
    ) -> Optional[EnrichmentData]:
        """Build a minimal EnrichmentData from web search alone (total crawl failure)."""
        domain = urlparse(
            website_url if website_url.startswith("http") else f"https://{website_url}"
        ).netloc.replace("www.", "")

        query = self._build_search_query(firm_name, domain)
        system_prompt = self._get_web_search_system_prompt(firm_name, domain)

        try:
            response = self.llm_client.chat.completions.create(
                model=self.settings.web_search_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": query},
                ],
                temperature=0.0,
                max_tokens=2000,
                extra_body={
                    "plugins": [{
                        "id": "web",
                        "max_results": self.settings.web_search_max_results,
                    }],
                },
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "web_search_dm",
                        "strict": True,
                        "schema": self._web_search_schema,
                    },
                },
            )

            content = response.choices[0].message.content
            data = json.loads(content)
            ws_resp = LLMWebSearchResponse(**data)
            dms = self._web_search_to_dms(ws_resp)

            return EnrichmentData(
                edited_description=ws_resp.brief,
                office_phone=normalize_to_e164(ws_resp.firm_phone) or None,
                office_email=ws_resp.firm_email or None,
                linkedin=ws_resp.firm_linkedin or None,
                decision_makers=dms,
                confidence_score=0.4,
            )

        except json.JSONDecodeError as e:
            logger.error(f"Web search JSON parse error for {firm_name}: {e}")
        except Exception as e:
            logger.error(f"Web search enrichment failed for {firm_name}: {e}")

        return None

    def _web_search_to_dms(self, ws_resp: LLMWebSearchResponse) -> List[DecisionMaker]:
        """Convert web search response people into DecisionMaker objects."""
        dms = []
        for person in ws_resp.people[:self.settings.max_decision_makers]:
            if not person.name:
                continue
            dms.append(DecisionMaker(
                name=person.name,
                title=person.title,
                summary=person.qualifications,
                phone_mobile=normalize_to_e164(person.phone),
                email=person.email or None,
                linkedin=person.linkedin or None,
            ))
        return dms

    def _get_web_search_system_prompt(self, firm_name: str, domain: str) -> str:
        return (
            "You are a research assistant finding senior decision makers at an "
            "Australian accounting firm. Use web search results to identify "
            "Partners, Directors, Principals, Founders, and Owners.\n\n"

            f"TARGET FIRM: {firm_name}\n"
            f"DOMAIN: {domain}\n\n"

            "RULES:\n"
            "- Only include people clearly associated with this specific firm.\n"
            "- Verify the firm name or domain matches before including a person.\n"
            "- Senior titles only: Partner, Director, Principal, Managing Director, "
            "Founder, Owner, Senior Manager. Exclude admin, juniors, graduates.\n"
            "- For LinkedIn results, extract the person's name, title, and profile URL.\n"
            "- For CPA/CAANZ/IPA directory results, note the qualification.\n"
            "- Phone numbers in E.164 format (+61XXXXXXXXX).\n"
            "- If you cannot confidently associate a person with this firm, exclude them.\n"
            "- The 'brief' field should only be filled if you find useful firmographic "
            "info (location, services, size) that supplements what we already have. "
            "Use pipe-separated format like: 'Parramatta NSW | Tax, SMSF | CPA member'\n"
            "- Do NOT fabricate information. Only report what appears in search results."
        )


# ------------------------------------------------------------------
# Public orchestration function
# ------------------------------------------------------------------

async def enrich_firms(
    settings: Settings,
    listings_with_urls: List[dict],
    checkpoint: Checkpoint,
    delay: float = 2.0,
) -> Dict[str, EnrichmentData]:
    """
    Enrich all firms that have website URLs.

    Args:
        settings: App settings.
        listings_with_urls: List of dicts with 'website_url' and 'name' keys.
        checkpoint: Checkpoint for resume.
        delay: Seconds between crawls.

    Returns:
        Dict mapping website_url to EnrichmentData.
    """
    enricher = WebsiteEnricher(settings)
    already_done = checkpoint.get_enriched_urls()
    results: Dict[str, EnrichmentData] = {}

    for url, data in checkpoint.get_all_enrichments().items():
        try:
            results[url] = EnrichmentData(**data)
        except Exception:
            pass

    remaining = [
        l for l in listings_with_urls
        if l["website_url"] not in already_done
    ]
    logger.info(f"Enriching {len(remaining)} firms ({len(already_done)} already done)")

    for i, listing in enumerate(remaining):
        url = listing["website_url"]
        name = listing["name"]

        logger.info(f"[{i+1}/{len(remaining)}] Enriching: {name} ({url})")
        try:
            enrichment = await enricher.enrich(url, name)
            if enrichment:
                results[url] = enrichment
                checkpoint.save_enrichment(url, enrichment.model_dump())
            else:
                checkpoint.mark_enriched(url)
        except Exception as e:
            logger.error(f"Enrichment failed for {url}: {e}")
            checkpoint.mark_enriched(url)

        await asyncio.sleep(delay)

    return results
