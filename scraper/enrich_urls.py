"""
Standalone URL enrichment: crawl a list of firm websites and extract
structured company + people data into Attio-ready CSVs.

Accepts a CSV or TXT file of URLs. CSV can optionally include a 'name'
column; otherwise the domain is used as the firm name.

Input formats:
  - TXT: one URL per line
  - CSV: must have a 'url' or 'website' or 'domain' column.
         Optional 'name' / 'firm' / 'company' column.

Outputs:
  - companies.csv   — company records with enrichment data
  - people.csv      — decision maker / contact records

Usage:
    python enrich_urls.py --input urls.txt
    python enrich_urls.py --input firms.csv --output data/output/my_run
    python enrich_urls.py --input urls.txt --concurrency 4 --skip-dedup
    python enrich_urls.py --input urls.txt --limit 10 --dry-run
"""
import argparse
import asyncio
import csv
import logging
import sys
import time
from pathlib import Path
from typing import Optional, List, Dict

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

from config import Settings
from checkpoint import Checkpoint
from website_enricher import WebsiteEnricher
from models import EnrichmentData, DecisionMaker, PersonRecord
from phone_utils import normalize_to_e164
from attio_dedup import export_attio_lookups, extract_domain

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("enrich_urls.log"),
    ],
)
logger = logging.getLogger(__name__)

URL_COLUMNS = ["url", "website", "domain", "website_url", "domains"]
NAME_COLUMNS = ["name", "firm", "company", "company_name", "firm_name"]


def _load_input(path: str) -> List[Dict[str, str]]:
    """Load a TXT or CSV file into a list of {url, name} dicts."""
    p = Path(path)
    entries = []

    if p.suffix.lower() == ".txt":
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                entries.append({"url": line, "name": ""})
    elif p.suffix.lower() == ".csv":
        df = pd.read_csv(p)
        cols_lower = {c.lower().strip(): c for c in df.columns}

        url_col = None
        for candidate in URL_COLUMNS:
            if candidate in cols_lower:
                url_col = cols_lower[candidate]
                break
        if not url_col:
            logger.error(
                f"CSV must have one of these columns: {URL_COLUMNS}. "
                f"Found: {list(df.columns)}"
            )
            sys.exit(1)

        name_col = None
        for candidate in NAME_COLUMNS:
            if candidate in cols_lower:
                name_col = cols_lower[candidate]
                break

        for _, row in df.iterrows():
            url = str(row[url_col]).strip()
            name = str(row[name_col]).strip() if name_col and pd.notna(row.get(name_col)) else ""
            if url and url.lower() != "nan":
                entries.append({"url": url, "name": name})
    else:
        logger.error(f"Unsupported file format: {p.suffix}. Use .txt or .csv")
        sys.exit(1)

    for e in entries:
        raw = e["url"]
        if not raw.startswith("http") and "." in raw:
            raw = raw.rstrip("/")
        e["url"] = raw

        if not e["name"]:
            d = extract_domain(raw)
            e["name"] = d.split(".")[0].replace("-", " ").title() if d else raw

    return entries


def _split_name(full_name: str) -> tuple:
    if not full_name:
        return ("", "")
    name = full_name.strip()
    if "," in name:
        parts = name.split(",", 1)
        return (parts[1].strip(), parts[0].strip())
    parts = name.split()
    if len(parts) == 1:
        return (parts[0], "")
    return (parts[0], " ".join(parts[1:]))


