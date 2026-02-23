#!/usr/bin/env python3
"""Quick debug script to see what LLM returns."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import Settings
from src.adaptive_crawler import AdaptiveWebsiteCrawler
import json
import logging

# Set up debug logging
logging.basicConfig(level=logging.DEBUG, format='%(name)s - %(levelname)s - %(message)s')

async def debug_llm():
    settings = Settings()
    crawler = AdaptiveWebsiteCrawler(settings)

    url = "https://www.jpo.com.au"

    print(f"Crawling {url}...")
    crawl_result = await crawler.crawl_intelligently(url, max_pages=1, crawl_strategy="main_only")

    if not crawl_result:
        print("Crawl failed!")
        return

    print(f"\nCrawled {crawl_result['pages_crawled']} pages")
    print(f"Main content length: {len(crawl_result.get('main_page', ''))} chars")

    # Show first 2000 chars of content
    content = crawl_result.get('main_page', '')
    print(f"\nFirst 2000 chars of markdown:")
    print("="*80)
    print(content[:2000])
    print("="*80)

if __name__ == "__main__":
    import asyncio
    asyncio.run(debug_llm())
