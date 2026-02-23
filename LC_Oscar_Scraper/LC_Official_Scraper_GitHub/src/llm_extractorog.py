"""
LLM extractor using OpenRouter API with MiMo-V2-Flash.
"""
import asyncio
import json
from openai import AsyncOpenAI
from typing import Optional, List, Callable, TypeVar, Any


__all__ = ["LLMExtractor", "retry_with_backoff"]

from src.config import Settings
from src.schemas import LLMExtractionResult, DecisionMaker, BusinessSegment
from src.phone_utils import normalize_to_e164
from src.logger import get_logger, log_llm_extraction

logger = get_logger(__name__)

T = TypeVar('T')


async def retry_with_backoff(
    func: Callable[..., Any],
    max_retries: int,
    retry_delay: float,
    *args: Any,
    **kwargs: Any
) -> T:
    """
    Retry a function with exponential backoff.

    Args:
        func: Async function to retry
        max_retries: Maximum number of retry attempts
        retry_delay: Base delay between retries in seconds
        *args: Positional arguments to pass to func
        **kwargs: Keyword arguments to pass to func

    Returns:
        Result from successful function call

    Raises:
        Last exception if all retries fail
    """
    last_exception = None

    for attempt in range(max_retries + 1):  # +1 for initial attempt
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            last_exception = e
            if attempt < max_retries:
                delay = retry_delay * (2 ** attempt)  # Exponential backoff
                logger.warning(
                    f"LLM call failed (attempt {attempt + 1}/{max_retries + 1}): {e}. "
                    f"Retrying in {delay:.1f}s..."
                )
                await asyncio.sleep(delay)
            else:
                logger.error(f"LLM call failed after {max_retries + 1} attempts: {e}")

    if last_exception:
        raise last_exception


