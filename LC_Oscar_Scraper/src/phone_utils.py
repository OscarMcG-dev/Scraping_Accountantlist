"""
Phone number normalization utilities for AU/NZ/UK.
"""
import phonenumbers
from typing import Optional, List
import re


def normalize_to_e164(phone: str, default_country: str = "AU") -> Optional[str]:
    """
    Parse and normalize phone number to E.164 format for AU/NZ/UK.

    Args:
        phone: Phone number string in various formats
        default_country: Default country code if not detected (AU, NZ, or UK)

    Returns:
        E.164 formatted phone number or None if invalid
    """
    if not phone:
        return None

    # Remove common formatting
    phone = phone.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")

    # Minimum length check - a valid phone must have at least 6-7 digits
    if len(phone) < 6:
        return None

    # Map common country names to ISO 3166-1 alpha-2 codes for phonenumbers
    country_map = {
        "AU": "AU",
        "NZ": "NZ",
        "UK": "GB",  # phonenumbers uses "GB" for United Kingdom
    }

    # Get the ISO code for phonenumbers parsing
    phonenumbers_country = country_map.get(default_country.upper(), default_country.upper())

    # If already in E.164 format, validate and return
    if phone.startswith("+"):
        try:
            parsed = phonenumbers.parse(phone, None)
            country_code = str(parsed.country_code) if parsed.country_code else None
            # Accept AU (+61), NZ (+64), UK (+44)
            if country_code in ["61", "64", "44"]:
                return phone
        except Exception:
            pass
        return None

    # Try parsing with default country
    try:
        parsed = phonenumbers.parse(phone, phonenumbers_country)
        if not parsed or not parsed.country_code:
            return None

        country_code = str(parsed.country_code)

        # Check if country code matches our target countries
        if country_code not in ["61", "64", "44"]:
            return None

        # Format to E.164
        e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
        return e164

    except Exception:
        # Fallback: Try parsing without default country
        try:
            parsed = phonenumbers.parse(phone, None)
            if not parsed or not parsed.country_code:
                return None
            country_code = str(parsed.country_code)
            if country_code in ["61", "64", "44"]:
                return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
        except Exception:
            pass

    return None


def detect_country_code(phone: str) -> Optional[str]:
    """
    Detect country code from phone number (AU/NZ/UK).

    Returns:
        Country code as "AU", "NZ", or "UK", or None
    """
    try:
        parsed = phonenumbers.parse(phone, None)
        country_code = str(parsed.country_code) if parsed.country_code else None

        # AU: +61, NZ: +64, UK: +44
        if country_code == "61":
            return "AU"
        elif country_code == "64":
            return "NZ"
        elif country_code == "44":
            return "UK"

        return None
    except Exception:
        return None


def classify_phone_type(phone: str) -> str:
    """
    Classify phone as mobile, office, or direct based on pattern.

    Note: This is a heuristic classification based on AU/NZ/UK patterns.
    Actual classification should use LLM context when possible.

    Args:
        phone: E.164 formatted phone number

    Returns:
        "mobile", "office", or "direct"
    """
    if not phone:
        return "office"

    # Clean phone for pattern matching
    clean = phone.replace(" ", "").replace("-", "")

    # Mobile patterns for AU/NZ/UK
    # AU mobile: +61 4xx xxx xxx
    # NZ mobile: +64 2x xxx xxx
    # UK mobile: +44 7xxx xxx xxx
    mobile_patterns = ["+614", "+642", "+447"]

    for pattern in mobile_patterns:
        if clean.startswith(pattern):
            return "mobile"

    # Default to office for non-mobile
    return "office"


def is_valid_e164(phone: str) -> bool:
    """
    Check if phone number is valid E.164 format.

    Args:
        phone: Phone number string

    Returns:
        True if valid E.164 format
    """
    if not phone:
        return False

    # Basic E.164 pattern: +<country><number>
    e164_pattern = r"^\+\d{1,3}\d{6,14}$"
    clean = phone.strip().replace(" ", "").replace("-", "")

    if not re.match(e164_pattern, clean):
        return False

    # Additional validation with phonenumbers
    try:
        parsed = phonenumbers.parse(phone, None)
        country_code = str(parsed.country_code) if parsed.country_code else None
        # Only validate for AU/NZ/UK since that's our target
        if country_code in ["61", "64", "44"]:
            return phonenumbers.is_valid_number(parsed)
        # For other countries, just check the pattern match
        return True
    except Exception:
        return False


