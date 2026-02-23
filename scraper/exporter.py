"""
Phase 3b: Generate Attio-ready CSVs.

Outputs:
  1. complete_scrape.csv       -- all firms, with attio_status column
  2. new_companies_for_import.csv  -- only NEW firms (ready for Attio CSV import)
  3. existing_companies_enrichment.csv -- only EXISTING firms (for manual review)
  4. people_for_import.csv     -- all people records
"""
import logging
import re
from pathlib import Path
from typing import List, Optional, Dict

import pandas as pd

from models import CompanyRecord, PersonRecord, DirectoryListing, EnrichmentData, DecisionMaker
from phone_utils import normalize_to_e164
from segment_mapper import map_areas_to_segment
from attio_dedup import extract_domain

logger = logging.getLogger(__name__)

# AU state abbreviation to full name (for Attio location.region)
STATE_FULL_NAMES = {
    "VIC": "Victoria",
    "NSW": "New South Wales",
    "QLD": "Queensland",
    "SA": "South Australia",
    "WA": "Western Australia",
    "TAS": "Tasmania",
    "NT": "Northern Territory",
    "ACT": "Australian Capital Territory",
}


def parse_address(raw: Optional[str]) -> dict:
    """
    Parse an accountantlist.com.au address string into Attio location components.

    Common formats:
      "Level 4. 36 Carrington St Sydney. NSW 2000"
      "Suite 9/27 Hunter St Parramatta. NSW 2150"
      "3/345 Kingsway,. Caringbah NSW 2229"
    """
    result = {
        "line_1": None,
        "locality": None,
        "region": None,
        "postcode": None,
    }
    if not raw:
        return result

    # Extract state + postcode from end
    match = re.search(r"[.\s,]?\s*(VIC|NSW|QLD|SA|WA|TAS|NT|ACT)\s+(\d{4})\s*$", raw)
    if match:
        result["region"] = STATE_FULL_NAMES.get(match.group(1), match.group(1))
        result["postcode"] = match.group(2)

        before_state = raw[:match.start()].rstrip(". ,")
        # Split on periods to separate structured parts
        parts = re.split(r"\.\s*", before_state)
        parts = [p.strip(" ,") for p in parts if p.strip(" ,")]

        if len(parts) >= 2:
            # Last part is typically the suburb/locality
            result["locality"] = parts[-1]
            result["line_1"] = ", ".join(parts[:-1])
        elif parts:
            # Single part -- try to extract suburb as last capitalized word(s)
            # that aren't street-type words
            result["line_1"] = parts[0]
            # Attempt heuristic: last word before state is often the suburb
            words = parts[0].rsplit(" ", 1)
            if len(words) == 2:
                result["locality"] = words[-1]
                result["line_1"] = words[0]
    else:
        result["line_1"] = raw

    return result


def build_company_records(
    listings: List[DirectoryListing],
    enrichments: Dict[str, EnrichmentData],
) -> List[CompanyRecord]:
    """Merge directory listings with enrichment data into CompanyRecords."""
    records = []

    for listing in listings:
        domain = extract_domain(listing.website_url)
        addr = parse_address(listing.street_address)
        segment = map_areas_to_segment(listing.areas_of_accountancy)
        phone = normalize_to_e164(listing.phone) if listing.phone else None

        # Start with directory data
        record = CompanyRecord(
            domains=domain,
            name=listing.name,
            segment=segment,
            office_phone=phone,
            office_email=listing.email,
            primary_location_line_1=addr["line_1"],
            primary_location_locality=addr["locality"],
            primary_location_region=addr["region"],
            primary_location_postcode=addr["postcode"],
            associated_location_4=listing.street_address or "",
            dm_1_name_temp=listing.contact_name,
            listing_url=listing.listing_url,
        )

        # If state was parsed from address but not from index
        if not record.primary_location_region and listing.state:
            record.primary_location_region = STATE_FULL_NAMES.get(
                listing.state, listing.state
            )

        # Merge enrichment data if available
        enrichment = enrichments.get(listing.website_url) if listing.website_url else None
        if enrichment and not enrichment.out_of_scope:
            if enrichment.description:
                record.description = enrichment.description
            if enrichment.office_phone and not record.office_phone:
                record.office_phone = enrichment.office_phone
            if enrichment.office_email and not record.office_email:
                record.office_email = enrichment.office_email
            if enrichment.associated_emails:
                record.associated_emails_1 = "; ".join(enrichment.associated_emails)
            if enrichment.associated_mobiles:
                record.associated_mobiles = enrichment.associated_mobiles
            if enrichment.associated_info:
                record.associated_location = enrichment.associated_info
            if enrichment.organisational_structure:
                record.organisational_structure = enrichment.organisational_structure
            if enrichment.linkedin:
                record.linkedin = enrichment.linkedin
            if enrichment.facebook:
                record.facebook = enrichment.facebook

            # Use first decision maker name if no contact from directory
            if not record.dm_1_name_temp and enrichment.decision_makers:
                record.dm_1_name_temp = enrichment.decision_makers[0].name

        records.append(record)

    return records


