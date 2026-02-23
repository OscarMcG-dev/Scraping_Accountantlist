"""
Test script for the accounting website scraper.
"""
import asyncio
import logging
import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import Settings
from src.processor import ScraperProcessor
from src.export import CSVExporter


def load_urls(filepath: str) -> list[str]:
    """
    Load URLs from text file.

    Args:
        filepath: Path to URLs file

    Returns:
        List of URLs
    """
    urls = []
    try:
        with open(filepath, 'r') as f:
            for line in f:
                url = line.strip()
                if url and not url.startswith('#'):
                    urls.append(url)
    except FileNotFoundError:
        print(f"Error: URLs file not found: {filepath}")
        sys.exit(1)
    except Exception as e:
        print(f"Error reading URLs file: {e}")
        sys.exit(1)

    return urls


async def main():
    """Main test function."""
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler('test_scraper.log'),
            logging.StreamHandler(sys.stdout)
        ]
    )
    logger = logging.getLogger(__name__)

    # Load settings
    try:
        settings = Settings()
    except Exception as e:
        logger.error(f"Failed to load settings: {e}")
        logger.error("Make sure .env file exists with OPENROUTER_API_KEY set")
        sys.exit(1)

    # Use sample URLs for testing
    sample_file = "tests/sample_urls.txt"
    if not Path(sample_file).exists():
        logger.error(f"Sample URLs file not found: {sample_file}")
        sys.exit(1)

    urls = load_urls(sample_file)
    logger.info(f"Testing with {len(urls)} sample URLs")

    if not urls:
        logger.error("No URLs to test")
        sys.exit(1)

    # Initialize processor
    processor = ScraperProcessor(settings)

    # Process URLs
    logger.info("=" * 60)
    logger.info("STARTING TEST BATCH")
    logger.info("=" * 60)

    successful, out_of_scope, low_confidence, broken = await processor.process_batch(urls)

    # Save test results
    exporter = CSVExporter(settings.output_dir)

    logger.info("\n" + "=" * 60)
    logger.info("EXPORTING TEST RESULTS")
    logger.info("=" * 60)

    results_file = exporter.export_results(successful, "test_results.csv")
    if results_file:
        logger.info(f"✅ Results: {results_file}")

    oos_file = exporter.export_out_of_scope(out_of_scope, "test_out_of_scope.csv")
    if oos_file:
        logger.info(f"📋 Out of Scope: {oos_file}")

    lc_file = exporter.export_low_confidence(low_confidence, "test_low_confidence.csv")
    if lc_file:
        logger.info(f"⚠️  Low Confidence: {lc_file}")

    broken_file = exporter.export_broken_urls(broken, "test_broken_urls.txt")
    if broken_file:
        logger.info(f"❌ Broken URLs: {broken_file}")

    # Print summary
    logger.info("\n" + "=" * 60)
    logger.info("TEST SUMMARY")
    logger.info("=" * 60)
    logger.info(f"Total URLs: {len(urls)}")
    logger.info(f"Successful: {len(successful)}")
    logger.info(f"Out of scope: {len(out_of_scope)}")
    logger.info(f"Low confidence: {len(low_confidence)}")
    logger.info(f"Broken: {len(broken)}")

    if successful:
        avg_confidence = sum(r.confidence_score for r in successful) / len(successful)
        logger.info(f"Average confidence: {avg_confidence:.2f}")
        total_dm = sum(len(r.decision_makers) for r in successful)
        logger.info(f"Total decision makers: {total_dm}")

        # Show breakdown by segment
        logger.info("\nBusiness Segment Breakdown:")
        from collections import Counter
        segments = [r.business_segment for r in successful]
        for segment, count in sorted(segments.items()):
            logger.info(f"  {segment}: {count}")

    # Show some examples
    if successful:
        logger.info("\nSample Results:")
        for i, company in enumerate(successful[:3], 1):
            logger.info(f"\n{i}. {company.company_name} ({company.company_url})")
            logger.info(f"   Segment: {company.business_segment}")
            logger.info(f"   Confidence: {company.confidence_score}")
            logger.info(f"   Decision Makers: {len(company.decision_makers)}")
            if company.decision_makers:
                for dm in company.decision_makers[:2]:
                    logger.info(f"      - {dm.name} ({dm.title})")
                    if dm.phone_mobile:
                        logger.info(f"        Mobile: {dm.phone_mobile}")

    logger.info("\n" + "=" * 60)
    logger.info("TEST COMPLETE")
    logger.info("=" * 60)

    if not successful:
        logger.warning("No successful extractions. Check the logs and error files.")


if __name__ == "__main__":
    asyncio.run(main())
