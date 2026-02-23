"""
Enrich Attio People records that have poor data (placeholder names, generic titles)
by crawling their associated company websites via Phase 2 enrichment.

Input:  Attio People CSV export (with Record ID, Company, Company > Domains, etc.)
Output: Attio-compatible People update CSV (matched on Record ID) with real names,
        titles, and enriched data filled in.

Usage:
    python enrich_justcall.py --input "path/to/attio_export.csv"
    python enrich_justcall.py --input "path/to/attio_export.csv" --limit 5 --dry-run
    python enrich_justcall.py --input "path/to/attio_export.csv" --output "data/output/enriched.csv"
    python enrich_justcall.py --input "path/to/campaign_contacts.csv" --format campaign
    python enrich_justcall.py --input "path/to/attio_export.csv" --concurrency 4
"""
import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Optional

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

from config import Settings
from checkpoint import Checkpoint
from website_enricher import WebsiteEnricher, prefilter_domains
from models import EnrichmentData, DecisionMaker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("enrich_justcall.log"),
    ],
)
logger = logging.getLogger(__name__)

PLACEHOLDER_PREFIXES = ("team member at",)
GENERIC_TITLES = {"office contact", "meet the team", ""}
JUNK_NAMES = {
    "customercare", "reception", "admin", "info", "enquiries", "accounts",
    "no posts found on your query!", "teetrees", "touch hello",
}

SENIORITY_ORDER = [
    "managing partner", "senior partner", "partner",
    "managing director", "director",
    "principal", "founder", "owner",
    "chartered accountant", "chief executive",
]

# Campaign/JustCall CSV column names -> canonical Attio-style names used internally.
# Ensures we never mix up fields (e.g. Phone vs Email) when reading different exports.
CAMPAIGN_TO_ATTIO_COLUMNS = {
    "Person Record ID": "Record ID",
    "Name": "Record",
    "Occupation": "Job title",
    "Email": "Email addresses",
    "Phone": "Phone numbers",
    "Website": "Company > Domains",
}


def _is_campaign_format(df: pd.DataFrame) -> bool:
    """True if CSV has Campaign/JustCall columns (Person Record ID, Name, Website)."""
    return (
        "Person Record ID" in df.columns
        and "Name" in df.columns
        and "Website" in df.columns
    )


def _normalize_csv_columns(df: pd.DataFrame, csv_format: Optional[str]) -> pd.DataFrame:
    """
    Normalize input columns to the canonical names expected by the rest of the pipeline.
    - attio: no change (already uses Record ID, Record, Job title, etc.).
    - campaign: rename Campaign/JustCall columns to Attio names; only renames columns
      that exist so we never overwrite or mix data.
    """
    if csv_format == "attio":
        return df
    if csv_format == "campaign" or (csv_format is None and _is_campaign_format(df)):
        rename = {
            old: new
            for old, new in CAMPAIGN_TO_ATTIO_COLUMNS.items()
            if old in df.columns
        }
        if rename:
            df = df.rename(columns=rename)
            logger.info(f"Normalized Campaign columns to Attio names: {list(rename.keys())} -> {list(rename.values())}")
    return df


def needs_enrichment(record_name: str, job_title: str) -> bool:
    """Return True if this record has poor data that enrichment could fix."""
    name_lower = (record_name or "").strip().lower()
    title_lower = (job_title or "").strip().lower()

    is_placeholder = any(name_lower.startswith(p) for p in PLACEHOLDER_PREFIXES)
    is_junk_name = name_lower in JUNK_NAMES
    is_single_word = len(name_lower.split()) == 1 and not is_placeholder
    is_generic_title = title_lower in GENERIC_TITLES

    return is_placeholder or is_junk_name or is_generic_title or is_single_word


def rank_decision_maker(dm: DecisionMaker) -> int:
    """Lower = more senior. Used to pick the best DM for a record."""
    title_lower = (dm.title or "").lower()
    for i, keyword in enumerate(SENIORITY_ORDER):
        if keyword in title_lower:
            return i
    return len(SENIORITY_ORDER)


def split_name(full_name: str) -> tuple:
    """Split a full name into (first, last)."""
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


def _safe_str(val) -> str:
    """Convert a pandas value to string, treating NaN/None as empty."""
    if pd.isna(val):
        return ""
    return str(val).strip()