def build_people_records(
    listings: List[DirectoryListing],
    enrichments: Dict[str, EnrichmentData],
) -> List[PersonRecord]:
    """Build PersonRecords from directory contacts and enrichment decision makers.

    Falls back to creating a 'Contact at <Firm>' record when no named DMs are
    found but orphan phone numbers or emails were discovered during enrichment.
    """
    people = []
    seen_keys = set()

    for listing in listings:
        enrichment = enrichments.get(listing.website_url) if listing.website_url else None
        domain = extract_domain(listing.website_url)

        named_dms_added = False

        # From enrichment decision makers
        if enrichment and enrichment.decision_makers:
            for dm in enrichment.decision_makers:
                if not dm.name:
                    continue

                key = (dm.name, dm.email or "")
                if key in seen_keys:
                    continue
                seen_keys.add(key)

                name_parts = _split_name(dm.name)
                phones = [
                    p for p in [dm.phone_office, dm.phone_mobile, dm.phone_direct]
                    if p
                ]

                people.append(PersonRecord(
                    first_name=name_parts[0],
                    last_name=name_parts[1],
                    email_addresses=dm.email,
                    job_title=dm.title,
                    phone_numbers=phones,
                    linkedin=dm.linkedin,
                    company_name=listing.name,
                    company_domain=domain,
                ))
                named_dms_added = True

        if named_dms_added:
            continue

        # From directory contact name (if not already added via enrichment)
        if listing.contact_name:
            key = (listing.contact_name, listing.email or "")
            if key in seen_keys:
                continue
            seen_keys.add(key)

            name_parts = _split_name(listing.contact_name)
            people.append(PersonRecord(
                first_name=name_parts[0],
                last_name=name_parts[1],
                email_addresses=listing.email,
                company_name=listing.name,
                company_domain=domain,
            ))
            continue

        # Fallback: no named DMs and no directory contact, but enrichment found
        # orphan phones/emails. Create a stub record so the data isn't lost.
        if enrichment and not enrichment.out_of_scope:
            orphan_phones = list(enrichment.associated_mobiles or [])
            if enrichment.office_phone:
                orphan_phones.insert(0, enrichment.office_phone)
            orphan_email = enrichment.office_email
            if not orphan_email and enrichment.associated_emails:
                orphan_email = enrichment.associated_emails[0]

            if orphan_phones or orphan_email:
                fallback_key = ("__fallback__", listing.name)
                if fallback_key not in seen_keys:
                    seen_keys.add(fallback_key)
                    people.append(PersonRecord(
                        first_name="Contact at",
                        last_name=listing.name,
                        email_addresses=orphan_email,
                        job_title="Office Contact",
                        phone_numbers=orphan_phones,
                        company_name=listing.name,
                        company_domain=domain,
                    ))

    return people


