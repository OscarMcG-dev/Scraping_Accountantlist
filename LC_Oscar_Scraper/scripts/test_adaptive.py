#!/usr/bin/env python3
"""
Test script for the new adaptive crawling approach.

This demonstrates the LLM-guided intelligent page discovery that replaces
brittle hard-coded URL patterns with semantic understanding.
"""
import asyncio
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import Settings
from src.adaptive_processor import AdaptiveScraperProcessor
from src.export import CSVExporter


async def test_single_url(url: str, strategy: str = "adaptive", max_pages: int = 5):
    """
    Test a single URL with different strategies.

    Args:
        url: URL to test
        strategy: "adaptive", "greedy", or "main_only"
        max_pages: Maximum pages to crawl
    """
    print(f"\n{'='*80}")
    print(f"Testing URL: {url}")
    print(f"Strategy: {strategy}, Max Pages: {max_pages}")
    print(f"{'='*80}\n")

    # Load settings
    settings = Settings()

    # Create processor
    processor = AdaptiveScraperProcessor(
        settings=settings,
        crawl_strategy=strategy,
        max_pages=max_pages
    )

    # Process URL
    company_data, out_of_scope, low_confidence = await processor.process_url(url)

    # Display results
    if company_data:
        print(f"\n✅ SUCCESS - Company: {company_data.company_name}")
        print(f"   Confidence: {company_data.confidence_score:.2f}")
        print(f"   Segment: {company_data.business_segment}")
        print(f"   Office Phone: {company_data.office_phone or 'N/A'}")
        print(f"   Office Email: {company_data.office_email or 'N/A'}")
        print(f"   Location: {company_data.associated_location or 'N/A'}")
        print(f"   Decision Makers: {len(company_data.decision_makers)}")
        for i, dm in enumerate(company_data.decision_makers, 1):
            print(f"      {i}. {dm.name} - {dm.title}")
            print(f"         Phone: {dm.phone_mobile or dm.phone_office or 'N/A'}")
            print(f"         Email: {dm.email or 'N/A'}")

    elif out_of_scope:
        print(f"\n⚠️  OUT OF SCOPE - {out_of_scope.company_name or 'Unknown'}")
        print(f"   Reason: {out_of_scope.reason}")
        print(f"   Confidence: {out_of_scope.confidence_score:.2f}")

    elif low_confidence:
        print(f"\n❌ LOW CONFIDENCE - {low_confidence.company_name or 'Unknown'}")
        print(f"   Reason: {low_confidence.reason}")
        print(f"   Confidence: {low_confidence.confidence_score:.2f}")


async def test_batch(urls: list, strategy: str = "adaptive", max_pages: int = 3, filename_prefix: str = "test_adaptive_"):
    """
    Test a batch of URLs.

    Args:
        urls: List of URLs to test
        strategy: Crawling strategy
        max_pages: Maximum pages per website
    """
    print(f"\n{'='*80}")
    print(f"Batch Test: {len(urls)} URLs")
    print(f"Strategy: {strategy}, Max Pages: {max_pages}")
    print(f"{'='*80}\n")

    # Load settings
    settings = Settings()

    # Create processor
    processor = AdaptiveScraperProcessor(
        settings=settings,
        crawl_strategy=strategy,
        max_pages=max_pages
    )

    # Process batch
    successful, out_of_scope, low_confidence, broken = await processor.process_batch(urls)

    # Summary
    print(f"\n{'='*80}")
    print("BATCH RESULTS SUMMARY")
    print(f"{'='*80}")
    print(f"✅ Successful:      {len(successful)}")
    print(f"⚠️  Out of Scope:   {len(out_of_scope)}")
    print(f"❌ Low Confidence:  {len(low_confidence)}")
    print(f"🔗 Broken URLs:      {len(broken)}")

    # Show business segment breakdown
    if successful:
        segments = {}
        for company in successful:
            seg = company.business_segment
            segments[seg] = segments.get(seg, 0) + 1

        print(f"\nBusiness Segments:")
        for segment, count in sorted(segments.items()):
            print(f"  - {segment}: {count}")

    # Show first 3 successful results
    if successful:
        print(f"\n{'='*80}")
        print("SAMPLE SUCCESSFUL RESULTS (First 3)")
        print(f"{'='*80}")
        for i, company in enumerate(successful[:3], 1):
            print(f"\n{i}. {company.company_name}")
            print(f"   URL: {company.company_url}")
            print(f"   Segment: {company.business_segment}")
            print(f"   Confidence: {company.confidence_score:.2f}")
            print(f"   Decision Makers: {len(company.decision_makers)}")

    # Show out of scope
    if out_of_scope:
        print(f"\n{'='*80}")
        print("OUT OF SCOPE EXAMPLES")
        print(f"{'='*80}")
        for i, oos in enumerate(out_of_scope[:3], 1):
            print(f"\n{i}. {oos.company_name or 'Unknown'} - {oos.company_url}")
            print(f"   Reason: {oos.reason}")

    # Show low confidence reasons
    if low_confidence:
        print(f"\n{'='*80}")
        print("LOW CONFIDENCE EXAMPLES")
        print(f"{'='*80}")
        reasons = {}
        for lc in low_confidence:
            reason = lc.reason.split(';')[0].strip()  # Take first reason
            reasons[reason] = reasons.get(reason, 0) + 1

        for reason, count in sorted(reasons.items(), key=lambda x: x[1], reverse=True)[:5]:
            print(f"  - {reason}: {count}")

    # Export results
    if successful or out_of_scope or low_confidence:
        output_dir = settings.output_dir
        exporter = CSVExporter(output_dir=output_dir)

        exporter.export_results(successful, f"{filename_prefix}results.csv")
        exporter.export_out_of_scope(out_of_scope, f"{filename_prefix}out_of_scope.csv")
        exporter.export_low_confidence(low_confidence, f"{filename_prefix}low_confidence.csv")
        exporter.export_broken_urls(broken, f"{filename_prefix}broken_urls.txt")

        print(f"\n\n📊 Results exported to: {output_dir}/{filename_prefix}*.csv")

    return successful, out_of_scope, low_confidence, broken


