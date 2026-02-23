"""
Main processor orchestrating the scraping pipeline.
"""
import asyncio
from typing import Optional, List, Tuple
from urllib.parse import urlparse

from src.config import Settings
from src.schemas import (
    CompanyData, OutOfScopeRecord, LowConfidenceRecord,
    LLMExtractionResult, BusinessSegment
)
from src.crawler import AccountingWebsiteCrawler
from src.llm_extractor import LLMExtractor
from src.phone_utils import normalize_to_e164
from src.logger import get_logger

logger = get_logger(__name__)


class ScraperProcessor:
    """Orchestrates the scraping pipeline."""

    def __init__(self, settings: Settings):
        """
        Initialize processor.

        Args:
            settings: Application settings
        """
        self.settings = settings
        self.crawler = AccountingWebsiteCrawler(settings)
        self.llm_extractor = LLMExtractor(settings)

    async def process_url(self, url: str) -> Tuple[Optional[CompanyData], Optional[OutOfScopeRecord], Optional[LowConfidenceRecord]]:
        """
        Process a single URL end-to-end.

        Args:
            url: URL to scrape

        Returns:
            Tuple of (CompanyData, OutOfScopeRecord, LowConfidenceRecord)
            Only one of these will be non-None
        """
        logger.info(f"Processing: {url}")

        # Validate URL format
        if not self._is_valid_url(url):
            logger.warning(f"Invalid URL format: {url}")
            return None, None, LowConfidenceRecord(
                company_url=url,
                company_name=None,
                confidence_score=0.0,
                reason="Invalid URL format"
            )

        # Step 1: Crawl the main page only
        crawl_result = await self.crawler.crawl_main_page(url)

        if not crawl_result:
            logger.warning(f"Crawl failed for {url}")
            return None, None, LowConfidenceRecord(
                company_url=url,
                company_name=None,
                confidence_score=0.0,
                reason="Crawl failed - site not accessible or timeout"
            )

        # Step 2: Extract with LLM (main page only - bio pages are optional)
        llm_result = await self.llm_extractor.extract(
            company_url=url,
            main_content=crawl_result.get("markdown", ""),
            bio_content=[]  # Not crawling bio pages - main page sufficient
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
            return None, OutOfScopeRecord(
                company_url=url,
                company_name=llm_result.company_name,
                reason=llm_result.out_of_scope_reason or "Out of scope",
                confidence_score=llm_result.confidence_score
            ), None

        # Check if low confidence
        if llm_result.confidence_score < self.settings.min_confidence_threshold:
            logger.warning(f"Low confidence for {url}: {llm_result.confidence_score}")
            reason = f"Confidence score {llm_result.confidence_score} below threshold"
            if not llm_result.company_name:
                reason += "; missing company name"
            return None, None, LowConfidenceRecord(
                company_url=url,
                company_name=llm_result.company_name,
                confidence_score=llm_result.confidence_score,
                reason=reason
            )

        # Convert to final CompanyData
        try:
            company_data = self._convert_to_company_data(url, llm_result)
            logger.info(f"Successfully processed: {url} (confidence: {llm_result.confidence_score})")
            return company_data, None, None
        except Exception as e:
            logger.error(f"Failed to convert to CompanyData for {url}: {e}")
            return None, None, LowConfidenceRecord(
                company_url=url,
                company_name=llm_result.company_name,
                confidence_score=llm_result.confidence_score,
                reason=f"Validation error: {str(e)}"
            )

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

    async def process_batch(self, urls: List[str]) -> Tuple[List[CompanyData], List[OutOfScopeRecord], List[LowConfidenceRecord], List[str]]:
        """
        Process multiple URLs concurrently.

        Args:
            urls: List of URLs to process

        Returns:
            Tuple of (successful, out_of_scope, low_confidence, broken_urls)
        """
        logger.info(f"Processing batch of {len(urls)} URLs")

        # Process with controlled concurrency
        semaphore = asyncio.Semaphore(self.settings.max_concurrent_crawls)

        async def process_with_semaphore(url):
            async with semaphore:
                try:
                    # Add delay between requests to avoid rate limiting
                    await asyncio.sleep(self.settings.delay_between_requests)
                    return await self.process_url(url)
                except Exception as e:
                    logger.error(f"Error processing {url}: {e}")
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

        logger.info(
            f"Batch complete: {len(successful)} successful, "
            f"{len(out_of_scope)} out of scope, "
            f"{len(low_confidence)} low confidence, "
            f"{len(broken)} broken"
        )

        return successful, out_of_scope, low_confidence, broken
