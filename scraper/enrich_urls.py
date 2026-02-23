"""
Standalone URL enrichment: crawl a list of firm websites and extract
structured company + people data into Attio-ready CSVs.

Accepts a CSV or TXT file of URLs. TXT = one URL per line (no headers).
CSV can optionally include a 'name' column; otherwise the domain is used.

Input formats:
  - TXT: one URL per line (blank lines and # comments ignored)
  - CSV: must have a 'url' or 'website' or 'domain' column.
         Optional 'name' / 'firm' / 'company' column.

Output formats (--output-format):
  - default: companies.csv + people.csv (company and people records)
  - justcall: single CSV matching enrich_justcall.py columns (Record ID, Company,
    Company > Domains, Phone numbers, Email addresses, first_name, last_name,
    Job title, Description, LinkedIn, enrichment_status) for Attio People import.

Usage:
    python enrich_urls.py --input urls.txt
    python enrich_urls.py --input urls.txt --output-format justcall
    python enrich_urls.py --input firms.csv --output data/output/my_run
    python enrich_urls.py --input urls.txt --concurrency 4
    python enrich_urls.py --input urls.txt --limit 10 --dry-run
"""
import argparse
import asyncio
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
from website_enricher import WebsiteEnricher, prefilter_domains
from models import EnrichmentData, DecisionMaker
from attio_dedup import extract_domain

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


def _fill_phones_from_dm(row: dict, dm: DecisionMaker) -> None:
    """Back-fill phone from DM's personal numbers when the record has none."""
    phones = []
    for p in (dm.phone_office, dm.phone_mobile, dm.phone_direct):
        if p and p not in phones:
            phones.append(p)
    if phones:
        row["Phone numbers"] = "; ".join(phones)


def _fill_phones_from_enrichment(row: dict, enrichment: EnrichmentData) -> None:
    """Back-fill phone(s) from enrichment when the record has none."""
    if row.get("Phone numbers"):
        return
    phones = []
    if enrichment.office_phone:
        phones.append(enrichment.office_phone)
    if enrichment.associated_mobiles:
        for p in enrichment.associated_mobiles:
            if p and p not in phones:
                phones.append(p)
    if phones:
        row["Phone numbers"] = "; ".join(phones)


def _build_people_rows(
    url: str, name: str, enrichment: Optional[EnrichmentData],
) -> List[dict]:
    """Build people rows (default format: first_name, last_name, job_title, etc.)."""
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
                "linkedin": enrichment.linkedin or "",
                "company_name": name,
                "company_domain": domain or "",
            })

    return rows


# Columns matching enrich_justcall.py output for Attio People import
JUSTCALL_COLUMNS = [
    "Record ID", "Company", "Company > Domains", "Phone numbers", "Email addresses",
    "first_name", "last_name", "Job title", "Description", "LinkedIn", "enrichment_status",
]


def _build_people_rows_justcall_format(
    url: str, name: str, enrichment: Optional[EnrichmentData], record_id_base: str = "",
) -> List[dict]:
    """Build people rows with same columns as enrich_justcall output (Attio People CSV)."""
    if not enrichment or enrichment.out_of_scope:
        return []

    domain = extract_domain(url) or ""
    rows = []
    seq = 0

    for dm in enrichment.decision_makers:
        if not dm.name:
            continue
        seq += 1
        first, last = _split_name(dm.name)
        row = {
            "Record ID": f"{record_id_base}-{seq}" if record_id_base else "",
            "Company": name,
            "Company > Domains": domain,
            "Phone numbers": "",
            "Email addresses": dm.email or "",
            "first_name": first,
            "last_name": last,
            "Job title": dm.title or "",
            "Description": enrichment.edited_description or "",
            "LinkedIn": dm.linkedin or "",
            "enrichment_status": "dm",
        }
        _fill_phones_from_dm(row, dm)
        if not row["Phone numbers"]:
            row["Phone numbers"] = "; ".join(
                p for p in [dm.phone_office, dm.phone_mobile, dm.phone_direct] if p
            )
        rows.append(row)

    if not rows:
        has_contact = bool(
            enrichment.office_phone or enrichment.associated_mobiles
            or enrichment.office_email or enrichment.associated_emails
        )
        if has_contact:
            orphan_email = enrichment.office_email
            if not orphan_email and enrichment.associated_emails:
                orphan_email = enrichment.associated_emails[0]
            row = {
                "Record ID": record_id_base or "",
                "Company": name,
                "Company > Domains": domain,
                "Phone numbers": "",
                "Email addresses": orphan_email or "",
                "first_name": "Contact at",
                "last_name": name,
                "Job title": "Office Contact",
                "Description": enrichment.edited_description or "",
                "LinkedIn": enrichment.linkedin or "",
                "enrichment_status": "no_dm_found_contact_only",
            }
            _fill_phones_from_enrichment(row, enrichment)
            rows.append(row)

    for row in rows:
        if not row.get("LinkedIn") and enrichment and enrichment.linkedin:
            row["LinkedIn"] = enrichment.linkedin

    return rows