async def compare_strategies(urls: list):
    """
    Compare different crawling strategies on the same URLs.

    Args:
        urls: List of URLs to test
    """
    strategies = ["main_only", "adaptive", "greedy"]

    results_summary = {}

    for strategy in strategies:
        print(f"\n\n{'#'*80}")
        print(f"# TESTING STRATEGY: {strategy.upper()}")
        print(f"{'#'*80}")

        successful, out_of_scope, low_confidence, broken = await test_batch(
            urls,
            strategy=strategy,
            max_pages=3,
            filename_prefix=f"compare_{strategy}_"
        )

        results_summary[strategy] = {
            "successful": len(successful),
            "out_of_scope": len(out_of_scope),
            "low_confidence": len(low_confidence),
            "broken": len(broken),
            "total": len(urls)
        }

    # Final comparison
    print(f"\n\n{'#'*80}")
    print(f"# STRATEGY COMPARISON SUMMARY")
    print(f"{'#'*80}")
    print(f"\n{'Strategy':<12} {'Successful':<12} {'Out of Scope':<15} {'Low Confidence':<15} {'Broken':<10} {'Total':<10}")
    print(f"{'-'*80}")

    for strategy in strategies:
        stats = results_summary[strategy]
        print(f"{strategy:<12} {stats['successful']:<12} {stats['out_of_scope']:<15} "
              f"{stats['low_confidence']:<15} {stats['broken']:<10} {stats['total']:<10}")


async def main():
    """Main test entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Test adaptive crawling")
    parser.add_argument("--url", help="Single URL to test")
    parser.add_argument("--strategy", default="adaptive",
                        choices=["adaptive", "greedy", "main_only"],
                        help="Crawling strategy")
    parser.add_argument("--max-pages", type=int, default=5,
                        help="Maximum pages to crawl per website")
    parser.add_argument("--compare", action="store_true",
                        help="Compare all strategies on sample URLs")
    parser.add_argument("--sample-count", type=int, default=5,
                        help="Number of sample URLs to use for batch/comparison")

    args = parser.parse_args()

    if args.url:
        # Test single URL
        await test_single_url(args.url, args.strategy, args.max_pages)

    elif args.compare:
        # Compare strategies
        from pathlib import Path

        sample_file = Path(__file__).parent.parent / "tests" / "sample_urls.txt"
        with open(sample_file) as f:
            all_urls = [line.strip() for line in f if line.strip()]

        sample_urls = all_urls[:args.sample_count]
        await compare_strategies(sample_urls)

    else:
        # Default: test a batch of URLs
        from pathlib import Path

        sample_file = Path(__file__).parent.parent / "tests" / "sample_urls.txt"
        with open(sample_file) as f:
            urls = [line.strip() for line in f if line.strip()]

        await test_batch(urls[:args.sample_count], args.strategy, args.max_pages)


if __name__ == "__main__":
    asyncio.run(main())
