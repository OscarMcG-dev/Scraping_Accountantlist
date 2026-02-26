"""
JustCall Sales Dialer API client for creating campaigns and adding contacts.

API docs: https://developer.justcall.io/reference
Auth: Authorization header with api_key:api_secret
"""
import logging
from typing import Any, Optional

import httpx

from config import Settings

logger = logging.getLogger(__name__)

BULK_IMPORT_BATCH_SIZE = 500

JUSTCALL_FIELD_MAP = {
    "first_name": "first_name",
    "last_name": "last_name",
    "phone numbers": "phone",
    "phone_numbers": "phone",
    "email addresses": "email",
    "email_addresses": "email",
    "company": "company",
    "notes": "notes",
}

JUSTCALL_CUSTOM_FIELD_MAP = {
    "record id": "attio_record_id",
    "job title": "job_title",
    "company > domains": "website",
    "company_domains": "website",
    "linkedin": "linkedin",
    "description": "description",
    "enrichment_status": "lead_grade",
}


def build_justcall_contact(row: dict, cols_lower_map: dict) -> dict:
    """Transform a CSV row dict into a JustCall contact payload with custom fields."""
    def _val(key: str) -> str:
        col = cols_lower_map.get(key)
        if not col:
            return ""
        v = row.get(col, "")
        return str(v).strip() if v and str(v).strip().lower() != "nan" else ""

    phone = _val("phone numbers") or _val("phone_numbers")
    if not phone:
        return {}

    contact: dict[str, Any] = {
        "first_name": _val("first_name") or "Contact",
        "last_name": _val("last_name"),
        "phone": phone,
    }
    email = _val("email addresses") or _val("email_addresses")
    if email:
        contact["email"] = email
    company = _val("company")
    if company:
        contact["company"] = company

    custom: dict[str, str] = {}
    for csv_key, jc_field in JUSTCALL_CUSTOM_FIELD_MAP.items():
        v = _val(csv_key)
        if v:
            custom[jc_field] = v[:200]
    if custom:
        contact["custom_fields"] = custom

    return contact


def grade_lead(row: dict, cols_lower_map: dict) -> str:
    """Grade a lead A/B/C/D based on available information.

    A = phone + name + title + email (full data)
    B = phone + name + title OR phone + name + email
    C = phone + name only
    D = phone only (or name but missing data)
    """
    def _has(key: str) -> bool:
        col = cols_lower_map.get(key)
        if not col:
            return False
        v = row.get(col, "")
        return bool(v and str(v).strip() and str(v).strip().lower() != "nan")

    has_phone = _has("phone numbers") or _has("phone_numbers")
    has_name = _has("first_name")
    has_title = _has("job title")
    has_email = _has("email addresses") or _has("email_addresses")

    if not has_phone:
        return "D"
    if has_name and has_title and has_email:
        return "A"
    if has_name and (has_title or has_email):
        return "B"
    if has_name:
        return "C"
    return "D"