def _build_company_row(url: str, name: str, enrichment: Optional[EnrichmentData]) -> dict:
    domain = extract_domain(url)
    row = {
        "domains": domain or "",
        "name": name,
        "description": "",
        "edited_description": "",
        "office_phone": "",
        "office_email": "",
        "associated_mobiles": "",
        "associated_emails": "",
        "associated_info": "",
        "organisational_structure": "",
        "linkedin": "",
        "facebook": "",
        "dm_1_name": "",
        "enrichment_status": "no_data",
    }
    if not enrichment:
        return row

    if enrichment.out_of_scope:
        row["enrichment_status"] = f"out_of_scope: {enrichment.out_of_scope_reason or ''}"
        return row

    row["description"] = enrichment.description
    row["edited_description"] = enrichment.edited_description
    row["office_phone"] = enrichment.office_phone or ""
    row["office_email"] = enrichment.office_email or ""
    row["associated_mobiles"] = "; ".join(enrichment.associated_mobiles) if enrichment.associated_mobiles else ""
    row["associated_emails"] = "; ".join(enrichment.associated_emails) if enrichment.associated_emails else ""
    row["associated_info"] = enrichment.associated_info
    row["organisational_structure"] = enrichment.organisational_structure or ""
    row["linkedin"] = enrichment.linkedin or ""
    row["facebook"] = enrichment.facebook or ""

    if enrichment.decision_makers:
        row["dm_1_name"] = enrichment.decision_makers[0].name or ""
        row["enrichment_status"] = "enriched_with_dms"
    else:
        row["enrichment_status"] = "enriched_no_dms"

    return row


def _build_people_rows(
    url: str, name: str, enrichment: Optional[EnrichmentData],
) -> List[dict]:
    if not enrichment or enrichment.out_of_scope:
        return []

    domain = extract_domain(url)
    rows = []

    for dm in enrichment.decision_makers:
        if not dm.name:
            continue
        first, last = _split_name(dm.name)
        phones = [p for p in [dm.phone_office, dm.phone_mobile, dm.phone_direct] if p]
        rows.append({
            "first_name": first,
            "last_name": last,
            "email_addresses": dm.email or "",
            "job_title": dm.title or "",
            "phone_numbers": "; ".join(phones),
            "linkedin": dm.linkedin or "",
            "company_name": name,
            "company_domain": domain or "",
        })

    if not rows:
        orphan_phones = list(enrichment.associated_mobiles or [])
        if enrichment.office_phone:
            orphan_phones.insert(0, enrichment.office_phone)
        orphan_email = enrichment.office_email
        if not orphan_email and enrichment.associated_emails:
            orphan_email = enrichment.associated_emails[0]

        if orphan_phones or orphan_email:
            rows.append({
                "first_name": "Contact at",
                "last_name": name,
                "email_addresses": orphan_email or "",
                "job_title": "Office Contact",
                "phone_numbers": "; ".join(orphan_phones),
                "linkedin": "",
                "company_name": name,
                "company_domain": domain or "",
            })

    return rows


