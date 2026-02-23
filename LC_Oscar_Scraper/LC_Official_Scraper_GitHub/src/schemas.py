"""
Pydantic schemas for data validation and structure.
Modified to be permissive with phone/email formats to prevent extraction loss.
"""
from typing import Optional, List
from pydantic import BaseModel, Field, field_validator
import re
import logging

# Get logger for validation warnings
logger = logging.getLogger(__name__)

__all__ = [
    "BusinessSegment",
    "DecisionMaker",
    "CompanyData",
    "LLMExtractionResult",
    "OutOfScopeRecord",
    "LowConfidenceRecord",
]

class BusinessSegment:
    """Valid business segment options."""
    GENERAL_ACCOUNTING = "General Accounting (Including Tax)"
    TAX_SPECIALIST = "Tax Specialist"
    BOOKKEEPING = "Bookkeeping (No Income Tax)"
    OTHER_ACCOUNTING = "Other Accounting (No Tax)"
    OTHER_TAX = "Other Tax"

    @classmethod
    def all(cls) -> List[str]:
        return [
            cls.GENERAL_ACCOUNTING,
            cls.TAX_SPECIALIST,
            cls.BOOKKEEPING,
            cls.OTHER_ACCOUNTING,
            cls.OTHER_TAX,
        ]

class DecisionMaker(BaseModel):
    """Decision maker information."""
    name: Optional[str] = None
    title: Optional[str] = None
    decision_maker_summary: str = Field(default="", description="Summary of decision maker experience/expertise")
    phone_office: Optional[str] = None
    phone_mobile: Optional[str] = None
    phone_direct: Optional[str] = None
    email: Optional[str] = None
    linkedin: Optional[str] = None

    @field_validator("phone_office", "phone_mobile", "phone_direct", mode="before")
    @classmethod
    def validate_permissive_phone(cls, v: Optional[str]) -> Optional[str]:
        """Clean phone numbers and warn instead of failing."""
        if not v or str(v).strip() == "":
            return None
        
        # Remove common formatting characters
        cleaned = re.sub(r"[^\d+]", "", str(v))
        
        # Basic check for a phone-like string
        if not re.match(r"^\+?\d{7,15}$", cleaned):
            logger.warning(f"Likely invalid phone format detected: {v}")
            # We return it anyway so we don't lose potential lead data
            return cleaned
        return cleaned

    @field_validator("email", mode="before")
    @classmethod
    def validate_permissive_email(cls, v: Optional[str]) -> Optional[str]:
        """Warn on suspicious emails but don't crash."""
        if not v or str(v).strip() == "":
            return None
        email = str(v).strip()
        if "@" not in email:
            logger.warning(f"Invalid email detected: {v}")
        return email

class CompanyData(BaseModel):
    """Complete company data for CRM import."""
    company_name: Optional[str] = None
    company_url: str
    office_phone: Optional[str] = None
    office_email: Optional[str] = None
    associated_emails: List[str] = Field(default_factory=list)
    associated_mobile_numbers: List[str] = Field(default_factory=list)
    associated_info: str = ""
    associated_location: Optional[str] = None
    organisational_structure: Optional[str] = Field(default="")
    team: Optional[str] = Field(default="")
    description: str = ""
    edited_description: str = Field(default="", description="Sales-ready insights")
    business_segment: str = Field(default="General Accounting (Including Tax)")
    decision_makers: List[DecisionMaker] = Field(default_factory=list)
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    tech_stack: List[str] = Field(default_factory=list)
    professional_memberships: List[str] = Field(default_factory=list)
    is_registered_tax_agent: bool = False


    @field_validator("office_phone", mode="before")
    @classmethod
    def validate_office_phone(cls, v: Optional[str]) -> Optional[str]:
        if not v: return None
        return re.sub(r"[^\d+]", "", str(v))

class LLMExtractionResult(BaseModel):
    """Schema for LLM extraction output."""
    company_name: Optional[str] = None
    office_phone: Optional[str] = None
    office_email: Optional[str] = None
    associated_emails: List[str] = Field(default_factory=list)
    associated_mobile_numbers: List[str] = Field(default_factory=list)
    associated_info: str = ""
    associated_location: Optional[str] = None
    organisational_structure: Optional[str] = Field(default="")
    team: Optional[str] = Field(default="")
    description: str = ""
    edited_description: str = Field(default="", description="Sales-ready insights")
    business_segment: str = "General Accounting (Including Tax)"
    decision_makers: List[DecisionMaker] = Field(default_factory=list)
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    out_of_scope: bool = False
    out_of_scope_reason: Optional[str] = None
    extraction_error: Optional[str] = Field(default=None)
    tech_stack: List[str] = Field(default_factory=list)
    professional_memberships: List[str] = Field(default_factory=list)
    is_registered_tax_agent: bool = False


    @field_validator("out_of_scope", mode="before")
    @classmethod
    def flatten_out_of_scope(cls, v):
        if isinstance(v, dict):
            return v.get("is_out_of_scope", v.get("out_of_scope", False))
        return v

class OutOfScopeRecord(BaseModel):
    company_url: str
    company_name: Optional[str] = None
    reason: str = ""
    confidence_score: float = 0.0

class LowConfidenceRecord(BaseModel):
    company_url: str
    company_name: Optional[str] = None
    confidence_score: float
    reason: str = ""