def format_for_display(phone: str) -> str:
    """
    Format phone number for display with readability.

    Args:
        phone: E.164 formatted phone number

    Returns:
        Readable format with spaces or hyphens
    """
    if not phone:
        return ""

    clean = phone.replace(" ", "").replace("-", "")

    # AU format: +61 X XXXX XXXX
    if clean.startswith("+61"):
        if len(clean) == 12:  # +61 + 9 digits (most common)
            return f"{clean[:3]} {clean[3:4]} {clean[4:8]} {clean[8:12]}"
        else:
            return phone

    # NZ format: +64 X XXX XXXX or +64 XX XXX XXXX
    if clean.startswith("+64"):
        if len(clean) == 11:  # +64 + 8 digits (landline)
            return f"{clean[:3]} {clean[3:4]} {clean[4:7]} {clean[7:11]}"
        elif len(clean) == 12:  # +64 + 9 digits (landline)
            return f"{clean[:3]} {clean[3:4]} {clean[4:8]} {clean[8:12]}"
        elif len(clean) == 13:  # +64 + 10 digits (mobile - 2-digit prefix)
            return f"{clean[:3]} {clean[3:5]} {clean[5:9]} {clean[9:13]}"
        else:
            return phone

    # UK format: +44 XXXX XXXXXX
    if clean.startswith("+44"):
        if len(clean) == 12:  # +44 + 10 digits
            return f"{clean[:3]} {clean[3:7]} {clean[7:]}"
        elif len(clean) == 13:  # +44 + 11 digits
            return f"{clean[:3]} {clean[3:7]} {clean[7:]}"
        else:
            return phone

    # Default: return as-is with basic formatting
    return phone


def extract_phone_numbers(text: str, default_country: str = "AU") -> List[str]:
    """
    Extract all valid phone numbers from text.

    Args:
        text: Text to search for phone numbers
        default_country: Default country code for parsing

    Returns:
        List of E.164 formatted phone numbers
    """
    if not text:
        return []

    # More comprehensive phone patterns
    patterns = [
        # International format with country code
        r"\+\d{1,3}[\s\-]?[\d\s\-]{3,}[\s\-]?[\d\s\-]{3,}[\s\-]?[\d\s\-]{3,}",
        # AU: 0X XXXX XXXX or 0XXXX XXXXX
        r"0[23478]\s?\d{4}\s?\d{4}",
        r"0[23478]\d{8}",
        # AU mobile: 04XX XXX XXX
        r"04\d{2}\s?\d{3}\s?\d{3}",
        # NZ: 0X XXX XXXX or 0XX XXX XXXX
        r"0[2-9]\s?\d{3}\s?\d{4}",
        r"0[2-9]\d{7,8}",
        # NZ mobile: 02X XXX XXX or 021 XXX XXXX
        r"02[1-9]\s?\d{3}\s?\d{3}",
        r"021\d{2}\s?\d{3}\s?\d{3}",
        # UK: 0XXX XXXXXX or 0XXXX XXXXXX
        r"0\d{3,4}\s?\d{6}",
        r"0\d{9,10}",
        # UK mobile: 07XXX XXXXXX
        r"07\d{3}\s?\d{6}",
        r"07\d{9}",
    ]

    found_numbers = set()

    for pattern in patterns:
        matches = re.finditer(pattern, text, re.IGNORECASE)
        for match in matches:
            phone = match.group()
            # Remove spaces and dashes for normalization
            phone_clean = phone.replace(" ", "").replace("-", "")
            normalized = normalize_to_e164(phone_clean, default_country)
            if normalized:
                found_numbers.add(normalized)

    return sorted(found_numbers)