async def run(
    input_path: str,
    output_dir: str,
    checkpoint_path: str,
    concurrency: int = 4,
    delay: float = 1.0,
    limit: Optional[int] = None,
    dry_run: bool = False,
    skip_dedup: bool = False,
):
    settings = Settings()
    entries = _load_input(input_path)
    logger.info(f"Loaded {len(entries)} URLs from {input_path}")

    if limit:
        entries = entries[:limit]
        logger.info(f"Limited to {limit} URLs")

    if dry_run:
        logger.info("DRY RUN — would enrich:")
        for e in entries[:20]:
            logger.info(f"  {e['name']} ({e['url']})")
        if len(entries) > 20:
            logger.info(f"  ... and {len(entries) - 20} more")
        return

    checkpoint = Checkpoint(checkpoint_path)
    enrichments: Dict[str, EnrichmentData] = {}

    for url, data in checkpoint.get_all_enrichments().items():
        try:
            enrichments[url] = EnrichmentData(**data)
        except Exception:
            pass

    already_done = checkpoint.get_enriched_urls()
    remaining = [e for e in entries if e["url"] not in already_done]
    logger.info(f"Enriching {len(remaining)} URLs ({len(already_done)} cached) with concurrency={concurrency}")

    if remaining:
        enricher = WebsiteEnricher(settings)
        await enricher.start_pool(size=concurrency)
        semaphore = asyncio.Semaphore(concurrency)
        counter = {"done": 0, "start_time": time.monotonic()}
        total = len(remaining)

        async def _enrich_one(entry: dict) -> None:
            async with semaphore:
                url, name = entry["url"], entry["name"]
                counter["done"] += 1
                idx = counter["done"]
                elapsed = time.monotonic() - counter["start_time"]
                rate = idx / elapsed * 60 if elapsed > 0 else 0
                eta = (total - idx) / rate if rate > 0 else 0
                logger.info(f"[{idx}/{total}] {name} ({url}) [{rate:.1f}/min, ETA {eta:.0f}min]")

                try:
                    enrichment = await enricher.enrich(url, name)
                    if enrichment:
                        enrichments[url] = enrichment
                        checkpoint.save_enrichment(url, enrichment.model_dump())
                        logger.info(f"  -> {len(enrichment.decision_makers)} DM(s)")
                    else:
                        checkpoint.mark_enriched(url)
                        logger.info("  -> No data")
                except Exception as e:
                    logger.error(f"  -> Failed: {e}")
                    checkpoint.mark_enriched(url)

                await asyncio.sleep(delay)

        try:
            batch_size = concurrency * 3
            for start in range(0, total, batch_size):
                batch = remaining[start:start + batch_size]
                await asyncio.gather(*[_enrich_one(e) for e in batch])
        finally:
            await enricher.stop_pool()

        elapsed_total = time.monotonic() - counter["start_time"]
        logger.info(f"Done: {counter['done']} URLs in {elapsed_total/60:.1f}min ({counter['done']/elapsed_total*60:.1f}/min)")

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    company_rows = []
    people_rows = []
    for entry in entries:
        url, name = entry["url"], entry["name"]
        enrichment = enrichments.get(url)
        company_rows.append(_build_company_row(url, name, enrichment))
        people_rows.extend(_build_people_rows(url, name, enrichment))

    df_companies = pd.DataFrame(company_rows).fillna("")
    companies_path = out / "companies.csv"
    df_companies.to_csv(companies_path, index=False, quoting=1)
    logger.info(f"Wrote {len(df_companies)} companies to {companies_path}")

    if people_rows:
        df_people = pd.DataFrame(people_rows).fillna("")
        people_path = out / "people.csv"
        df_people.to_csv(people_path, index=False, quoting=1)
        logger.info(f"Wrote {len(df_people)} people to {people_path}")

    enriched = df_companies[df_companies["enrichment_status"].str.startswith("enriched")].shape[0]
    with_dms = df_companies[df_companies["enrichment_status"] == "enriched_with_dms"].shape[0]
    logger.info(f"Summary: {enriched}/{len(entries)} enriched, {with_dms} with DMs, {len(people_rows)} people records")


def main():
    parser = argparse.ArgumentParser(
        description="Enrich a list of firm URLs — crawl websites, extract company + people data, export CSVs"
    )
    parser.add_argument("--input", required=True, help="Path to input file (.txt or .csv)")
    parser.add_argument("--output", default="data/output/url_enrichment", help="Output directory")
    parser.add_argument("--checkpoint", default="data/state/url_enrichment_checkpoint.json", help="Checkpoint file")
    parser.add_argument("--concurrency", type=int, default=4, help="Concurrent crawls (default: 4)")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between crawls in seconds")
    parser.add_argument("--limit", type=int, default=None, help="Limit to N URLs (for testing)")
    parser.add_argument("--dry-run", action="store_true", help="Preview URLs without crawling")
    parser.add_argument("--skip-dedup", action="store_true", help="Skip Attio dedup")
    args = parser.parse_args()

    asyncio.run(run(
        input_path=args.input,
        output_dir=args.output,
        checkpoint_path=args.checkpoint,
        concurrency=args.concurrency,
        delay=args.delay,
        limit=args.limit,
        dry_run=args.dry_run,
        skip_dedup=args.skip_dedup,
    ))


if __name__ == "__main__":
    main()