class JustCallClient:
    """Client for JustCall Sales Dialer APIs (create campaign, add contacts)."""

    def __init__(self, api_key: str = "", api_secret: str = "", base_url: str = ""):
        settings = Settings()
        self.api_key = api_key or settings.justcall_api_key
        self.api_secret = api_secret or settings.justcall_api_secret
        self.base_url = (base_url or settings.justcall_base_url).rstrip("/")
        self._auth = f"{self.api_key}:{self.api_secret}"
        self._headers = {
            "Authorization": self._auth,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def is_configured(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def _request(
        self,
        method: str,
        path: str,
        json: Optional[dict] = None,
        timeout: float = 30.0,
    ) -> dict:
        url = f"{self.base_url}{path}"
        with httpx.Client(timeout=timeout) as client:
            resp = client.request(
                method,
                url,
                headers=self._headers,
                json=json,
            )
            resp.raise_for_status()
            return resp.json() if resp.content else {}

    async def _request_async(
        self,
        method: str,
        path: str,
        json: Optional[dict] = None,
        timeout: float = 30.0,
    ) -> dict:
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(
                method,
                url,
                headers=self._headers,
                json=json,
            )
            resp.raise_for_status()
            return resp.json() if resp.content else {}

    def create_campaign(
        self,
        name: str,
        campaign_type: str = "Autodial",
        default_number: Optional[str] = None,
        country_code: str = "AU",
        contact_dialing_order: str = "First in first out",
    ) -> dict:
        """
        Create a Sales Dialer campaign.
        campaign_type: Autodial, Predictive, or Dynamic.
        Returns dict with campaign id, name, etc.
        """
        body: dict[str, Any] = {
            "name": name,
            "type": campaign_type,
            "country_code": country_code,
            "contact_dialing_order": contact_dialing_order,
        }
        if default_number:
            body["default_number"] = default_number
        return self._request("POST", "/sales_dialer/campaigns", json=body)

    async def create_campaign_async(
        self,
        name: str,
        campaign_type: str = "Autodial",
        default_number: Optional[str] = None,
        country_code: str = "AU",
        contact_dialing_order: str = "First in first out",
    ) -> dict:
        body: dict[str, Any] = {
            "name": name,
            "type": campaign_type,
            "country_code": country_code,
            "contact_dialing_order": contact_dialing_order,
        }
        if default_number:
            body["default_number"] = default_number
        return await self._request_async("POST", "/sales_dialer/campaigns", json=body)

    def add_contact_to_campaign(
        self,
        campaign_id: str,
        first_name: str,
        last_name: str,
        phone: str,
        email: Optional[str] = None,
        company: Optional[str] = None,
        custom_fields: Optional[dict] = None,
    ) -> dict:
        """Add a single contact to a campaign. Phone must be E.164 or with country code."""
        body: dict[str, Any] = {
            "first_name": first_name or "",
            "last_name": last_name or "",
            "phone": phone,
        }
        if email:
            body["email"] = email
        if company:
            body["company"] = company
        if custom_fields:
            body["custom_fields"] = custom_fields
        return self._request(
            "POST",
            f"/sales_dialer/campaigns/{campaign_id}/contacts",
            json=body,
        )

    def bulk_import_contacts(
        self,
        campaign_id: str,
        contacts: list[dict],
        callback_url: Optional[str] = None,
    ) -> dict:
        """
        Bulk import contacts into a campaign.
        Each contact: { first_name, last_name, phone [, email, company ] }
        Returns batch/import status.
        """
        body: dict[str, Any] = {
            "campaign_id": campaign_id,
            "contacts": contacts,
        }
        if callback_url:
            body["callback_url"] = callback_url
        return self._request(
            "POST",
            "/sales_dialer/contacts/bulk_import",
            json=body,
            timeout=60.0,
        )

    async def bulk_import_contacts_async(
        self,
        campaign_id: str,
        contacts: list[dict],
        callback_url: Optional[str] = None,
    ) -> dict:
        results: list[dict] = []
        for i in range(0, len(contacts), BULK_IMPORT_BATCH_SIZE):
            batch = contacts[i : i + BULK_IMPORT_BATCH_SIZE]
            body: dict[str, Any] = {
                "campaign_id": campaign_id,
                "contacts": batch,
            }
            if callback_url:
                body["callback_url"] = callback_url
            r = await self._request_async(
                "POST",
                "/sales_dialer/contacts/bulk_import",
                json=body,
                timeout=60.0,
            )
            results.append(r)
            logger.info(f"Bulk import batch {i // BULK_IMPORT_BATCH_SIZE + 1}: {len(batch)} contacts")
        return {"batches": len(results), "total_contacts": len(contacts), "results": results}

    def get_campaign(self, campaign_id: str) -> dict:
        """Get campaign details and contact counts."""
        return self._request("GET", f"/sales_dialer/campaigns/{campaign_id}")
