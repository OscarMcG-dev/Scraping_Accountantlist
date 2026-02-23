"""
Main script to run the accounting website scraper with checkpoint support.
"""
import asyncio
import argparse
import sys
from pathlib import Path
from datetime import datetime

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import Settings
from src.schemas import CompanyData, OutOfScopeRecord, LowConfidenceRecord
from src.adaptive_processor import AdaptiveScraperProcessor
from src.export import CSVExporter
from src.checkpoint_manager import CheckpointManager
from src.logger import get_logger

logger = get_logger(__name__)


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
        logger.error(f"URLs file not found: {filepath}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Error reading URLs file: {e}")
        sys.exit(1)

    return urls


def display_resume_info(checkpoint_manager: CheckpointManager):
    """
    Display information about a checkpoint that will be resumed.

    Args:
        checkpoint_manager: Loaded checkpoint manager
    """
    progress = checkpoint_manager.get_progress()
    results = checkpoint_manager.get_accumulated_results()

    logger.info("\n" + "=" * 60)
    logger.info("RESUMING FROM CHECKPOINT")
    logger.info("=" * 60)
    logger.info(f"Session: {checkpoint_manager.current_checkpoint.get('session_id')}")
    logger.info(f"Start Time: {progress['start_time']}")
    logger.info(f"Last Update: {progress['last_update']}")
    logger.info(f"\nProgress: {progress['processed']}/{progress['total_urls']} URLs ({progress['progress_percent']:.1f}%)")
    logger.info(f"Remaining: {progress['remaining']} URLs")
    logger.info(f"\nResults so far:")
    logger.info(f"  Successful: {progress['successful']}")
    logger.info(f"  Out of Scope: {progress['out_of_scope']}")
    logger.info(f"  Low Confidence: {progress['low_confidence']}")
    logger.info(f"  Broken: {progress['broken']}")
    logger.info("=" * 60)