def _split_name(full_name: str) -> tuple:
    """Split a full name into (first, last). Handles 'Last, First' format."""
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


def export_csvs(
    records: List[CompanyRecord],
    people: List[PersonRecord],
    output_dir: str = "data/output",
) -> dict:
    """
    Write all CSVs and return paths.

    Returns dict with keys: complete, new, existing, people
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {}

    # -- Company CSVs --
    company_dicts = [_company_to_export_dict(r) for r in records]
    df_all = pd.DataFrame(company_dicts)

    # 1. Complete scrape
    p = out / "complete_scrape.csv"
    df_all.to_csv(p, index=False, quoting=1)
    paths["complete"] = str(p)
    logger.info(f"Wrote {len(df_all)} rows to {p}")

    # 2. New companies for import (exclude internal columns)
    df_new = df_all[df_all["attio_status"] == "new"].drop(
        columns=["attio_status", "attio_record_id", "listing_url"], errors="ignore"
    )
    p = out / "new_companies_for_import.csv"
    df_new.to_csv(p, index=False, quoting=1)
    paths["new"] = str(p)
    logger.info(f"Wrote {len(df_new)} new companies to {p}")

    # 3. Existing companies enrichment
    df_existing = df_all[df_all["attio_status"] == "existing"]
    p = out / "existing_companies_enrichment.csv"
    df_existing.to_csv(p, index=False, quoting=1)
    paths["existing"] = str(p)
    logger.info(f"Wrote {len(df_existing)} existing companies to {p}")

    # -- People CSV --
    if people:
        people_dicts = [_person_to_export_dict(p) for p in people]
        df_people = pd.DataFrame(people_dicts)
        p = out / "people_for_import.csv"
        df_people.to_csv(p, index=False, quoting=1)
        paths["people"] = str(p)
        logger.info(f"Wrote {len(df_people)} people to {p}")
    else:
        paths["people"] = ""

    return paths


def _format_primary_location(r: CompanyRecord) -> str:
    """
    Build a single location string for Attio's native location parser.
    Format: "line_1, locality, region, postcode, AU"
    """
    parts = []
    if r.primary_location_line_1:
        parts.append(r.primary_location_line_1)
    if r.primary_location_locality:
        parts.append(r.primary_location_locality)
    if r.primary_location_region:
        parts.append(r.primary_location_region)
    if r.primary_location_postcode:
        parts.append(r.primary_location_postcode)
    if parts:
        parts.append(r.primary_location_country_code)
    return ", ".join(parts)


def _company_to_export_dict(r: CompanyRecord) -> dict:
    """Flatten a CompanyRecord to an Attio-importable CSV row."""
    return {
        "domains": r.domains or "",
        "name": r.name,
        "description": r.description,
        "primary_location": _format_primary_location(r),
        "segment": r.segment or "",
        "office_phone": r.office_phone or "",
        "office_email": r.office_email or "",
        "associated_mobiles": "; ".join(r.associated_mobiles) if r.associated_mobiles else "",
        "associated_emails_1": r.associated_emails_1,
        "associated_location": r.associated_location,
        "associated_location_4": r.associated_location_4,
        "organisational_structure": r.organisational_structure or "",
        "linkedin": r.linkedin or "",
        "facebook": r.facebook or "",
        "original_data_source_scrape": r.original_data_source_scrape,
        "dm_1_name_temp": r.dm_1_name_temp or "",
        "attio_status": r.attio_status,
        "attio_record_id": r.attio_record_id or "",
        "listing_url": r.listing_url,
    }


def _person_to_export_dict(p: PersonRecord) -> dict:
    return {
        "first_name": p.first_name or "",
        "last_name": p.last_name or "",
        "email_addresses": p.email_addresses or "",
        "job_title": p.job_title or "",
        "phone_numbers": "; ".join(p.phone_numbers) if p.phone_numbers else "",
        "linkedin": p.linkedin or "",
        "company_name": p.company_name,
        "company_domain": p.company_domain or "",
    }
