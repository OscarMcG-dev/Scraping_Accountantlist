"""
One-off: cross-check domains from website_urls.txt against Attio (domain lookup only).
Reports how many are already in Attio vs not. No other changes.
"""
import asyncio
import logging
import sys
from pathlib import Path

# Run from scraper dir so imports work
sys.path.insert(0, str(Path(__file__).resolve().parent))

from attio_dedup import export_attio_lookups, extract_domain
from config import Settings

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)
# Reduce noise during Attio export
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("attio_dedup").setLevel(logging.WARNING)


def load_domains_from_file(path: Path) -> list[str]:
    """Load and normalize domains from a text file (one URL/domain per line)."""
    domains = []
    seen = set()
    for line in path.read_text().strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        domain = extract_domain(line)
        if domain and domain not in seen:
            seen.add(domain)
            domains.append(domain)
    return domains


async def main() -> None:
    settings = Settings()
    if not settings.attio_api_key:
        logger.error("ATTIO_API_KEY not set. Set it in .env to run domain dedup check.")
        sys.exit(1)

    url_file = Path(__file__).resolve().parent / "data" / "state" / "website_urls.txt"
    if not url_file.exists():
        logger.error("File not found: %s", url_file)
        sys.exit(1)

    domains = load_domains_from_file(url_file)
    logger.info("Loaded %d unique domains from %s", len(domains), url_file.name)

    logger.info("Exporting Attio company records (domain lookup only)...")
    domain_lookup, _ = await export_attio_lookups(settings.attio_api_key)

    in_attio = 0
    not_in_attio = 0
    for d in domains:
        if domain_lookup.get(d):
            in_attio += 1
        else:
            not_in_attio += 1

    logger.info("")
    logger.info("Domain cross-check (Attio domains only):")
    logger.info("  In Attio:    %d", in_attio)
    logger.info("  Not in Attio: %d", not_in_attio)
    logger.info("  Total:      %d", in_attio + not_in_attio)


if __name__ == "__main__":
    asyncio.run(main())
