"""
CLI entrypoint: orchestrate harvest -> enrich -> dedup -> export.

Usage:
    python main.py                        # Run all phases
    python main.py --phase 1              # Directory harvest only
    python main.py --phase 2              # Website enrichment only (requires Phase 1)
    python main.py --phase 3              # Dedup + export only (requires Phase 1)
    python main.py --states VIC NSW       # Limit to specific states
    python main.py --skip-enrichment      # Skip Phase 2 (no crawl4ai/LLM)
    python main.py --skip-dedup           # Skip Attio dedup (export all as 'new')
"""
import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

load_dotenv()

from config import Settings
from models import DirectoryListing, EnrichmentData
from checkpoint import Checkpoint
from directory_scraper import scrape_directory
from website_enricher import enrich_firms
from attio_dedup import export_attio_lookups, classify_records, extract_domain
from exporter import build_company_records, build_people_records, export_csvs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scraper.log"),
    ],
)
logger = logging.getLogger(__name__)


async def run_phase1(
    settings: Settings,
    checkpoint: Checkpoint,
    states: Optional[List[str]] = None,
) -> List[DirectoryListing]:
    """Phase 1: Scrape the directory."""
    logger.info("=" * 60)
    logger.info("PHASE 1: Directory Harvest")
    logger.info("=" * 60)

    listings = await scrape_directory(
        checkpoint=checkpoint,
        delay=settings.directory_delay,
        max_concurrent=settings.directory_max_concurrent,
        states=states,
    )

    logger.info(f"Phase 1 complete: {len(listings)} firms scraped")
    with_urls = sum(1 for l in listings if l.website_url)
    with_contacts = sum(1 for l in listings if l.contact_name)
    with_email = sum(1 for l in listings if l.email)
    logger.info(f"  {with_urls} have website URLs")
    logger.info(f"  {with_contacts} have contact names")
    logger.info(f"  {with_email} have email addresses")

    return listings


async def run_phase2(
    settings: Settings,
    checkpoint: Checkpoint,
    listings: List[DirectoryListing],
) -> dict:
    """Phase 2: Enrich firms with website URLs."""
    logger.info("=" * 60)
    logger.info("PHASE 2: Website Enrichment")
    logger.info("=" * 60)

    if not settings.openrouter_api_key:
        logger.warning("OPENROUTER_API_KEY not set, skipping enrichment")
        return {}

    to_enrich = [
        {"website_url": l.website_url, "name": l.name}
        for l in listings
        if l.website_url
    ]

    if not to_enrich:
        logger.info("No firms with website URLs to enrich")
        return {}

    logger.info(f"Enriching {len(to_enrich)} firms...")
    enrichments = await enrich_firms(
        settings=settings,
        listings_with_urls=to_enrich,
        checkpoint=checkpoint,
        delay=settings.directory_delay,
    )

    logger.info(f"Phase 2 complete: {len(enrichments)} firms enriched")
    return {url: e for url, e in enrichments.items()}


async def run_phase3(
    settings: Settings,
    listings: List[DirectoryListing],
    enrichments: dict,
    skip_dedup: bool = False,
) -> dict:
    """Phase 3: Build records, dedup, export CSVs."""
    logger.info("=" * 60)
    logger.info("PHASE 3: Dedup & Export")
    logger.info("=" * 60)

    # Build company records
    records = build_company_records(listings, enrichments)
    logger.info(f"Built {len(records)} company records")

    # Dedup against Attio
    if not skip_dedup and settings.attio_api_key:
        logger.info("Exporting Attio lookups for dedup...")
        domain_lookup, phone_lookup = await export_attio_lookups(settings.attio_api_key)
        records = classify_records(records, domain_lookup, phone_lookup)
    else:
        if skip_dedup:
            logger.info("Dedup skipped (--skip-dedup flag)")
        else:
            logger.warning("ATTIO_API_KEY not set, skipping dedup (all marked as 'new')")

    # Build people records
    people = build_people_records(listings, enrichments)
    logger.info(f"Built {len(people)} people records")

    # Export CSVs
    paths = export_csvs(records, people, settings.output_dir)

    logger.info("=" * 60)
    logger.info("EXPORT COMPLETE")
    for label, path in paths.items():
        if path:
            logger.info(f"  {label}: {path}")
    logger.info("=" * 60)

    return paths


async def main():
    parser = argparse.ArgumentParser(
        description="Scrape accountantlist.com.au and export Attio-ready CSVs"
    )
    parser.add_argument(
        "--phase", type=int, choices=[1, 2, 3],
        help="Run only a specific phase (default: all)"
    )
    parser.add_argument(
        "--states", nargs="+",
        help="Limit to specific states (e.g., --states VIC NSW)"
    )
    parser.add_argument(
        "--skip-enrichment", action="store_true",
        help="Skip Phase 2 (website enrichment)"
    )
    parser.add_argument(
        "--skip-dedup", action="store_true",
        help="Skip Attio dedup (export all as 'new')"
    )
    parser.add_argument(
        "--checkpoint", default="data/state/checkpoint.json",
        help="Path to checkpoint file"
    )
    parser.add_argument(
        "--force-recrawl", choices=("all", "no-dm"),
        default=None,
        help="Invalidate cached enrichment data. "
             "'no-dm': re-crawl domains with no decision makers. "
             "'all': wipe and re-crawl everything."
    )

    args = parser.parse_args()

    settings = Settings()
    checkpoint = Checkpoint(args.checkpoint)

    if args.phase == 1 or args.phase is None:
        listings = await run_phase1(settings, checkpoint, args.states)
    else:
        # Load from checkpoint
        raw = checkpoint.get_directory_listings()
        if not raw:
            logger.error("No Phase 1 data in checkpoint. Run Phase 1 first.")
            sys.exit(1)
        listings = [DirectoryListing(**l) for l in raw]
        logger.info(f"Loaded {len(listings)} listings from checkpoint")

    if args.force_recrawl:
        if args.force_recrawl == "all":
            count = checkpoint.invalidate_all_enrichments()
            logger.info(f"Force recrawl (all): invalidated {count} cached enrichments")
        elif args.force_recrawl == "no-dm":
            count = checkpoint.invalidate_no_dm_urls()
            logger.info(f"Force recrawl (no-dm): invalidated {count} domains with no decision makers")

    enrichments = {}
    if (args.phase == 2 or args.phase is None) and not args.skip_enrichment:
        enrichments = await run_phase2(settings, checkpoint, listings)
    elif args.phase == 3 or (args.phase is None and args.skip_enrichment):
        # Load cached enrichments
        raw_enrichments = checkpoint.get_all_enrichments()
        for url, data in raw_enrichments.items():
            try:
                enrichments[url] = EnrichmentData(**data)
            except Exception:
                pass
        logger.info(f"Loaded {len(enrichments)} enrichments from checkpoint")

    if args.phase == 3 or args.phase is None:
        await run_phase3(settings, listings, enrichments, args.skip_dedup)


if __name__ == "__main__":
    asyncio.run(main())
