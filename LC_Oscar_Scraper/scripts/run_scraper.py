"""
Main script to run the accounting website scraper.
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
    Load URLs from text file (one per line).

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
    """Main execution function."""
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler('scraper.log'),
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

    # Load URLs
    urls_file = "data/input/urls.txt"
    if not Path(urls_file).exists():
        logger.error(f"URLs file not found: {urls_file}")
        logger.error("Please create data/input/urls.txt with one URL per line")
        sys.exit(1)

    urls = load_urls(urls_file)
    logger.info(f"Loaded {len(urls)} URLs from {urls_file}")

    if not urls:
        logger.error("No URLs to process")
        sys.exit(1)

    # Initialize processor
    processor = ScraperProcessor(settings)

    # Process all URLs
    logger.info("Starting scraping...")
    successful, out_of_scope, low_confidence, broken = await processor.process_batch(urls)

    # Export results
    exporter = CSVExporter(settings.output_dir)

    logger.info("\n" + "=" * 60)
    logger.info("EXPORTING RESULTS")
    logger.info("=" * 60)

    # Successful extractions
    results_file = exporter.export_results(successful)
    if results_file:
        logger.info(f"✅ Results: {results_file}")

    # Out of scope
    oos_file = exporter.export_out_of_scope(out_of_scope)
    if oos_file:
        logger.info(f"📋 Out of Scope: {oos_file}")

    # Low confidence
    lc_file = exporter.export_low_confidence(low_confidence)
    if lc_file:
        logger.info(f"⚠️  Low Confidence: {lc_file}")

    # Broken URLs
    broken_file = exporter.export_broken_urls(broken)
    if broken_file:
        logger.info(f"❌ Broken URLs: {broken_file}")

    # Summary
    logger.info("\n" + "=" * 60)
    logger.info("SUMMARY")
    logger.info("=" * 60)
    logger.info(f"Total URLs processed: {len(urls)}")
    logger.info(f"Successful extractions: {len(successful)}")
    logger.info(f"Out of scope: {len(out_of_scope)}")
    logger.info(f"Low confidence: {len(low_confidence)}")
    logger.info(f"Broken/failed: {len(broken)}")

    if successful:
        avg_confidence = sum(r.confidence_score for r in successful) / len(successful)
        logger.info(f"Average confidence score: {avg_confidence:.2f}")
        total_dm = sum(len(r.decision_makers) for r in successful)
        logger.info(f"Total decision makers found: {total_dm}")

    logger.info("\n" + "=" * 60)
    logger.info("SCRAPING COMPLETE")
    logger.info("=" * 60)

    # Exit with error if no successful results
    if not successful:
        logger.warning("No successful extractions. Please check the logs and error files.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