async def main():
    """Main execution function."""
    parser = argparse.ArgumentParser(
        description="LC Official Scraper - Adaptive Web Crawler with Checkpoint Support"
    )
    parser.add_argument(
        '--batch-file',
        type=str,
        default='data/input/urls.txt',
        help='Path to file containing URLs (one per line)'
    )
    parser.add_argument(
        '--strategy',
        type=str,
        choices=['adaptive', 'greedy', 'main_only'],
        default='adaptive',
        help='Crawling strategy (default: adaptive)'
    )
    parser.add_argument(
        '--max-pages',
        type=int,
        default=5,
        help='Maximum number of pages to crawl per website (default: 5)'
    )
    parser.add_argument(
        '--checkpoint-name',
        type=str,
        default='batch',
        help='Name for this checkpoint batch (default: batch)'
    )
    parser.add_argument(
        '--resume',
        action='store_true',
        help='Resume from the latest checkpoint'
    )
    parser.add_argument(
        '--resume-from',
        type=str,
        default=None,
        help='Resume from a specific checkpoint file'
    )
    parser.add_argument(
        '--skip-progress',
        action='store_true',
        help='Disable real-time progress tracking'
    )

    args = parser.parse_args()

    # Load settings
    try:
        settings = Settings()
    except Exception as e:
        logger.error(f"Failed to load settings: {e}")
        logger.error("Make sure .env file exists with OPENROUTER_API_KEY set")
        sys.exit(1)

    # Handle resume
    checkpoint_manager = None
    urls_to_process = []

    if args.resume or args.resume_from:
        # Load checkpoint
        if args.resume_from:
            checkpoint_manager = CheckpointManager.load_checkpoint(args.resume_from)
        else:
            # Load latest checkpoint
            latest = CheckpointManager.get_latest_checkpoint()
            if not latest:
                logger.error("No checkpoints found to resume from")
                sys.exit(1)
            checkpoint_manager = CheckpointManager.load_checkpoint(latest)

        if not checkpoint_manager:
            logger.error("Failed to load checkpoint")
            sys.exit(1)

        display_resume_info(checkpoint_manager)

        # Get remaining URLs
        urls_to_process = checkpoint_manager.get_remaining_urls()

        if not urls_to_process:
            logger.info("All URLs have been processed. No work remaining.")
            # Export accumulated results
            exporter = CSVExporter(settings.output_dir)
            results = checkpoint_manager.get_accumulated_results()

            logger.info("\nExporting accumulated results...")
            results_file = exporter.export_results([
                CompanyData(**r) for r in results['successful']
            ])
            if results_file:
                logger.info(f"✅ Results: {results_file}")

            sys.exit(0)

        logger.info(f"Resuming with {len(urls_to_process)} remaining URLs")
    else:
        # New session
        if not Path(args.batch_file).exists():
            logger.error(f"URLs file not found: {args.batch_file}")
            logger.error("Please create the file with one URL per line")
            sys.exit(1)

        urls_to_process = load_urls(args.batch_file)
        logger.info(f"Loaded {len(urls_to_process)} URLs from {args.batch_file}")

        if not urls_to_process:
            logger.error("No URLs to process")
            sys.exit(1)

        # Initialize checkpoint manager
        checkpoint_manager = CheckpointManager()
        checkpoint_manager.initialize_session(urls_to_process, args.checkpoint_name)
        logger.info(f"Checkpoint file: {checkpoint_manager.checkpoint_file}")

    # Initialize processor with checkpoint manager
    processor = AdaptiveScraperProcessor(
        settings=settings,
        crawl_strategy=args.strategy,
        max_pages=args.max_pages,
        checkpoint_manager=checkpoint_manager
    )

    # Process URLs
    logger.info("\n" + "=" * 60)
    logger.info("STARTING CRAWL")
    logger.info("=" * 60)
    logger.info(f"Strategy: {args.strategy}")
    logger.info(f"Max Pages: {args.max_pages}")
    logger.info(f"Progress Tracking: {'Disabled' if args.skip_progress else 'Enabled'}")
    logger.info("=" * 60)

    successful, out_of_scope, low_confidence, broken = await processor.process_batch(
        urls_to_process,
        enable_progress_tracking=not args.skip_progress
    )

    # Mark checkpoint as completed
    checkpoint_manager.mark_completed()

    # Merge with accumulated results
    accumulated = checkpoint_manager.get_accumulated_results()
    all_successful = accumulated['successful'] + [cd.model_dump() for cd in successful]
    all_out_of_scope = accumulated['out_of_scope'] + [oos.model_dump() for oos in out_of_scope]
    all_low_confidence = accumulated['low_confidence'] + [lc.model_dump() for lc in low_confidence]
    all_broken = accumulated['broken'] + broken

    # Export results
    exporter = CSVExporter(settings.output_dir)

    logger.info("\n" + "=" * 60)
    logger.info("EXPORTING RESULTS")
    logger.info("=" * 60)

    # Successful extractions
    if all_successful:
        results_file = exporter.export_results([
            CompanyData(**r) for r in all_successful
        ])
        if results_file:
            logger.info(f"✅ Results: {results_file}")

    # Out of scope
    if all_out_of_scope:
        oos_file = exporter.export_out_of_scope([
            OutOfScopeRecord(**r) for r in all_out_of_scope
        ])
        if oos_file:
            logger.info(f"📋 Out of Scope: {oos_file}")

    # Low confidence
    if all_low_confidence:
        lc_file = exporter.export_low_confidence([
            LowConfidenceRecord(**r) for r in all_low_confidence
        ])
        if lc_file:
            logger.info(f"⚠️  Low Confidence: {lc_file}")

    # Broken URLs
    if all_broken:
        broken_file = exporter.export_broken_urls(all_broken)
        if broken_file:
            logger.info(f"❌ Broken URLs: {broken_file}")

    # Summary
    logger.info("\n" + "=" * 60)
    logger.info("FINAL SUMMARY")
    logger.info("=" * 60)
    logger.info(f"Total URLs processed: {checkpoint_manager.current_checkpoint.get('total_urls', 0)}")
    logger.info(f"Successful extractions: {len(all_successful)}")
    logger.info(f"Out of scope: {len(all_out_of_scope)}")
    logger.info(f"Low confidence: {len(all_low_confidence)}")
    logger.info(f"Broken/failed: {len(all_broken)}")
    logger.info(f"Checkpoint: {checkpoint_manager.checkpoint_file}")

    if all_successful:
        avg_confidence = sum(r.get('confidence_score', 0) for r in all_successful) / len(all_successful)
        logger.info(f"Average confidence score: {avg_confidence:.2f}")
        total_dm = sum(len(r.get('decision_makers', [])) for r in all_successful)
        logger.info(f"Total decision makers found: {total_dm}")

    logger.info("=" * 60)
    logger.info("SCRAPING COMPLETE")
    logger.info("=" * 60)

    # List available checkpoints
    logger.info("\nAvailable checkpoints:")
    checkpoints = CheckpointManager.list_checkpoints()
    for cp in checkpoints[:5]:  # Show last 5
        logger.info(f"  - {Path(cp).name}")

    # Exit with error if no successful results
    if not all_successful:
        logger.warning("No successful extractions. Please check the logs and error files.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
