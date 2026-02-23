#!/usr/bin/env python3
"""
Quick local test: enrich one URL to verify crawl + LLM (link triage, extraction).
Usage: from scraper dir, with .env set:
  python test_enrich_one.py [url] [firm_name]
Default URL: a small accounting firm site.
"""
import asyncio
import os
import sys

# Ensure we load .env from scraper dir
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.chdir(Path(__file__).resolve().parent)

from dotenv import load_dotenv
load_dotenv()

from config import Settings
from website_enricher import WebsiteEnricher


async def main():
    url = (sys.argv[1] if len(sys.argv) > 1 else "https://www.natax.com.au").strip()
    if not url.startswith("http"):
        url = "https://" + url
    name = sys.argv[2] if len(sys.argv) > 2 else "NA Tax Accountants"

    settings = Settings()
    if not settings.openrouter_api_key:
        print("OPENROUTER_API_KEY not set in .env — skipping LLM (crawl-only test)")
        print("Set it to run full enrichment.")

    enricher = WebsiteEnricher(settings)
    await enricher.start_pool(1)
    try:
        print(f"Enriching: {name} @ {url}")
        result = await enricher.enrich(url, name)
        if result:
            print("OK — got enrichment")
            print(f"  office_phone: {result.office_phone}")
            print(f"  office_email: {result.office_email}")
            print(f"  decision_makers: {len(result.decision_makers)}")
            if result.decision_makers:
                for dm in result.decision_makers[:3]:
                    print(f"    - {dm.name} ({dm.title})")
        else:
            print("No enrichment (crawl failed or no data)")
    finally:
        await enricher.stop_pool()


if __name__ == "__main__":
    asyncio.run(main())