async def run(
    input_path: str,
    output_dir: str,
    checkpoint_path: str,
    concurrency: int = 4,
    delay: float = 1.0,
    limit: Optional[int] = None,
    dry_run: bool = False,
    output_format: str = "default",
    justcall_output_path: Optional[str] = None,
    force_recrawl: Optional[str] = None,
):
    """
    output_format: "default" -> companies.csv + people.csv
                   "justcall" -> single CSV with enrich_justcall columns (Attio People)
    justcall_output_path: when set and output_format=justcall, write CSV to this path (e.g. for unique filenames).
    """
    settings = Settings()
    entries = _load_input(input_path)
    logger.info(f"Loaded {len(entries)} URLs from {input_path}")

    all_urls = [e["url"] for e in entries]
    valid_urls, skipped = prefilter_domains(all_urls)
    if skipped:
        valid_set = set(valid_urls)
        before = len(entries)
        entries = [e for e in entries if e["url"].strip().lower() in valid_set]
        logger.info(f"Pre-filter: {before} -> {len(entries)} URLs ({len(skipped)} skipped: DNS/invalid)")
        for dom, reason in skipped[:10]:
            logger.debug(f"  Skipped {dom}: {reason}")

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

    # Key checkpoint by domain so we only crawl each domain once
    def _domain(url: str) -> str:
        d = extract_domain(url)
        return (d or url).strip().lower()

    checkpoint = Checkpoint(checkpoint_path)

    if force_recrawl == "all":
        count = checkpoint.invalidate_all_enrichments()
        logger.info(f"Force recrawl (all): invalidated {count} cached enrichments")
    elif force_recrawl == "no-dm":
        count = checkpoint.invalidate_no_dm_urls()
        logger.info(f"Force recrawl (no-dm): invalidated {count} domains with no decision makers")

    enrichments: Dict[str, EnrichmentData] = {}

    for stored_key, data in checkpoint.get_all_enrichments().items():
        try:
            enrichments[stored_key] = EnrichmentData(**data)
        except Exception:
            pass

    already_done = checkpoint.get_enriched_urls()
    # Remaining = entries whose domain we haven't enriched yet
    domains_seen = set()
    remaining = []
    for e in entries:
        dom = _domain(e["url"])
        if dom in already_done:
            continue
        if dom in domains_seen:
            continue
        domains_seen.add(dom)
        remaining.append(e)

    logger.info(f"Enriching {len(remaining)} unique domains ({len(already_done)} cached) with concurrency={concurrency}")

    if remaining:
        enricher = WebsiteEnricher(settings)
        await enricher.start_pool(size=concurrency)
        semaphore = asyncio.Semaphore(concurrency)
        counter = {"done": 0, "start_time": time.monotonic()}
        total = len(remaining)

        async def _enrich_one(entry: dict) -> None:
            async with semaphore:
                url, name = entry["url"], entry["name"]
                domain = _domain(url)
                counter["done"] += 1
                idx = counter["done"]
                elapsed = time.monotonic() - counter["start_time"]
                rate = idx / elapsed * 60 if elapsed > 0 else 0
                eta = (total - idx) / rate if rate > 0 else 0
                logger.info(f"[{idx}/{total}] {name} ({domain}) [{rate:.1f}/min, ETA {eta:.0f}min]")

                try:
                    enrichment = await enricher.enrich(url, name)
                    if enrichment:
                        enrichments[domain] = enrichment
                        checkpoint.save_enrichment(domain, enrichment.model_dump())
                        logger.info(f"  -> {len(enrichment.decision_makers)} DM(s)")
                    else:
                        checkpoint.mark_enriched(domain)
                        logger.info("  -> No data")
                except Exception as e:
                    logger.error(f"  -> Failed: {e}")
                    checkpoint.mark_enriched(domain)

                await asyncio.sleep(delay)

        try:
            batch_size = concurrency * 3
            for start in range(0, total, batch_size):
                batch = remaining[start:start + batch_size]
                await asyncio.gather(*[_enrich_one(e) for e in batch])
        finally:
            await enricher.stop_pool()

        elapsed_total = time.monotonic() - counter["start_time"]
        logger.info(f"Done: {counter['done']} domains in {elapsed_total/60:.1f}min ({counter['done']/elapsed_total*60:.1f}/min)")

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    if output_format == "justcall":
        # Single CSV with enrich_justcall columns: one row per DM, or one "Contact at" per domain
        justcall_rows = []
        seen_domains = set()
        for i, entry in enumerate(entries):
            url, name = entry["url"], entry["name"]
            domain = _domain(url)
            if domain in seen_domains:
                continue
            seen_domains.add(domain)
            enrichment = enrichments.get(domain)
            record_id_base = f"domain-{i+1}"
            justcall_rows.extend(
                _build_people_rows_justcall_format(url, name, enrichment, record_id_base=record_id_base)
            )
        justcall_filename = Path(justcall_output_path) if justcall_output_path else (out / "people_justcall_format.csv")
        if justcall_rows:
            df = pd.DataFrame(justcall_rows)[JUSTCALL_COLUMNS].fillna("")
            df.to_csv(justcall_filename, index=False, quoting=1)
            logger.info(f"Wrote {len(df)} people rows (justcall format) to {justcall_filename}")
        else:
            pd.DataFrame(columns=JUSTCALL_COLUMNS).to_csv(justcall_filename, index=False, quoting=1)
            logger.info(f"Wrote 0 people rows to {justcall_filename}")
        logger.info(f"Summary: {len(justcall_rows)} people records (Attio People format)")
        return

    company_rows = []
    people_rows = []
    for entry in entries:
        url, name = entry["url"], entry["name"]
        domain = _domain(url)
        enrichment = enrichments.get(domain)
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
    parser.add_argument(
        "--output-format",
        choices=("default", "justcall"),
        default="default",
        help="default: companies.csv + people.csv; justcall: single Attio People CSV (same columns as enrich_justcall)",
    )
    parser.add_argument(
        "--justcall-output",
        default=None,
        help="When --output-format justcall: write CSV to this path (default: <output-dir>/people_justcall_format.csv)",
    )
    parser.add_argument("--concurrency", type=int, default=4, help="Concurrent crawls (default: 4)")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between crawls in seconds")
    parser.add_argument("--limit", type=int, default=None, help="Limit to N URLs (for testing)")
    parser.add_argument("--dry-run", action="store_true", help="Preview URLs without crawling")
    parser.add_argument(
        "--force-recrawl", choices=("all", "no-dm"),
        default=None,
        help="Invalidate cached data. 'no-dm': re-crawl domains with no DMs. 'all': re-crawl everything."
    )
    args = parser.parse_args()

    asyncio.run(run(
        input_path=args.input,
        output_dir=args.output,
        checkpoint_path=args.checkpoint,
        concurrency=args.concurrency,
        delay=args.delay,
        limit=args.limit,
        dry_run=args.dry_run,
        output_format=args.output_format,
        justcall_output_path=args.justcall_output,
        force_recrawl=args.force_recrawl,
    ))


if __name__ == "__main__":
    main()
