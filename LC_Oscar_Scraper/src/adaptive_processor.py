"""
Adaptive processor using intelligent LLM-guided crawling.
Replaces brittle pattern-based discovery with semantic understanding.
"""
import asyncio
from typing import Optional, List, Tuple
from urllib.parse import urlparse
from pathlib import Path

from src.config import Settings
from src.schemas import (
    CompanyData, OutOfScopeRecord, LowConfidenceRecord,
    LLMExtractionResult, BusinessSegment
)
from src.adaptive_crawler import AdaptiveWebsiteCrawler
from src.failure_classifier import FailureClassifier
from src.llm_extractor import LLMExtractor
from src.export import CSVExporter
from src.logger import get_logger
from src.checkpoint_manager import CheckpointManager
from src.progress_tracker import ProgressTracker

logger = get_logger(__name__)


class AdaptiveScraperProcessor:
    """
    Orchestrates adaptive scraping with intelligent page discovery.

    This processor uses LLM-guided crawling to handle websites with
    non-standard URL patterns and varying complexity.
    """

    def __init__(
        self,
        settings: Settings,
        crawl_strategy: str = "adaptive",
        max_pages: int = 5,
        checkpoint_manager: Optional[CheckpointManager] = None
    ):
        """
        Initialize adaptive processor.

        Args:
            settings: Application settings
            crawl_strategy: "adaptive" (LLM-guided), "greedy" (all links), or "main_only"
            max_pages: Maximum number of pages to crawl per website
            checkpoint_manager: Optional checkpoint manager for resumable processing
        """
        self.settings = settings
        self.crawl_strategy = crawl_strategy
        self.max_pages = max_pages
        self.checkpoint_manager = checkpoint_manager

        self.crawler = AdaptiveWebsiteCrawler(settings)
        self.llm_extractor = LLMExtractor(settings)

    async def process_url(self, url: str) -> Tuple[Optional[CompanyData], Optional[OutOfScopeRecord], Optional[LowConfidenceRecord]]:
        """
        Process a single URL using adaptive crawling with fallback.

        Args:
            url: URL to scrape

        Returns:
            Tuple of (CompanyData, OutOfScopeRecord, LowConfidenceRecord)
            Only one of these will be non-None
        """
        logger.info(f"Processing {url} with {self.crawl_strategy} strategy")

        # Validate URL format
        if not self._is_valid_url(url):
            logger.warning(f"Invalid URL format: {url}")
            return None, None, LowConfidenceRecord(
                company_url=url,
                company_name=None,
                confidence_score=0.0,
                reason="Invalid URL format"
            )

        # Step 1: Intelligently crawl the website with fallback
        crawl_result = await self._crawl_with_fallback(url)

        if not crawl_result:
            record = LowConfidenceRecord(
                company_url=url,
                company_name=None,
                confidence_score=0.0,
                reason="Crawl failed - site not accessible or timeout"
            )
            # Record checkpoint as broken
            if self.checkpoint_manager:
                self.checkpoint_manager.record_url_processed(url, "broken")
            return None, None, record

        logger.info(
            f"Successfully crawled {crawl_result['pages_crawled']} pages for {url} "
            f"(strategy: {crawl_result['strategy']})"
        )

        # Step 2: Extract with LLM using all crawled content
        combined_content = crawl_result.get("markdown", "")

        # Extract team/bio pages specifically if available
        sub_pages = crawl_result.get("sub_pages", {})
        bio_content = []
        for page_url, page_content in sub_pages.items():
            if page_url == "main":
                continue
            # Include all sub-pages as potential bio content
            bio_content.append(page_content)

        llm_result = await self.llm_extractor.extract(
            company_url=url,
            main_content=crawl_result.get("main_page", ""),
            bio_content=bio_content
        )

        # Step 3: Normalize and validate
        llm_result = self.llm_extractor.normalize_llm_output(
            llm_result,
            self.settings.default_country
        )

        # Step 4: Handle based on results
        # Check if out of scope
        if llm_result.out_of_scope:
            logger.info(f"Out of scope: {url} - {llm_result.out_of_scope_reason}")
            record = OutOfScopeRecord(
                company_url=url,
                company_name=llm_result.company_name,
                reason=llm_result.out_of_scope_reason or "Out of scope",
                confidence_score=llm_result.confidence_score
            )
            # Record checkpoint
            if self.checkpoint_manager:
                self.checkpoint_manager.record_url_processed(url, "out_of_scope", record.model_dump())
            return None, record, None

        # Check if low confidence
        if llm_result.confidence_score < self.settings.min_confidence_threshold:
            logger.warning(f"Low confidence for {url}: {llm_result.confidence_score}")
            reason = f"Confidence score {llm_result.confidence_score} below threshold"
            if not llm_result.company_name:
                reason += "; missing company name"
            record = LowConfidenceRecord(
                company_url=url,
                company_name=llm_result.company_name,
                confidence_score=llm_result.confidence_score,
                reason=reason
            )
            # Record checkpoint
            if self.checkpoint_manager:
                self.checkpoint_manager.record_url_processed(url, "low_confidence", record.model_dump())
            return None, None, record

        # Convert to final CompanyData
        try:
            company_data = self._convert_to_company_data(url, llm_result)
            logger.info(f"Successfully processed: {url} (confidence: {llm_result.confidence_score})")
            # Record checkpoint
            if self.checkpoint_manager:
                self.checkpoint_manager.record_url_processed(url, "successful", company_data.model_dump())
            return company_data, None, None
        except Exception as e:
            logger.error(f"Failed to convert to CompanyData for {url}: {e}")
            record = LowConfidenceRecord(
                company_url=url,
                company_name=llm_result.company_name,
                confidence_score=llm_result.confidence_score,
                reason=f"Validation error: {str(e)}"
            )
            # Record checkpoint
            if self.checkpoint_manager:
                self.checkpoint_manager.record_url_processed(url, "low_confidence", record.model_dump())
            return None, None, record

    async def _crawl_with_fallback(self, url: str) -> Optional[dict]:
        """
        Crawl with adaptive strategy, falling back to main_only on timeout/failure.

        Args:
            url: URL to crawl

        Returns:
            Crawl result or None
        """
        try:
            # First attempt: Try with original strategy
            logger.info(f"First attempt: {url} with {self.crawl_strategy} strategy")
            result = await self.crawler.crawl_intelligently(
                url=url,
                max_pages=self.max_pages,
                crawl_strategy=self.crawl_strategy
            )

            if result and result.get("pages_crawled", 0) > 0:
                # Success - return result
                return result
            elif not result:
                # Complete failure - try with main_only as fallback
                logger.warning(f"Primary strategy failed for {url}, trying main_only fallback")
                result = await self.crawler.crawl_intelligently(
                    url=url,
                    max_pages=self.max_pages,
                    crawl_strategy="main_only"
                )
                return result
            else:
                # Got some data (main page) but sub-pages failed
                # This is still useful - return it
                logger.info(f"Partial success for {url}: got main page, sub-pages may be incomplete")
                return result

        except Exception as e:
            # Unexpected error - try main_only as last resort
            logger.error(f"Error during primary crawl of {url}: {e}, trying main_only fallback")
            try:
                result = await self.crawler.crawl_intelligently(
                    url=url,
                    max_pages=self.max_pages,
                    crawl_strategy="main_only"
                )
                return result
            except Exception as e2:
                logger.error(f"Fallback also failed for {url}: {e2}")
                return None

    def _is_valid_url(self, url: str) -> bool:
        """
        Check if URL is valid format.

        Args:
            url: URL to validate

        Returns:
            True if valid URL format
        """
        try:
            result = urlparse(url)
            return all([result.scheme in ['http', 'https'], result.netloc])
        except Exception:
            return False

    def _convert_to_company_data(self, url: str, llm_result: LLMExtractionResult) -> CompanyData:
        """
        Convert LLM result to CompanyData.

        Args:
            url: Original URL
            llm_result: LLM extraction result

        Returns:
            CompanyData instance
        """
        data_dict = llm_result.model_dump()

        # Add URL
        data_dict["company_url"] = url

        # Remove fields not in CompanyData
        data_dict.pop("out_of_scope", None)
        data_dict.pop("out_of_scope_reason", None)

        # Create CompanyData (validates schema)
        return CompanyData(**data_dict)

    async def process_batch(
        self,
        urls: List[str],
        enable_progress_tracking: bool = True
    ) -> Tuple[List[CompanyData], List[OutOfScopeRecord], List[LowConfidenceRecord], List[str]]:
        """
        Process multiple URLs concurrently.

        Args:
            urls: List of URLs to process
            enable_progress_tracking: Whether to enable real-time progress tracking

        Returns:
            Tuple of (successful, out_of_scope, low_confidence, broken_urls)
        """
        logger.info(f"Processing batch of {len(urls)} URLs")

        # Initialize progress tracker
        tracker = ProgressTracker(len(urls)) if enable_progress_tracking else None

        # Process with controlled concurrency
        semaphore = asyncio.Semaphore(self.settings.max_concurrent_crawls)

        async def process_with_semaphore(url):
            async with semaphore:
                try:
                    # Add delay between requests to avoid rate limiting
                    await asyncio.sleep(self.settings.delay_between_requests)
                    result = await self.process_url(url)

                    # Update progress tracker
                    if tracker:
                        if result[0]:  # successful
                            tracker.update("successful", url)
                        elif result[1]:  # out of scope
                            tracker.update("out_of_scope", url)
                        elif result[2]:  # low confidence or broken
                            if "crawl failed" in result[2].reason.lower():
                                tracker.update("broken", url)
                            else:
                                tracker.update("low_confidence", url)

                    return result
                except Exception as e:
                    logger.error(f"Error processing {url}: {e}")
                    # Update progress tracker
                    if tracker:
                        tracker.update("broken", url)
                    # Return as low confidence
                    return None, None, LowConfidenceRecord(
                        company_url=url,
                        company_name=None,
                        confidence_score=0.0,
                        reason=f"Processing error: {str(e)}"
                    )

        # Process all URLs
        results = await asyncio.gather(
            *[process_with_semaphore(url) for url in urls],
            return_exceptions=False
        )

        # Separate results
        successful = []
        out_of_scope = []
        low_confidence = []
        broken = []

        for result in results:
            company_data, oos_record, lc_record = result
            if company_data:
                successful.append(company_data)
            elif oos_record:
                out_of_scope.append(oos_record)
            elif lc_record:
                # If it's a crawl failure, add to broken
                if "crawl failed" in lc_record.reason.lower():
                    broken.append(lc_record.company_url)
                else:
                    low_confidence.append(lc_record)

        # Log final progress summary
        if tracker:
            tracker.log_summary()

        logger.info(
            f"Batch complete: {len(successful)} successful, "
            f"{len(out_of_scope)} out of scope, "
            f"{len(low_confidence)} low confidence, "
            f"{len(broken)} broken"
        )

        return successful, out_of_scope, low_confidence, broken