def build_enriched_row(
    original: pd.Series,
    enrichment: Optional[EnrichmentData],
) -> dict:
    """
    Build an output row for the Attio People update CSV.
    Preserves existing good data, fills in gaps from enrichment.
    """
    record_id = original["Record ID"]
    current_name = _safe_str(original.get("Record", ""))
    current_title = _safe_str(original.get("Job title", ""))
    current_email = _safe_str(original.get("Email addresses", ""))
    current_phone = _safe_str(original.get("Phone numbers", ""))
    company = _safe_str(original.get("Company", ""))
    domain = _safe_str(original.get("Company > Domains", ""))

    row = {
        "Record ID": record_id,
        "Company": company,
        "Company > Domains": domain,
        "Phone numbers": current_phone,
        "Email addresses": current_email,
        "first_name": "",
        "last_name": "",
        "Job title": current_title if current_title.lower() not in GENERIC_TITLES else "",
        "Description": "",
        "LinkedIn": "",
        "enrichment_status": "no_enrichment",
    }

    if not enrichment or enrichment.out_of_scope:
        if enrichment and enrichment.out_of_scope:
            row["enrichment_status"] = f"out_of_scope: {enrichment.out_of_scope_reason or ''}"
        _fill_name_from_current(row, current_name)
        return row

    name_lower = current_name.strip().lower()
    is_placeholder = any(name_lower.startswith(p) for p in PLACEHOLDER_PREFIXES)
    is_junk = name_lower in JUNK_NAMES

    dm = _pick_best_dm(enrichment.decision_makers)

    if dm and (is_placeholder or is_junk):
        first, last = split_name(dm.name)
        row["first_name"] = first
        row["last_name"] = last
        row["Job title"] = dm.title or row["Job title"]
        row["enrichment_status"] = "name_replaced"

        if dm.email and not current_email:
            row["Email addresses"] = dm.email
        if dm.linkedin:
            row["LinkedIn"] = dm.linkedin
        if not row["Phone numbers"]:
            _fill_phones_from_dm(row, dm)
    elif dm and not is_placeholder and not is_junk:
        _fill_name_from_current(row, current_name)
        if row["Job title"].lower() in GENERIC_TITLES or not row["Job title"]:
            row["Job title"] = dm.title or ""
            row["enrichment_status"] = "title_upgraded"
        else:
            row["enrichment_status"] = "existing_kept"

        if dm.email and not row["Email addresses"]:
            row["Email addresses"] = dm.email
        if dm.linkedin and not row["LinkedIn"]:
            row["LinkedIn"] = dm.linkedin
        if not row["Phone numbers"]:
            _fill_phones_from_dm(row, dm)
    else:
        # No named decision maker: keep or set name, then back-fill contact info below.
        has_contact_info = bool(
            (enrichment.office_phone or enrichment.associated_mobiles or
             enrichment.office_email or enrichment.associated_emails)
        )
        if has_contact_info and (is_placeholder or is_junk or not (current_name or "").strip()):
            # Placeholder record so contact info is not lost: "Contact at [Company]"
            company_name = _safe_str(original.get("Company", "")) or "Firm"
            row["first_name"] = "Contact at"
            row["last_name"] = company_name
            row["Job title"] = row["Job title"] or "Office Contact"
            row["enrichment_status"] = "no_dm_found_contact_only"
        else:
            _fill_name_from_current(row, current_name)
            row["enrichment_status"] = "no_dm_found"

    # Always fill in company-level data from enrichment, even without DMs
    if enrichment.edited_description:
        row["Description"] = enrichment.edited_description

    if not row["Email addresses"] and enrichment.office_email:
        row["Email addresses"] = enrichment.office_email
    if not row["Email addresses"] and enrichment.associated_emails:
        row["Email addresses"] = enrichment.associated_emails[0]

    _fill_phones_from_enrichment(row, enrichment)

    if not row["LinkedIn"] and enrichment.linkedin:
        row["LinkedIn"] = enrichment.linkedin

    return row


def _fill_name_from_current(row: dict, current_name: str) -> None:
    """Fill first/last name from the current record name if it's a real name."""
    name_lower = current_name.strip().lower()
    if any(name_lower.startswith(p) for p in PLACEHOLDER_PREFIXES) or name_lower in JUNK_NAMES:
        return
    first, last = split_name(current_name)
    row["first_name"] = first
    row["last_name"] = last


