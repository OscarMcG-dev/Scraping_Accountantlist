"""
crawl4ai wrapper for accounting website crawling.
"""
import asyncio
from typing import Optional, List
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
from src.logger import get_logger

logger = get_logger(__name__)


class AccountingWebsiteCrawler:
    """Crawler for accounting firm websites using crawl4ai."""

    def __init__(self, settings):
        """
        Initialize the crawler.

        Args:
            settings: Application settings
        """
        self.settings = settings

        # Browser configuration
        self.browser_config = BrowserConfig(
            headless=True,
            viewport_width=1920,
            viewport_height=1080,
        )

        # Base crawler configuration
        self.base_config = CrawlerRunConfig(
            page_timeout=self.settings.page_timeout,
            remove_overlay_elements=True,
            # Exclude irrelevant sections for accounting sites
            excluded_tags=["nav", "footer", "aside", "header"],
            remove_forms=True,
        )

    async def crawl_main_page(self, url: str) -> Optional[dict]:
        """
        Crawl main page, extract content and discover bio page URLs.

        Args:
            url: Main page URL

        Returns:
            Dictionary with crawl data or None if failed
        """
        try:
            async with AsyncWebCrawler(config=self.browser_config) as crawler:
                result = await crawler.arun(url, config=self.base_config)

                if not result.success:
                    logger.warning(f"Crawl failed for {url}: {result.error_message if hasattr(result, 'error_message') else 'Unknown error'}")
                    return None

                # Extract links for team/bio pages (optional - many sites don't have them)
                team_links = self._discover_team_pages(result)

                return {
                    "url": url,
                    "html": result.html,
                    "markdown": result.markdown,
                    "links": result.links,
                    "team_links": team_links,  # Empty list if no team pages found
                    "bio_content": [],  # Bio content will be filled if we crawl team pages
                    "title": result.metadata.get("title") if result.metadata else None,
                    "description": result.metadata.get("description") if result.metadata else None,
                }

        except Exception as e:
            logger.error(f"Error crawling {url}: {e}")
            return None

    def _discover_team_pages(self, result) -> List[str]:
        """
        Discover URLs for team/bio pages from crawl result.

        Args:
            result: crawl4ai result object

        Returns:
            List of team/bio page URLs
        """
        team_links = []
        internal_links = result.links.get("internal", [])

        # Common patterns for team/about pages
        team_patterns = [
            "/team", "/about/team", "/our-people", "/our-team",
            "/staff", "/meet-the-team", "/partners", "/directors",
            "/about-us", "/about/our-team", "/people", "/meetyourteam",
            "/our-staff", "/team-members", "/our-partners"
        ]

        for link in internal_links:
            # Handle both string and dict formats
            if isinstance(link, dict):
                url = link.get("href", link.get("url", ""))
            else:
                url = str(link)

            for pattern in team_patterns:
                if pattern in url.lower():
                    # Only include unique links
                    if url not in team_links:
                        team_links.append(url)
                    break  # Found a match, move to next link

        logger.info(f"Discovered {len(team_links)} team/bio pages")
        return team_links

    async def crawl_bio_pages(self, team_links: List[str]) -> List[str]:
        """
        Crawl individual bio pages and return markdown content.

        Args:
            team_links: List of team/bio page URLs

        Returns:
            List of markdown content from bio pages
        """
        if not team_links:
            return []

        bio_content = []

        async with AsyncWebCrawler(config=self.browser_config) as crawler:
            # Use arun_many for concurrent crawling
            results = await crawler.arun_many(
                urls=team_links,
                config=self.base_config,
                max_concurrent=3  # Be more conservative for bio pages
            )

            for result in results:
                if result.success:
                    bio_content.append(result.markdown)
                else:
                    logger.debug(f"Failed to crawl bio page: {result.url}")

        return bio_content

    async def crawl_full_site(self, url: str) -> Optional[dict]:
        """
        Crawl main page + bio pages for complete data extraction.

        Args:
            url: Main page URL

        Returns:
            Dictionary with full crawl data or None if failed
        """
        main_result = await self.crawl_main_page(url)

        if not main_result:
            return None

        # Crawl bio pages if found
        if main_result.get("team_links"):
            logger.info(f"Crawling {len(main_result['team_links'])} bio pages for {url}")
            main_result["bio_content"] = await self.crawl_bio_pages(main_result["team_links"])
        else:
            main_result["bio_content"] = []

        return main_result

    async def check_url_accessible(self, url: str) -> bool:
        """
        Quick check if URL is accessible (for broken URL detection).

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