class LLMExtractor:
    """Extract structured data using LLM."""

    def __init__(self, settings: Settings, client: Optional[AsyncOpenAI] = None):
        """
        Initialize LLM extractor.

        Args:
            settings: Application settings
            client: Optional pre-configured OpenAI client (for shared connection)
        """
        self.settings = settings

        # Use provided client or create new one (backwards compatible)
        self.client = client or AsyncOpenAI(
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

        # Call LLM with retry logic
        try:
            async def _llm_call() -> Any:
                return await self.client.chat.completions.create(
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

            response = await retry_with_backoff(
                _llm_call,
                self.settings.max_retries,
                self.settings.retry_delay
            )

            # Parse response
            content = response.choices[0].message.content

            data = json.loads(content)

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

                # Return result with error field populated to distinguish from truly empty results
                return LLMExtractionResult(
                    confidence_score=0.0,
                    extraction_error=f"Validation error: {str(validation_error)}"
                )

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse LLM response as JSON for {company_url}: {e}")
            logger.error(f"Raw content that failed: {content}")
            return LLMExtractionResult(
                confidence_score=0.0,
                extraction_error=f"JSON decode error: {str(e)}"
            )
        except Exception as e:
            logger.error(f"LLM extraction failed for {company_url}: {e}")
            logger.error(f"Error type: {type(e).__name__}")
            return LLMExtractionResult(
                confidence_score=0.0,
                extraction_error=f"Extraction error: {type(e).__name__}: {str(e)}"
            )

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

        return f"""You are an expert at analyzing accounting firm websites in Australia, New Zealand, and the UK.

Your task is to extract structured data from accounting firm websites.

TASKS:
1. Extract company information (name, phone, email, location, description)
2. Identify decision makers (people who can make business decisions)
3. Classify business segment into one of these categories:
{segments}
4. Identify if the business is OUT OF SCOPE:
   - Obvious: Non-accounting businesses (e.g., lawn care, retail, construction)
   - Borderline: Accounting software vendors, fintech platforms, advisory-only firms without accounting services
5. Normalize phone numbers to E.164 format (+61 for Australia, +64 for New Zealand, +44 for UK)
6. Extract contact details for decision makers specifically
7. Generate sales-ready insights for cold calling (edited_description field)
8. Assess your confidence in the extraction (0.0 to 1.0)

DECISION MAKER IDENTIFICATION:
- Look for titles like: Partner, Principal, Director, Managing Director, Executive Director,
  Partner in Charge, Senior Partner, Tax Partner, Audit Partner, Principal Accountant
- Be permissive - include anyone with a senior-sounding title
- Exclude: Receptionists, junior staff, assistants, administrative roles
- For each decision maker, create a 2-3 sentence summary of their experience/expertise for sales context (decision_maker_summary field)

BUSINESS SEGMENT CLASSIFICATION:

SALES INTELLIGENCE EXTRACTION (edited_description):
- Create a compelling, sales-ready description for cold calling
- Highlight key value propositions and unique selling points
- Mention service specialties, target industries, or client types served
- Note any mentions of business size (SME, mid-market, enterprise)
- Include geographic focus or service area if specified
- Keep it concise (2-3 sentences max) but impactful for sales conversations
- Focus on what would matter to a potential partner or client

- General Accounting (Including Tax): Full-service accounting firms that provide tax services
- Tax Specialist: Firms focused primarily on tax services (tax advisory, tax compliance)
- Bookkeeping (No Income Tax): Bookkeeping services only, no income tax preparation
- Other Accounting (No Tax): Other accounting services excluding tax (e.g., forensic, payroll)
- Other Tax: Tax-related businesses that aren't traditional accounting firms (e.g., tax software, tax return preparers)

OUT OF SCOPE DETECTION:
Set out_of_scope=true if:
- The business is not an accounting firm
- The business sells accounting software to accountants (rather than providing accounting services)
- The business is clearly outside the target market
Provide a brief reason for why it's out of scope.

PHONE NORMALIZATION:
- Australia: +61 2X XXXX XXXX (landlines), +61 4XX XXX XXX (mobiles)
- New Zealand: +64 X XXX XXXX
- United Kingdom: +44 XXXX XXXXXX
- Format must be: +<country><number> (e.g., +61212345678)
- Only extract mobile numbers if possible for decision makers

OUTPUT FORMAT:
- Return valid JSON with these EXACT field names:
  {{
    "company_name": "string",
    "office_phone": "E.164 format or empty string",
    "office_email": "valid email or empty string",
    "associated_emails": ["email1", "email2"],
    "associated_mobile_numbers": ["E.164 phone1", "E.164 phone2"],
    "associated_info": "string",
    "associated_location": "string",
    "organisational_structure": "string",
    "team": "string",
    "description": "string",
    "edited_description": "Sales-ready insights for cold calling",
    "business_segment": "one of the 5 categories",
    "decision_makers": [{{"name", "title", "phone_office", "phone_mobile", "phone_direct", "email", "linkedin"}}],
    "confidence_score": 0.0 to 1.0,
    "out_of_scope": true/false,
    "out_of_scope_reason": "string if out_of_scope"
  }}
- IMPORTANT: Use flat structure, NO nested objects like "company_details"
- Phone numbers must be in E.164 format
- Email addresses must be valid format
- Confidence score (0.0 to 1.0) reflects overall extraction quality
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
        if len(content) > self.settings.max_content_length_for_llm:
            content = content[:self.settings.max_content_length_for_llm] + "\n\n... (truncated due to length)"

        return f"""Extract and classify this accounting firm website:

URL: {company_url}

Website Content:
{content}

Please extract:
1. company_name: The name of the accounting firm
2. office_phone: Main contact phone in E.164 format
3. office_email: Main contact email
4. associated_location: Office location/address
5. description: Company description
6. edited_description: Sales-ready insights for cold calling (2-3 sentences, value proposition)
7. associated_emails: List of all emails found
8. associated_mobile_numbers: List of all mobile numbers in E.164 format
9. associated_info: Any supplementary information
10. organisational_structure: Summary of partners/directors/staff
11. team: Team information
12. business_segment: One of 5 categories (General Accounting, Tax Specialist, Bookkeeping, Other Accounting, Other Tax)
13. out_of_scope: true/false
14. out_of_scope_reason: Reason if out of scope
15. decision_makers: Array of decision maker objects with fields: name, title, phone_office, phone_mobile, phone_direct, email, linkedin, decision_maker_summary
16. confidence_score: 0.0 to 1.0

CRITICAL: Return flat JSON with field names exactly as listed above. Do NOT use nested objects like "company_details".

Be thorough but accurate. If information is missing, leave fields empty rather than guessing.
Focus on finding decision makers' mobile phone numbers when available.

Return your response as valid JSON matching the schema.
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
