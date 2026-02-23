"""
LLM extractor using OpenRouter API with MiMo-V2-Flash.
"""
import json
from openai import OpenAI
from typing import Optional, List

from src.config import Settings
from src.schemas import LLMExtractionResult, DecisionMaker, BusinessSegment
from src.phone_utils import normalize_to_e164
from src.logger import get_logger, log_llm_extraction

logger = get_logger(__name__)


class LLMExtractor:
    """Extract structured data using LLM."""

    def __init__(self, settings: Settings):
        """
        Initialize LLM extractor.

        Args:
            settings: Application settings
        """
        self.settings = settings

        # OpenRouter client
        self.client = OpenAI(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
        )

    async def extract(self, company_url: str, main_content: str, bio_content: List[str]) -> LLMExtractionResult:
        """
        Extract structured data using LLM.

        Args:
            company_url: URL of the website
            main_content: Main page markdown content
            bio_content: List of bio page markdown content

        Returns:
            LLMExtractionResult with structured data
        """
        # Combine all content
        all_content = self._combine_content(main_content, bio_content)

        # Build prompt
        prompt = self._build_extraction_prompt(company_url, all_content)

        # Call LLM
        try:
            response = self.client.chat.completions.create(
                model=self.settings.openrouter_model,
                messages=[
                    {"role": "system", "content": self._get_system_prompt()},
                    {"role": "user", "content": prompt},
                ],
                temperature=self.settings.llm_temperature,
                max_tokens=4000,
                response_format={"type": "json_object"},
                # MiMo-V2-Flash reasoning setting
                extra_body={
                    "reasoning": {"enabled": self.settings.reasoning_enabled}
                } if self.settings.reasoning_enabled else None,
            )

            # Parse response
            content = response.choices[0].message.content

            # Debug: Log raw LLM response
            logger.debug(f"Raw LLM response for {company_url}:\n{content[:1000]}")

            data = json.loads(content)

            # Debug: Log parsed data
            logger.debug(f"Parsed data keys: {list(data.keys())}")
            if data.get("company_name"):
                logger.debug(f"Company name found: {data['company_name']}")
            if data.get("confidence_score"):
                logger.debug(f"Confidence score: {data['confidence_score']}")

            # Try to create LLMExtractionResult with detailed error handling
            try:
                result = LLMExtractionResult(**data)
                logger.info(f"Successfully validated extraction result for {company_url}")
                return result
            except Exception as validation_error:
                logger.error(f"Pydantic validation failed for {company_url}: {validation_error}")
                logger.error(f"Validation error type: {type(validation_error).__name__}")

                # For Pydantic validation errors, show which fields failed
                if hasattr(validation_error, 'errors'):
                    for error in validation_error.errors():
                        logger.error(f"Field error: {error['loc'][0] if error['loc'] else 'unknown'} - {error['msg']}")

                return LLMExtractionResult(confidence_score=0.0)

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse LLM response as JSON for {company_url}: {e}")
            logger.error(f"Raw content that failed: {content}")
            return LLMExtractionResult(confidence_score=0.0)
        except Exception as e:
            logger.error(f"LLM extraction failed for {company_url}: {e}")
            logger.error(f"Error type: {type(e).__name__}")
            return LLMExtractionResult(confidence_score=0.0)

    def _combine_content(self, main_content: str, bio_content: List[str]) -> str:
        """
        Combine main and bio page content.

        Args:
            main_content: Main page markdown
            bio_content: List of bio page markdown

        Returns:
            Combined content string
        """
        result = f"=== Main Page ===\n{main_content}\n\n"

        if bio_content:
            result += f"=== Bio/Team Pages ({len(bio_content)}) ===\n"
            for i, bio in enumerate(bio_content, 1):
                result += f"\n--- Bio Page {i} ---\n{bio}\n"

        return result

    def _get_system_prompt(self) -> str:
        """
        Get the system prompt for the LLM.

        Returns:
            System prompt string
        """
        segments = "\n".join(f"- {seg}" for seg in BusinessSegment.all())

        return f"""You are a data analyst extracting factual firmographic data from accounting firm websites in Australia, New Zealand, and the UK. Your output will be read by sales reps during cold calls — it must be instantly useful.

CRITICAL RULES:
- Report ONLY facts stated on the website. Never invent or embellish.
- Strip ALL marketing language. No adjectives like 'trusted', 'leading', 'passionate', 'dedicated', 'expert', 'boutique', 'client-focused'.
- If information is not on the site, leave the field as an empty string.

TASKS:
1. Extract company information (name, phone, email, location, factual description)
2. Identify decision makers (senior staff who make business decisions)
3. Classify business segment into one of these categories:
{segments}
4. Normalize phone numbers to E.164 format (+61 AU, +64 NZ, +44 UK)
5. Assess extraction confidence (0.0 to 1.0)

DESCRIPTION: Write a factual summary. State services offered, location, and who they serve. Do not copy taglines or mission statements.

EDITED_DESCRIPTION: The rep reads this while the phone rings. Use pipe-separated bullet points:
  - Suburb/city and state
  - Core services (tax, SMSF, audit, bookkeeping, BAS, etc.)
  - Software stack (Xero, MYOB, QuickBooks, Sage)
  - Team size if stated
  - Client types or industry niches
  - Professional body memberships (CAANZ, CPA, NTAA, IPA)
Example: 'Dee Why NSW | Tax, SMSF, audit, BAS | Xero, MYOB | ~8 staff | Medical & trades clients | CAANZ, CPA members'

DECISION MAKERS:
- Look for: Partner, Principal, Director, Managing Director, Senior Partner, Tax Partner, Audit Partner, Founder, Owner, Manager
- Be permissive with senior titles
- Exclude: Receptionists, admin staff, juniors, graduates

DECISION_MAKER_SUMMARY: For each DM, write factual bullet points:
  - Qualifications (CA, CPA, NTAA fellow, BBus, etc.)
  - Years at firm or in industry
  - Specific responsibilities (e.g. 'heads SMSF division')
  - Prior firms (e.g. 'ex-PwC')
  - Industry specializations
Use short factual fragments separated by '. '. No flowing prose.
Example: 'CA, CPA. 12 yrs at firm. Heads tax compliance. Ex-Deloitte. Specialises in medical practices.'

ASSOCIATED_INFO: List factual details — professional memberships, tax agent reg number, software stack (incl. add-ons like Dext, Hubdoc), industry niches.

BUSINESS SEGMENTS:
- General Accounting (Including Tax): Full-service firms that provide tax services
- Tax Specialist: Primarily tax services (advisory, compliance)
- Bookkeeping (No Income Tax): Bookkeeping only, no income tax
- Other Accounting (No Tax): Other accounting excluding tax (forensic, payroll)
- Other Tax: Tax-related but not traditional accounting (tax software, tax preparers)

OUT OF SCOPE: Set out_of_scope=true if:
- Not an accounting firm
- Sells accounting software (rather than providing accounting services)
- Clearly outside the target market

PHONE NORMALIZATION:
- Format: +<country><number> (e.g., +61212345678)
- Prioritise mobile numbers for decision makers

OUTPUT FORMAT:
Return valid JSON with these EXACT field names:
  {{
    "company_name": "string",
    "office_phone": "E.164 format or empty string",
    "office_email": "valid email or empty string",
    "associated_emails": ["email1", "email2"],
    "associated_mobile_numbers": ["E.164 phone1"],
    "associated_info": "string",
    "associated_location": "string",
    "organisational_structure": "string",
    "team": "string",
    "description": "string",
    "edited_description": "pipe-separated firmographic brief",
    "business_segment": "one of the 5 categories",
    "decision_makers": [{{"name", "title", "decision_maker_summary", "phone_office", "phone_mobile", "phone_direct", "email", "linkedin"}}],
    "confidence_score": 0.0 to 1.0,
    "out_of_scope": true/false,
    "out_of_scope_reason": "string if out_of_scope"
  }}
- Use flat structure, NO nested objects
- Phone numbers in E.164 format
- Extract up to {self.settings.max_decision_makers} decision makers
"""

    def _build_extraction_prompt(self, company_url: str, content: str) -> str:
        """
        Build the extraction prompt.

        Args:
            company_url: URL being scraped
            content: Combined website content

        Returns:
            Prompt string
        """
        # Truncate content if too long (leave room for prompt + response)
        max_content_length = 25000  # Leave room for system prompt and response
        if len(content) > max_content_length:
            content = content[:max_content_length] + "\n\n... (truncated due to length)"

        return f"""Extract factual firmographic data from this accounting firm website.

URL: {company_url}

Website Content:
{content}

Extract all fields per the schema. Report only facts from the website — no marketing language, no embellishment. If information is missing, leave fields as empty strings. Prioritise finding decision makers and their mobile numbers.

CRITICAL: Return flat JSON with field names exactly as listed in the schema. Do NOT use nested objects.
"""

    def normalize_llm_output(self, result: LLMExtractionResult, default_country: str = "AU") -> LLMExtractionResult:
        """
        Normalize phone numbers in LLM output.

        Args:
            result: LLM extraction result
            default_country: Default country for phone parsing

        Returns:
            Normalized LLMExtractionResult
        """
        data = result.model_dump()

        # Normalize office phone
        if data.get("office_phone"):
            normalized = normalize_to_e164(data["office_phone"], default_country)
            data["office_phone"] = normalized if normalized else None

        # Normalize mobile numbers
        normalized_mobiles = []
        for phone in data.get("associated_mobile_numbers", []):
            normalized = normalize_to_e164(phone, default_country)
            if normalized:
                normalized_mobiles.append(normalized)
        data["associated_mobile_numbers"] = normalized_mobiles

        # Normalize decision maker phones
        for dm in data.get("decision_makers", []):
            for phone_field in ["phone_office", "phone_mobile", "phone_direct"]:
                if dm.get(phone_field):
                    normalized = normalize_to_e164(dm[phone_field], default_country)
                    dm[phone_field] = normalized if normalized else None

        return LLMExtractionResult(**data)