def _pick_best_dm(dms: list) -> Optional[DecisionMaker]:
    """Pick the most senior decision maker from the list."""
    if not dms:
        return None
    named = [dm for dm in dms if dm.name and dm.name.strip()]
    if not named:
        return None
    return min(named, key=rank_decision_maker)


def _fill_phones_from_dm(row: dict, dm: DecisionMaker) -> None:
    """Back-fill phone from DM's personal numbers when the record has none."""
    phones = []
    for p in (dm.phone_office, dm.phone_mobile, dm.phone_direct):
        if p and p not in phones:
            phones.append(p)
    if phones:
        row["Phone numbers"] = "; ".join(phones)


def _fill_phones_from_enrichment(row: dict, enrichment: EnrichmentData) -> None:
    """Back-fill phone(s) from enrichment data if the record has none. Joins multiple numbers."""
    if row["Phone numbers"]:
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


async def run_enrichment(
    input_path: str,
    output_path: str,
    checkpoint_path: str,
    limit: Optional[int] = None,
    dry_run: bool = False,
    delay: float = 1.0,
    concurrency: int = 4,
    force_recrawl: Optional[str] = None,
    csv_format: Optional[str] = None,
):
    """Main enrichment pipeline with concurrent crawling."""
    settings = Settings()

    logger.info(f"Loading CSV from: {input_path}")
    df = pd.read_csv(input_path)
    logger.info(f"Loaded {len(df)} records")

    df = _normalize_csv_columns(df, csv_format)

    df["_needs_enrichment"] = df.apply(
        lambda r: needs_enrichment(str(r.get("Record", "")), str(r.get("Job title", ""))),
        axis=1,
    )
    needs_it = df["_needs_enrichment"].sum()
    logger.info(f"{needs_it}/{len(df)} records need enrichment")

    raw_domains = (
        df[df["_needs_enrichment"]]["Company > Domains"]
        .dropna()
        .str.strip()
        .str.lower()
        .unique()
        .tolist()
    )
    logger.info(f"{len(raw_domains)} unique domains before pre-filter")

    domains_to_enrich, skipped = prefilter_domains(raw_domains)
    if skipped:
        logger.info(f"Pre-filter skipped {len(skipped)} domains (DNS/invalid)")
        for dom, reason in skipped[:10]:
            logger.debug(f"  Skipped {dom}: {reason}")

    logger.info(f"{len(domains_to_enrich)} domains to crawl after pre-filter")

    if limit:
        domains_to_enrich = domains_to_enrich[:limit]
        logger.info(f"Limited to {limit} domains")

    if dry_run:
        logger.info("DRY RUN -- would crawl these domains:")
        for d in domains_to_enrich[:20]:
            logger.info(f"  {d}")
        if len(domains_to_enrich) > 20:
            logger.info(f"  ... and {len(domains_to_enrich) - 20} more")
        return

    checkpoint = Checkpoint(checkpoint_path)

    if force_recrawl == "all":
        count = checkpoint.invalidate_all_enrichments()
        logger.info(f"Force recrawl (all): invalidated {count} cached enrichments")
    elif force_recrawl == "no-dm":
        count = checkpoint.invalidate_no_dm_urls()
        logger.info(f"Force recrawl (no-dm): invalidated {count} domains with no decision makers")

    enrichments: dict[str, EnrichmentData] = {}

    for url, data in checkpoint.get_all_enrichments().items():
        try:
            enrichments[url] = EnrichmentData(**data)
        except Exception:
            pass

    already_done = checkpoint.get_enriched_urls()
    remaining = [d for d in domains_to_enrich if d not in already_done]
    logger.info(
        f"Crawling {len(remaining)} domains ({len(already_done)} already cached) "
        f"with concurrency={concurrency}"
    )

    if remaining:
        enricher = WebsiteEnricher(settings)
        await enricher.start_pool(size=concurrency)

        semaphore = asyncio.Semaphore(concurrency)
        counter = {"done": 0, "start_time": time.monotonic()}
        total = len(remaining)

        domain_to_company = {}
        for _, row in df.iterrows():
            d = str(row.get("Company > Domains", "")).strip().lower()
            if d and d not in domain_to_company:
                domain_to_company[d] = str(row.get("Company", ""))

        async def _enrich_one(domain: str) -> None:
            async with semaphore:
                firm_name = domain_to_company.get(domain, domain)
                counter["done"] += 1
                idx = counter["done"]
                elapsed = time.monotonic() - counter["start_time"]
                rate = idx / elapsed * 60 if elapsed > 0 else 0
                eta_min = (total - idx) / rate if rate > 0 else 0
                logger.info(
                    f"[{idx}/{total}] Enriching: {firm_name} ({domain}) "
                    f"[{rate:.1f}/min, ETA {eta_min:.0f}min]"
                )

                try:
                    enrichment = await enricher.enrich(domain, firm_name)
                    if enrichment:
                        enrichments[domain] = enrichment
                        checkpoint.save_enrichment(domain, enrichment.model_dump())
                        dm_count = len(enrichment.decision_makers)
                        logger.info(f"  -> {dm_count} decision maker(s) found")
                    else:
                        checkpoint.mark_enriched(domain)
                        logger.info("  -> No data extracted")
                except Exception as e:
                    logger.error(f"  -> Failed: {e}")
                    checkpoint.mark_enriched(domain)

                await asyncio.sleep(delay)

        try:
            batch_size = concurrency * 3
            for batch_start in range(0, total, batch_size):
                batch = remaining[batch_start:batch_start + batch_size]
                await asyncio.gather(*[_enrich_one(d) for d in batch])
        finally:
            await enricher.stop_pool()

        elapsed_total = time.monotonic() - counter["start_time"]
        logger.info(
            f"Enrichment complete: {counter['done']} domains in "
            f"{elapsed_total/60:.1f} minutes "
            f"({counter['done']/elapsed_total*60:.1f}/min average)"
        )

    logger.info("=" * 60)
    logger.info("Building enriched output CSV")
    logger.info("=" * 60)

    output_rows = []
    for _, original in df.iterrows():
        domain = str(original.get("Company > Domains", "")).strip().lower()
        enrichment = enrichments.get(domain)
        row = build_enriched_row(original, enrichment)
        output_rows.append(row)

    out_df = pd.DataFrame(output_rows)
    out_df = out_df.fillna("")

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(out_path, index=False, quoting=1)
    logger.info(f"Wrote {len(out_df)} rows to {out_path}")

    status_counts = out_df["enrichment_status"].value_counts()
    logger.info("Enrichment status summary:")
    for status, count in status_counts.items():
        logger.info(f"  {status}: {count}")

    has_name = (out_df["first_name"].str.strip() != "").sum()
    has_title = (out_df["Job title"].str.strip() != "").sum()
    has_desc = (out_df["Description"].str.strip() != "").sum()
    logger.info(f"Output quality: {has_name} with names, {has_title} with titles, {has_desc} with descriptions")


