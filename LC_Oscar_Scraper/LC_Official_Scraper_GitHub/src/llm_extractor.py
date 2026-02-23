"""
LLM extractor using OpenRouter API with MiMo-V2-Flash.
Optimized for cold-calling hooks and minimal marketing fluff.
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
    """Extract structured data using LLM with a focus on sales intelligence."""

    def __init__(self, settings: Settings, client: Optional[AsyncOpenAI] = None):
        """
        Initialize LLM extractor.
        """
        self.settings = settings

        # Use provided client or create new one
        self.client = client or AsyncOpenAI(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
        )

    async def extract(self, company_url: str, main_content: str, bio_content: List[str]) -> LLMExtractionResult:
        """
        Extract structured data using LLM.
        """
        # Combine all content
        all_content = self._combine_content(main_content, bio_content)

        # Build prompt
        prompt = self._build_extraction_prompt(company_url, all_content)

        # Call LLM with retry logic
        try:
            async def _llm_call() -> Any:
                # Corrected to .create() for modern OpenAI Python SDK
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
                # Return result with error field populated
                return LLMExtractionResult(
                    confidence_score=0.0,
                    extraction_error=f"Validation error: {str(validation_error)}"
                )

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse LLM response as JSON for {company_url}: {e}")
            return LLMExtractionResult(
                confidence_score=0.0,
                extraction_error=f"JSON decode error: {str(e)}"
            )
        except Exception as e:
            logger.error(f"LLM extraction failed for {company_url}: {e}")
            return LLMExtractionResult(
                confidence_score=0.0,
                extraction_error=f"Extraction error: {type(e).__name__}: {str(e)}"
            )

    def _combine_content(self, main_content: str, bio_content: List[str]) -> str:
        """
        Combine main and bio page content.
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
        """
        segments = "\n".join(f"- {seg}" for seg in BusinessSegment.all())

        return f"""You are an expert at extracting high-value B2B lead intelligence from accounting and advisory firm websites in Australia, New Zealand, and the UK.

Your goal is to provide data that a cold-caller can use immediately to establish credibility and relevance.

CRITICAL INSTRUCTIONS FOR CONTENT PRUNING:

1. DECISION MAKER SUMMARY (decision_maker_summary):
   - REMOVE ALL MARKETING SPIN: Do not use words like "passionate", "dedicated", "strategic", "experienced", "expert", or "focuses on success".
   - ONLY EXTRACT FACTS: 
     - Specific industry niches (e.g., "Medical Specialists", "Crypto tax", "Agriculture", "SMSF").
     - Professional Background (e.g., "Ex-Deloitte", "Chartered Accountant", "Registered Tax Agent").
     - Key Skills/Services (e.g., "R&D Tax Credits", "Business Structuring", "M&A Advisory").
   - FORMAT: Use short, punchy sentence fragments. 
   - GOOD EXAMPLE: "Ex-PwC. Niche: High-net-worth medical groups and property developers. Specializes in R&D tax."
   - BAD EXAMPLE: "John is a highly experienced partner who is passionate about helping his clients achieve their financial dreams."

2. SALES INSIGHTS (edited_description):
   - Purpose: A 2-sentence "hook" for a cold call.
   - Mention: Recent awards, physical office locations, specific technology (e.g. "Xero Platinum Partner"), or professional memberships (e.g. "CAANZ").
   - GOOD EXAMPLE: "Xero Platinum Partner based in Melbourne with a heavy focus on the E-commerce sector. Recently won 'Advisory Firm of the Year 2024'."

TASKS:
1. Extract company name, main phone, and primary email.
2. Identify Decision Makers (Partners, Directors, Principals, Owners).
3. Classify business segment:
{segments}
4. Extract Firmographics: Identify technologies used (e.g., Xero, MYOB, Dext) and professional memberships (e.g., CAANZ, CPA, IPA). Note: If a Decision Maker bio mentions a credential, apply it to the firm level too. Determine if they are a "Registered Tax Agent".
5. Detect if Out of Scope (Non-accounting businesses or software-only vendors).
6. Normalize all phone numbers to E.164 format (+61... for AU).

OUTPUT FORMAT:
- Return valid JSON with these EXACT field names:
  {{
    "company_name": "string",
    "office_phone": "E.164 format",
    "office_email": "string",
    "associated_emails": ["email1", "email2"],
    "associated_mobile_numbers": ["phone1", "phone2"],
    "tech_stack": ["tech1", "tech2"],
    "professional_memberships": ["membership1", "membership2"],
    "is_registered_tax_agent": true/false,
    "associated_info": "string",
    "associated_location": "string",
    "organisational_structure": "summary of leadership hierarchy",
    "team": "summary of staff size",
    "description": "Short factual summary",
    "edited_description": "2-sentence sales hook",
    "business_segment": "one of the 5 categories",
    "decision_makers": [{{"name", "title", "phone_office", "phone_mobile", "phone_direct", "email", "linkedin", "decision_maker_summary"}}],
    "confidence_score": 0.0 to 1.0,
    "out_of_scope": true/false,
    "out_of_scope_reason": "string"
  }}
"""

    def _build_extraction_prompt(self, company_url: str, content: str) -> str:
        """
        Build the extraction prompt.
        """
        # Truncate content if too long
        if len(content) > self.settings.max_content_length_for_llm:
            content = content[:self.settings.max_content_length_for_llm] + "\n\n... (truncated due to length)"

        return f"""Extract and classify the accounting firm at {company_url}.

Website Content:
{content}

Please extract:
1. company_name
2. office_phone: Provide digits only in E.164 format (e.g., +61...).
3. edited_description: A 2-sentence cold-call "hook". Focus on specialties and "social proof" (awards/partnerships).
4. decision_makers: Array of key people. Include direct mobile numbers if found in bio text.
5. decision_maker_summary: Concisely state FACTS only. (e.g., "Niche: Medical", "Background: Big 4"). 

STRICT: NO marketing fluff. NO generic adjectives. If no specific niche or background is found, leave the summary empty.
"""

    def normalize_llm_output(self, result: LLMExtractionResult, default_country: str = "AU") -> LLMExtractionResult:
        """
        Post-process phone numbers.
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
