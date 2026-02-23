"""
Phase 3a: Deduplicate scraped firms against existing Attio records.

Bulk-exports all company domains and phone numbers from Attio via
the REST API, then classifies each scraped firm as 'new' or 'existing'.
"""
import logging
from typing import Dict, Set, Tuple, Optional
from urllib.parse import urlparse

import httpx

from models import CompanyRecord

logger = logging.getLogger(__name__)

ATTIO_API_BASE = "https://api.attio.com/v2"


async def export_attio_lookups(api_key: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    """
    Paginate through all Attio company records and build lookup dicts.

    Returns:
        (domain_to_record_id, phone_to_record_id) mapping dicts.
    """
    domain_lookup: Dict[str, str] = {}
    phone_lookup: Dict[str, str] = {}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    offset = 0
    page_size = 50
    total_fetched = 0

    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            payload = {
                "sorts": [{"attribute": "created_at", "direction": "asc"}],
                "limit": page_size,
                "offset": offset,
            }
            resp = await client.post(
                f"{ATTIO_API_BASE}/objects/companies/records/query",
                headers=headers,
                json=payload,
            )

            if resp.status_code != 200:
                logger.error(f"Attio API error ({resp.status_code}): {resp.text[:500]}")
                break

            data = resp.json()
            records = data.get("data", [])
            if not records:
                break

            for record in records:
                record_id = record.get("id", {}).get("record_id", "")
                values = record.get("values", {})

                # Extract domains
                for domain_entry in values.get("domains", []):
                    domain = domain_entry.get("domain", "")
                    if domain:
                        domain_lookup[domain.lower()] = record_id

                # Extract office phone
                for phone_entry in values.get("office_phone", []):
                    phone = phone_entry.get("original_phone_number", "")
                    if phone:
                        phone_lookup[phone] = record_id

            total_fetched += len(records)
            logger.info(f"Fetched {total_fetched} Attio records...")

            if not data.get("next_page_token") and len(records) < page_size:
                break

            offset += page_size

    logger.info(f"Attio export complete: {len(domain_lookup)} domains, "
                f"{len(phone_lookup)} phones across {total_fetched} records")
    return domain_lookup, phone_lookup


def extract_domain(url: Optional[str]) -> Optional[str]:
    """Extract the bare domain from a URL (no www prefix)."""
    if not url:
        return None
    if not url.startswith("http"):
        url = "https://" + url
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        return domain if domain else None
    except Exception:
        return None


def classify_records(
    records: list[CompanyRecord],
    domain_lookup: Dict[str, str],
    phone_lookup: Dict[str, str],
) -> list[CompanyRecord]:
    """
    Set attio_status and attio_record_id on each record.
    Matches by domain first, then by phone number.
    """
    new_count = 0
    existing_count = 0

    for record in records:
        matched_id = None

        # Try domain match
        if record.domains:
            domain = record.domains.lower()
            matched_id = domain_lookup.get(domain)

        # Fallback: phone match
        if not matched_id and record.office_phone:
            matched_id = phone_lookup.get(record.office_phone)

        if matched_id:
            record.attio_status = "existing"
            record.attio_record_id = matched_id
            existing_count += 1
        else:
            record.attio_status = "new"
            new_count += 1

    logger.info(f"Classification: {new_count} new, {existing_count} existing")
    return records
