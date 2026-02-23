"""
Pydantic schemas for data validation and structure.
"""
from typing import Optional, List, Literal
from pydantic import BaseModel, Field, field_validator
import re


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
    decision_maker_summary: str = Field(default="", description="Summary of decision maker experience/expertise for sales context")
    phone_office: Optional[str] = None
    phone_mobile: Optional[str] = None
    phone_direct: Optional[str] = None
    email: Optional[str] = None
    linkedin: Optional[str] = None

    @field_validator("phone_office", "phone_mobile", "phone_direct", mode="before")
    @classmethod
    def validate_e164(cls, v: Optional[str]) -> Optional[str]:
        """Validate E.164 phone format."""
        # Handle None or empty string
        if v is None or v == "" or (isinstance(v, str) and v.strip() == ""):
            return None
        # E.164 format: +<country><number>
        # Allow spaces and hyphens for readability in validation
        phone = v.strip().replace(" ", "").replace("-", "")
        if not re.match(r"\+\d{1,3}\d{6,14}$", phone):
            raise ValueError(f"Invalid E.164 format: {v}")
        return v

    @field_validator("email", mode="before")
    @classmethod
    def validate_email(cls, v: Optional[str]) -> Optional[str]:
        """Validate email format."""
        # Handle None or empty string
        if v is None or v == "" or (isinstance(v, str) and v.strip() == ""):
            return None
        if not re.match(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", v):
            raise ValueError(f"Invalid email format: {v}")
        return v


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
    organisational_structure: str = ""
    team: str = ""
    description: str = ""
    edited_description: str = Field(default="", description="Sales-ready insights for cold calling")
    business_segment: str = Field(default="General Accounting (Including Tax)")
    decision_makers: List[DecisionMaker] = Field(default_factory=list)
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator("office_phone", mode="before")
    @classmethod
    def validate_office_phone(cls, v: Optional[str]) -> Optional[str]:
        """Validate office phone E.164 format."""
        # Handle None or empty string
        if v is None or v == "" or (isinstance(v, str) and v.strip() == ""):
            return None
        phone = v.strip().replace(" ", "").replace("-", "")
        if not re.match(r"\+\d{1,3}\d{6,14}$", phone):
            raise ValueError(f"Invalid E.164 format: {v}")
        return v

    @field_validator("office_email", mode="before")
    @classmethod
    def validate_office_email(cls, v: Optional[str]) -> Optional[str]:
        """Validate office email format."""
        # Handle None or empty string
        if v is None or v == "" or (isinstance(v, str) and v.strip() == ""):
            return None
        if not re.match(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", v):
            raise ValueError(f"Invalid email format: {v}")
        return v

    @field_validator("business_segment", mode="before")
    @classmethod
    def validate_business_segment(cls, v: str) -> str:
        """Validate business segment is one of valid options."""
        if v not in BusinessSegment.all():
            raise ValueError(f"Invalid business segment: {v}")
        return v


class LLMExtractionResult(BaseModel):
    """Schema for LLM extraction output."""
    company_name: Optional[str] = None
    office_phone: Optional[str] = None
    office_email: Optional[str] = None
    associated_emails: List[str] = Field(default_factory=list)
    associated_mobile_numbers: List[str] = Field(default_factory=list)
    associated_info: str = ""
    associated_location: Optional[str] = None
    organisational_structure: str = ""
    team: str = ""
    description: str = ""
    edited_description: str = Field(default="", description="Sales-ready insights for cold calling")
    business_segment: str = "General Accounting (Including Tax)"
    decision_makers: List[DecisionMaker] = Field(default_factory=list)
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    out_of_scope: bool = False
    out_of_scope_reason: Optional[str] = None

    @field_validator("organisational_structure", mode="before")
    @classmethod
    def flatten_organisational_structure(cls, v):
        """Flatten dict organisational_structure to string."""
        if isinstance(v, dict):
            # Convert dict to string format
            parts = []
            for key, value in v.items():
                if isinstance(value, (int, float)):
                    parts.append(f"{key.replace('_', ' ').title()}: {value}")
                elif isinstance(value, dict):
                    parts.append(f"{key.replace('_', ' ').title()}: {value}")
                else:
                    parts.append(f"{key.replace('_', ' ').title()}: {value}")
            return "; ".join(parts) if parts else ""
        return v if v else ""

    @field_validator("out_of_scope", mode="before")
    @classmethod
    def flatten_out_of_scope(cls, v):
        """Handle dict out_of_scope field."""
        if isinstance(v, dict):
            return v.get("is_out_of_scope", v.get("out_of_scope", False))
        return v

    @field_validator("out_of_scope_reason", mode="before")
    @classmethod
    def extract_out_of_scope_reason(cls, v):
        """Extract reason from dict out_of_scope_reason field."""
        if isinstance(v, dict):
            return v.get("reason", v.get("explanation", str(v)))
        return v


class OutOfScopeRecord(BaseModel):
    """Record for out-of-scope URLs."""
    company_url: str
    company_name: Optional[str] = None
    reason: str = ""
    confidence_score: float = 0.0


class LowConfidenceRecord(BaseModel):
    """Record for low-confidence extractions."""
    company_url: str
    company_name: Optional[str] = None
    confidence_score: float
    reason: str = ""
