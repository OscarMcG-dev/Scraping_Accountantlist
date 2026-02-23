"""
Adaptive crawler using LLM-guided intelligent page discovery.
Replaces brittle hard-coded URL patterns with semantic understanding.
"""
import asyncio
import time
from typing import Optional, List, Dict, Any
from openai import AsyncOpenAI


__all__ = ["AdaptiveWebsiteCrawler"]
from urllib.parse import urljoin, urlparse
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
from src.config import Settings
from src.link_analyzer import LinkAnalyzer
from src.failure_classifier import FailureClassifier
from src.logger import get_logger, log_crawl_start, log_crawl_success, log_crawl_failure, log_http_fallback

logger = get_logger(__name__)


class AdaptiveWebsiteCrawler:
    """
    Intelligent crawler that uses LLM to discover relevant pages.
    Instead of relying on hard-coded URL patterns, this crawler:
    1. Extracts all internal links from the main page
    2. Uses LLM to analyze and classify links by relevance
    3. Prioritizes crawling based on semantic understanding
    4. Handles non-standard URL patterns gracefully
    """

    def __init__(self, settings: Settings, llm_client: Optional[AsyncOpenAI] = None):
        """
        Initialize the adaptive crawler.

        Args:
            settings: Application settings
            llm_client: Optional pre-configured OpenAI client (for shared connection)
        """
        self.settings = settings
        self.link_analyzer = LinkAnalyzer(settings, llm_client)
        # Browser configuration
        self.browser_config = BrowserConfig(
            headless=True,
            viewport_width=1920,
            viewport_height=1080,
            ignore_https_errors=True,
            text_mode=True,
            light_mode=True,
            enable_stealth=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        # Base crawler configuration
        self.base_config = CrawlerRunConfig(
            page_timeout=self.settings.page_timeout,
            remove_overlay_elements=True,
            excluded_tags=[],
            remove_forms=True,
            exclude_external_images=True
        )
        # Config for link discovery (keep navigation elements)
        self.discovery_config = CrawlerRunConfig(
            page_timeout=self.settings.page_timeout,
            remove_overlay_elements=True,
            exclude_external_images=True,
            # Keep navigation, footer to extract links with context
        )

    async def crawl_intelligently(
        self,
        url: str,
        max_pages: int = 10,
        crawl_strategy: str = "adaptive"
    ) -> Optional[Dict[str, Any]]:
        """
        Crawl website using intelligent page discovery.

        Args:
            url: Main page URL
            max_pages: Maximum number of pages to crawl (including main page)
            crawl_strategy: "adaptive" (LLM-guided), "greedy" (all links), or "main_only"

        Returns:
            Dictionary with all crawled content and metadata
        """
        log_crawl_start(logger, url, crawl_strategy, max_pages)

        # Step 1: Crawl main page and extract links
        main_result = await self.crawl_main_page(url)
        if not main_result:
            return None

        # Extract internal links with full context
        internal_links = self._extract_internal_links_with_context(main_result)
        logger.info(f"Found {len(internal_links)} internal links on main page")

        # Step 2: Determine which pages to crawl
        if crawl_strategy == "main_only":
            # Basic link discovery - prioritize likely team/about pages
            priority_urls = self._basic_link_discovery(internal_links)
            logger.info(f"Basic discovery: will crawl {len(priority_urls)} prioritized pages")
        elif crawl_strategy == "greedy":
            # Crawl all internal links (fallback)
            priority_urls = [self._normalize_url(url, link)
                            for link in internal_links]
            logger.info(f"Greedy strategy: will crawl all {len(priority_urls)} links")
        elif crawl_strategy == "adaptive":
            # Use LLM to analyze and prioritize links
            try:
                link_analysis = await self.link_analyzer.analyze_links(
                    company_url=url,
                    internal_links=internal_links
                )
                priority_urls = link_analysis.get("priority_order", [])
                logger.info(
                    f"LLM analysis: {len(link_analysis.get('team_links', []))} team links, "
                    f"{len(priority_urls)} total to crawl"
                )
            except Exception as e:
                # LLM analysis failed - fallback to basic discovery
                logger.warning(f"LLM analysis failed for {url}: {e}, using basic discovery")
                priority_urls = self._basic_link_discovery(internal_links)
        else:
            raise ValueError(f"Unknown crawl strategy: {crawl_strategy}")

        # Step 3: Crawl prioritized pages
        pages_to_crawl = priority_urls[:max_pages - 1]  # -1 because we already have main page
        if not pages_to_crawl:
            logger.info("No additional pages to crawl")
            return self._build_crawl_result(main_result, [], crawl_strategy)

        logger.info(f"Crawling {len(pages_to_crawl)} sub-pages")
        sub_results = await self.crawl_sub_pages(url, pages_to_crawl)

        # Step 4: Build comprehensive result
        return self._build_crawl_result(main_result, sub_results, crawl_strategy)

    def _basic_link_discovery(self, internal_links: List[Dict[str, str]]) -> List[str]:
        """
        Basic link discovery without LLM - prioritize likely team/about pages.
        This is a faster fallback when LLM analysis fails or times out.

        Args:
            internal_links: List of internal link dictionaries

        Returns:
            List of prioritized URLs
        """
        prioritized = []

        # Keywords for team/about pages (highest priority)
        team_keywords = ['team', 'our-people', 'people', 'staff', 'our-team', 'meet', 'about', 'about-us', 'who-we-are']
        # Keywords for service pages (medium priority)
        service_keywords = ['services', 'service', 'what-we-do']

        for link in internal_links[:self.settings.max_links_for_basic_discovery]:  # Limit to avoid too many pages
            url = link.get("url", "")
            text = link.get("text", "").lower()

            # Check for team/people pages (highest priority)
            if any(kw in text for kw in team_keywords):
                if url and url not in prioritized:
                    prioritized.append(url)

        # Add service pages (medium priority)
        for link in internal_links[20:50]:
            url = link.get("url", "")
            text = link.get("text", "").lower()

            if any(kw in text for kw in service_keywords):
                if url and url not in prioritized:
                    prioritized.append(url)

        # Ensure no duplicates
        prioritized = list(dict.fromkeys(prioritized))

        return prioritized

    async def crawl_main_page(self, url: str) -> Optional[Dict[str, Any]]:
        """
        Crawl main page with full link context.

        Args:
            url: Main page URL

        Returns:
            Crawl result or None if failed
        """
        try:
            start_time = time.perf_counter_ns()
            async with AsyncWebCrawler(config=self.browser_config) as crawler:
                # Use discovery config to keep navigation elements
                result = await crawler.arun(url, config=self.discovery_config)
                if not result.success:
                    error_msg = result.error.message if hasattr(result.error, 'message') else 'Unknown error'
                    log_crawl_failure(logger, url, "NavigationError", error_msg)
                    return None

                log_crawl_success(logger, url, 1, 0.0, int((time.perf_counter_ns() - start_time) / 1_000_000))
                return {
                    "url": url,
                    "html": result.html,
                    "markdown": result.markdown,
                    "links": result.links,
                    "title": result.metadata.get("title") if result.metadata else None,
                    "description": result.metadata.get("description") if result.metadata else None,
                    "success": True
                }
        except Exception as e:
            logger.error(f"Error crawling main page {url}: {e}")
            return None

    def _extract_internal_links_with_context(self, crawl_result: Dict[str, Any]) -> List[Dict[str, str]]:
        """
        Extract internal links with their text/context from crawl result.

        Args:
            crawl_result: Crawl result dictionary

        Returns:
            List of dictionaries with url, text, and context
        """
        links_data = crawl_result.get("links", {})
        internal_links = links_data.get("internal", [])

        if not internal_links:
            return []

        # Extract links with context from HTML
        # This requires parsing HTML - for now, use basic link info
        enriched_links = []
        for link in internal_links:
            # Handle both dict and string formats
            if isinstance(link, dict):
                url = link.get("href", link.get("url", ""))
                text = link.get("text", link.get("content", "")).strip()
            else:
                url = str(link)
                text = ""

            # Clean URL (remove fragments)
            url = url.split("#")[0] if "#" in url else url

            if url:  # Skip empty URLs
                enriched_links.append({
                    "url": url,
                    "text": text,
                })

        # Deduplicate by URL
        seen_urls = set()
        unique_links = []
        for link in enriched_links:
            if link["url"] not in seen_urls:
                seen_urls.add(link["url"])
                unique_links.append(link)

        return unique_links

    async def crawl_sub_pages(
        self,
        base_url: str,
        urls_to_crawl: List[str]
    ) -> List[Dict[str, Any]]:
        """
        Crawl multiple sub-pages concurrently.

        Args:
            base_url: Base URL for normalizing relative URLs
            urls_to_crawl: List of URLs to crawl

        Returns:
            List of crawl results
        """
        if not urls_to_crawl:
            return []

        # Normalize URLs
        normalized_urls = [self._normalize_url(base_url, url) for url in urls_to_crawl]

        # Filter out non-HTTP URLs and duplicates
        normalized_urls = [u for u in normalized_urls if u and u.startswith("http")]
        normalized_urls = list(dict.fromkeys(normalized_urls))

        if not normalized_urls:
            return []

        logger.info(f"Crawling {len(normalized_urls)} sub-pages")

        results = []
        async with AsyncWebCrawler(config=self.browser_config) as crawler:
            # Crawl with controlled concurrency
            semaphore = asyncio.Semaphore(self.settings.sub_page_concurrency_limit)

            async def crawl_with_limit(url) -> Dict[str, Any]:
                async with semaphore:
                    try:
                        result = await crawler.arun(url, config=self.base_config)
                        return {
                            "url": url,
                            "markdown": result.markdown if result.success else "",
                            "success": result.success,
                            "error": result.error_message if hasattr(result, 'error_message') else ""
                        }
                    except Exception as e:
                        logger.debug(f"Failed to crawl {url}: {e}")
                        return {
                            "url": url,
                            "markdown": "",
                            "success": False,
                            "error": str(e)
                        }

            results = await asyncio.gather(
                *[crawl_with_limit(url) for url in normalized_urls],
                return_exceptions=False
            )

        # Filter successful results
        successful = [r for r in results if r.get("success")]

        logger.info(f"Successfully crawled {len(successful)} of {len(normalized_urls)} sub-pages")
        return successful

    def _normalize_url(self, base_url: str, link: str) -> Optional[str]:
        """
        Normalize relative URLs to absolute URLs.

        Args:
            base_url: Base URL
            link: Link (could be relative or absolute)

        Returns:
            Absolute URL or None if invalid
        """
        if not link:
            return None

        # Skip non-HTTP(S) URLs
        if link.startswith("javascript:") or link.startswith("mailto:") or link.startswith("tel:"):
            return None

        # If already absolute, return as-is
        if link.startswith("http://") or link.startswith("https://"):
            return link

        try:
            # Convert relative to absolute
            absolute = urljoin(base_url, link)
            # Validate URL format
            parsed = urlparse(absolute)
            if not parsed.scheme or not parsed.netloc:
                return None
            return absolute
        except Exception:
            return None

    def _build_crawl_result(
        self,
        main_result: Dict[str, Any],
        sub_results: List[Dict[str, Any]],
        strategy: str
    ) -> Dict[str, Any]:
        """
        Build comprehensive crawl result.

        Args:
            main_result: Main page crawl result
            sub_results: List of sub-page crawl results
            strategy: Crawl strategy used

        Returns:
            Comprehensive result dictionary
        """
        # Combine all markdown content
        all_content = {
            "main": main_result.get("markdown", ""),
        }
        # Add sub-page content
        for sub in sub_results:
            url = sub.get("url", "")
            all_content[url] = sub.get("markdown", "")

        return {
            "url": main_result.get("url"),
            "strategy": strategy,
            "main_page": main_result.get("markdown", ""),
            "sub_pages": all_content,
            "pages_crawled": 1 + len(sub_results),
            "title": main_result.get("title"),
            "description": main_result.get("description"),
        }

    async def check_url_accessible(self, url: str) -> bool:
        """
        Quick check if URL is accessible.

        Args:
            url: URL to check

        Returns:
            True if accessible, False otherwise
        """
        try:
            async with AsyncWebCrawler(config=self.browser_config) as crawler:
                result = await crawler.arun(
                    url,
                    config=CrawlerRunConfig(
                        page_timeout=10000,  # Quick check
                        bypass_cache=True,
                    )
                )
                return result.success
        except Exception as e:
            logger.debug(f"URL not accessible: {url} - {e}")
            return False

    async def try_url_with_fallback(self, url: str) -> tuple[Optional[str], bool]:
        """
        Try HTTPS first, automatically fallback to HTTP on failure.

        Args:
            url: URL to try

        Returns:
            Tuple of (working_url, was_https_fallback)
                working_url: URL that succeeded (or None if both failed)
                was_https_fallback: True if HTTP succeeded when HTTPS failed
        """
        # Ensure URL has scheme
        if not url.startswith('http://') and not url.startswith('https://'):
            url = f'https://{url}'

        # Try HTTPS first
        logger.info(f'Trying HTTPS: {url}')
        try:
            async with AsyncWebCrawler(config=self.browser_config) as crawler:
                result = await crawler.arun(
                    url,
                    config=CrawlerRunConfig(
                        page_timeout=self.settings.page_timeout,
                        bypass_cache=True,
                    )
                )
                if result.success:
                    logger.info(f'HTTPS succeeded: {url}')
                    log_http_fallback(logger, url, True, True)
                    return url, False
        except Exception as e:
            logger.debug(f'HTTPS attempt failed: {e}')

        # Try HTTP if HTTPS failed
        if url.startswith('https://'):
            http_url = url.replace('https://', 'http://', 1)
            logger.info(f'Trying HTTP fallback: {http_url}')
            try:
                async with AsyncWebCrawler(config=self.browser_config) as crawler:
                    result = await crawler.arun(
                        http_url,
                        config=CrawlerRunConfig(
                            page_timeout=self.settings.page_timeout,
                            bypass_cache=True,
                        )
                    )
                    if result.success:
                        logger.info(f'HTTP fallback succeeded: {http_url}')
                        log_http_fallback(logger, http_url, False, True)
                        return http_url, True
            except Exception as e:
                logger.debug(f'HTTP fallback failed: {e}')

        # Both failed
        logger.warning(f'Both HTTPS and HTTP attempts failed for: {url}')
        return None, False
