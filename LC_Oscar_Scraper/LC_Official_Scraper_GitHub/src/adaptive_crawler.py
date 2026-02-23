"""
Adaptive crawler using Two-Tier LLM-guided intelligent page discovery.
Phase 1: Discover Hubs (Team, Contact, About).
Phase 2: Expand to deep links found on those Hubs (Individual Bios).
"""
import asyncio
import time
from typing import Optional, List, Dict, Any
from openai import AsyncOpenAI
from urllib.parse import urljoin, urlparse
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
from src.config import Settings
from src.link_analyzer import LinkAnalyzer
from src.logger import get_logger, log_crawl_start, log_crawl_success, log_crawl_failure, log_http_fallback

logger = get_logger(__name__)

class AdaptiveWebsiteCrawler:
    def __init__(self, settings: Settings, llm_client: Optional[AsyncOpenAI] = None):
        self.settings = settings
        self.link_analyzer = LinkAnalyzer(settings, llm_client)
        
        # Optimized Browser Config
        self.browser_config = BrowserConfig(
            headless=True,
            viewport_width=1920,
            viewport_height=1080,
            ignore_https_errors=True,
            text_mode=True,         # High-speed text-only
            light_mode=True,        # Disable background features
            enable_stealth=True,    # Bypass basic bot checks
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        
        # Config for deep pages (Bio pages) - keep everything to avoid missing numbers
        self.base_config = CrawlerRunConfig(
            page_timeout=self.settings.page_timeout,
            remove_overlay_elements=True,
            excluded_tags=["nav", "aside"], # Keep footer/header as they often have phone numbers
            remove_forms=False,             # Forms sometimes contain direct contact info
            word_count_threshold=10,
            exclude_external_images=True,
            exclude_social_media_links=True
        )
        
        # Config for discovery (Homepage/Hubs)
        self.discovery_config = CrawlerRunConfig(
            page_timeout=self.settings.page_timeout,
            remove_overlay_elements=True,
            exclude_external_images=True,
            exclude_social_media_links=True
        )

    async def crawl_intelligently(
        self,
        url: str,
        max_pages: int = 15,
        crawl_strategy: str = "adaptive"
    ) -> Optional[Dict[str, Any]]:
        """
        Crawl using Two-Tier strategy: Homepage -> Hubs -> Bios.
        """
        log_crawl_start(logger, url, crawl_strategy, max_pages)

        # Tier 1: Main Page
        main_result = await self.crawl_main_page(url)
        if not main_result:
            return None

        # Tier 1 Analysis: Find Hubs (Team, Contact, About)
        internal_links = self._extract_internal_links_with_context(main_result)
        
        if crawl_strategy != "adaptive":
            priority_urls = self._basic_link_discovery(internal_links)
        else:
            try:
                link_analysis = await self.link_analyzer.analyze_links(url, internal_links)
                priority_urls = link_analysis.get("priority_order", [])
            except Exception as e:
                logger.warning(f"LLM analysis failed for {url}: {e}, using basic discovery")
                priority_urls = self._basic_link_discovery(internal_links)

        # Tier 2: Crawl Hubs and Sniff for BIOS
        hubs_to_crawl = priority_urls[:5] # Top 5 most promising hubs
        logger.info(f"Tier 1: Crawling {len(hubs_to_crawl)} hub pages")
        
        hub_results = await self.crawl_sub_pages(url, hubs_to_crawl)
        
        crawled_urls = {url}
        for res in hub_results:
            crawled_urls.add(res['url'])

        # Dynamic Expansion: Look for individual bio links on the hub pages
        deep_links = []
        bio_patterns = ['/team/', '/people/', '/staff/', '/profile/', '/about/']
        
        for hub_res in hub_results:
            hub_links = self._extract_internal_links_with_context(hub_res)
            # Find links that look like individual profiles
            for link in hub_links:
                link_url = link['url']
                # Sniff test: URL is long (deep) or matches bio patterns
                if any(pattern in link_url.lower() for pattern in bio_patterns) or link_url.count('/') >= 3:
                    if link_url not in crawled_urls:
                        deep_links.append(link_url)

        # Fill remaining capacity with deep links
        remaining_slots = max_pages - len(hub_results) - 1
        if deep_links and remaining_slots > 0:
            unique_bios = list(dict.fromkeys(deep_links))[:remaining_slots]
            logger.info(f"Tier 2: Expanding to {len(unique_bios)} individual bio pages")
            bio_results = await self.crawl_sub_pages(url, unique_bios)
            all_sub_results = hub_results + bio_results
        else:
            all_sub_results = hub_results

        return self._build_crawl_result(main_result, all_sub_results, "two-tier-adaptive")

    async def crawl_main_page(self, url: str) -> Optional[Dict[str, Any]]:
        try:
            start_time = time.perf_counter_ns()
            async with AsyncWebCrawler(config=self.browser_config) as crawler:
                result = await crawler.arun(url, config=self.discovery_config)
                if not result.success:
                    log_crawl_failure(logger, url, "NavigationError", result.error_message)
                    return None

                log_crawl_success(logger, url, 1, 0.0, int((time.perf_counter_ns() - start_time) / 1_000_000))
                return {
                    "url": url, "html": result.html, "markdown": result.markdown,
                    "links": result.links, "title": result.metadata.get("title") if result.metadata else None,
                    "success": True
                }
        except Exception as e:
            log_crawl_failure(logger, url, "InternalError", str(e))
            return None

    async def crawl_sub_pages(self, base_url: str, urls_to_crawl: List[str]) -> List[Dict[str, Any]]:
        if not urls_to_crawl: return []
        
        normalized_urls = list(dict.fromkeys([u for u in [self._normalize_url(base_url, url) for url in urls_to_crawl] if u and u.startswith("http")]))
        
        async with AsyncWebCrawler(config=self.browser_config) as crawler:
            semaphore = asyncio.Semaphore(self.settings.sub_page_concurrency_limit)

            async def crawl_with_limit(url) -> Dict[str, Any]:
                async with semaphore:
                    try:
                        result = await crawler.arun(url, config=self.base_config)
                        return {
                            "url": url, "markdown": result.markdown if result.success else "",
                            "success": result.success
                        }
                    except Exception:
                        return {"url": url, "markdown": "", "success": False}

            results = await asyncio.gather(*[crawl_with_limit(url) for url in normalized_urls])
        return [r for r in results if r.get("success")]

    def _extract_internal_links_with_context(self, crawl_result: Dict[str, Any]) -> List[Dict[str, str]]:
        links_data = crawl_result.get("links", {})
        internal_links = links_data.get("internal", [])
        enriched_links = []
        for link in internal_links:
            url = link.get("href", link.get("url", "")).split("#")[0]
            text = link.get("text", link.get("content", "")).strip()
            if url: enriched_links.append({"url": url, "text": text})
        
        seen = set()
        return [x for x in enriched_links if not (x['url'] in seen or seen.add(x['url']))]

    def _normalize_url(self, base_url: str, link: str) -> Optional[str]:
        if not link or any(link.startswith(x) for x in ["javascript:", "mailto:", "tel:"]): return None
        if link.startswith("http"): return link
        try:
            absolute = urljoin(base_url, link)
            parsed = urlparse(absolute)
            return absolute if parsed.scheme and parsed.netloc else None
        except Exception: return None

    def _build_crawl_result(self, main_result: Dict[str, Any], sub_results: List[Dict[str, Any]], strategy: str) -> Dict[str, Any]:
        all_content = {"main": main_result.get("markdown", "")}
        for sub in sub_results:
            all_content[sub.get("url", "")] = sub.get("markdown", "")
        return {
            "url": main_result.get("url"), "strategy": strategy,
            "main_page": main_result.get("markdown", ""), "sub_pages": all_content,
            "pages_crawled": 1 + len(sub_results)
        }

    def _basic_link_discovery(self, internal_links: List[Dict[str, str]]) -> List[str]:
        team_keywords = ['team', 'people', 'staff', 'meet', 'about', 'contact']
        prioritized = [l['url'] for l in internal_links if any(kw in l['text'].lower() or kw in l['url'].lower() for kw in team_keywords)]
        return list(dict.fromkeys(prioritized))