def main():
    parser = argparse.ArgumentParser(
        description="Enrich Attio People records by crawling company websites"
    )
    parser.add_argument(
        "--input", required=True,
        help="Path to the Attio People export CSV or Campaign/JustCall contacts CSV"
    )
    parser.add_argument(
        "--format", dest="csv_format", choices=("attio", "campaign"), default=None,
        help="Input CSV format: 'attio' (Record ID, Record, Job title, Company > Domains, ...) "
             "or 'campaign' (Person Record ID, Name, Occupation, Website, ...). Default: auto-detect."
    )
    parser.add_argument(
        "--output", default="data/output/attio_people_update.csv",
        help="Output path for the enriched CSV (default: data/output/attio_people_update.csv)"
    )
    parser.add_argument(
        "--checkpoint", default="data/state/justcall_enrichment_checkpoint.json",
        help="Path to checkpoint file for resume support"
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Limit to N domains (for testing)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview which domains would be crawled without actually crawling"
    )
    parser.add_argument(
        "--delay", type=float, default=1.0,
        help="Delay between crawls in seconds (default: 1.0)"
    )
    parser.add_argument(
        "--concurrency", type=int, default=4,
        help="Number of firms to enrich concurrently (default: 4)"
    )
    parser.add_argument(
        "--force-recrawl", choices=("all", "no-dm"),
        default=None,
        help="Invalidate cached checkpoint data before running. "
             "'no-dm': re-crawl domains that found no decision makers. "
             "'all': wipe all cached enrichments and re-crawl everything."
    )

    args = parser.parse_args()

    asyncio.run(run_enrichment(
        input_path=args.input,
        output_path=args.output,
        checkpoint_path=args.checkpoint,
        limit=args.limit,
        dry_run=args.dry_run,
        delay=args.delay,
        concurrency=args.concurrency,
        force_recrawl=args.force_recrawl,
        csv_format=args.csv_format,
    ))


if __name__ == "__main__":
    main()
