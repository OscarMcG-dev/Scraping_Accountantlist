"""
Intelligent link analysis using LLM to discover relevant pages.
Replaces brittle hard-coded URL patterns with semantic understanding.
"""
import asyncio
from typing import List, Dict, Any, Callable, TypeVar, Optional
from openai import AsyncOpenAI
import json


__all__ = ["LinkAnalyzer", "retry_with_backoff"]

from src.config import Settings
from src.logger import get_logger

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


class LinkAnalyzer:
    """Analyze and classify website links using LLM intelligence."""

    def __init__(self, settings: Settings, client: Optional[AsyncOpenAI] = None):
        """
        Initialize link analyzer.

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

    async def analyze_links(
        self,
        company_url: str,
        internal_links: List[Dict[str, Any]],
        navigation_structure: str = ""
    ) -> Dict[str, Any]:
        """
        Analyze internal links to identify which ones to crawl for team/decision maker info.

        Args:
            company_url: Base URL of the website
            internal_links: List of internal links with metadata
            navigation_structure: Additional context about navigation elements

        Returns:
            Dictionary with:
            - team_links: List of URLs likely containing team/people info
            - about_links: List of URLs with company information
            - service_links: List of URLs describing services (optional)
            - contact_links: List of URLs with contact information
            - priority_order: List of URLs in crawling priority order
        """
        if not internal_links:
            return {
                "team_links": [],
                "about_links": [],
                "service_links": [],
                "contact_links": [],
                "priority_order": []
            }

        # Prepare link data for LLM
        link_data = self._prepare_link_data(internal_links)

        # Build prompt
        prompt = self._build_link_analysis_prompt(company_url, link_data, navigation_structure)

        # Call LLM with retry logic
        try:
            async def _llm_call() -> Any:
                return await self.client.chat.completions.create(
                    model=self.settings.openrouter_model,
                    messages=[
                        {"role": "system", "content": self._get_system_prompt()},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.0,  # Deterministic classification
                    max_tokens=2000,
                    response_format={"type": "json_object"},
                )

            response = await retry_with_backoff(
                _llm_call,
                self.settings.max_retries,
                self.settings.retry_delay
            )

            content = response.choices[0].message.content
            result = json.loads(content)

            logger.info(f"Link analysis complete: {len(result.get('team_links', []))} team links found")
            return result

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse LLM response: {e}")
            return self._fallback_analysis(internal_links)
        except Exception as e:
            logger.error(f"Link analysis failed: {e}")
            return self._fallback_analysis(internal_links)

    def _prepare_link_data(self, internal_links: List[Dict[str, Any]]) -> str:
        """
        Convert link data to format suitable for LLM analysis.

        Args:
            internal_links: List of link dictionaries

        Returns:
            Formatted string of links
        """
        # Limit links to avoid overwhelming LLM
        links_to_analyze = internal_links[:self.settings.max_links_for_llm_analysis]

        formatted_links = []
        for i, link in enumerate(links_to_analyze, 1):
            # Handle both dict and string formats
            if isinstance(link, dict):
                url = link.get("href", link.get("url", ""))
                text = link.get("text", link.get("content", "")).strip()
            else:
                url = str(link)
                text = ""

            # Clean up text
            text = text.replace("\n", " ").replace("\t", " ")

            formatted_links.append(
                f"{i}. URL: {url}\n   Text: {text if text else '(no text)'}"
            )

        return "\n".join(formatted_links)

    def _get_system_prompt(self) -> str:
        """
        Get system prompt for link analysis.

        Returns:
            System prompt string
        """
        return """You are an expert at analyzing website navigation and link structures for accounting firms in Australia, New Zealand, and the UK.

Your task is to intelligently identify which internal links on a website are most likely to contain:
1. Team/People/Staff information (decision makers, partners, directors)
2. Company/about information
3. Service descriptions
4. Contact information

KEY INSIGHTS:
- Don't rely on URL patterns alone - accounting websites often have non-standard URLs
- Look at LINK TEXT and CONTEXT more than URL structure
- Navigation bars, footers, and sidebars are prime locations for team links
- Be flexible and creative - team info might be called "Our People", "Staff", "Partners", "Directors", "Meet the Team", etc.
- Prioritize pages that likely contain individual staff bios or profiles
- URLs like /john-smith or /staff/sarah-jones are valuable
- Even unconventional URLs can be relevant if the link text suggests it

CLASSIFICATION CRITERIA:

TEAM_LINKS (highest priority):
- Any page with "Our People", "Team", "Partners", "Leadership", "About Us", or "Contact".
- IMPORTANT: If you see a list of individual names in the link text, ALWAYS include them.
- We want to find mobile numbers and direct emails, so prioritize bio pages, or pages that may link to information about decision makers. 

ABOUT_LINKS:
- Company history, mission, values, overview pages
- Look for: about us, our story, who we are, company overview, history

SERVICE_LINKS:
- Descriptions of services offered
- Look for: services, what we do, our services, expertise

CONTACT_LINKS:
- Contact forms, addresses, general contact info
- Look for: contact us, get in touch, our offices

PRIORITY_ORDER:
- Return URLs in order you would crawl them
- Team links should come first (to capture decision makers)
- Then about/service/contact pages for context

RESPONSE FORMAT:
Return valid JSON with these exact keys:
{
    "team_links": ["url1", "url2", ...],
    "about_links": ["url1", "url2", ...],
    "service_links": ["url1", "url2", ...],
    "contact_links": ["url1", "url2", ...],
    "priority_order": ["url1", "url2", ...],
    "reasoning": "Brief explanation of your analysis"
}

If no links match a category, return empty list.
Be thorough but accurate - don't include irrelevant links.
"""

    def _build_link_analysis_prompt(
        self,
        company_url: str,
        link_data: str,
        navigation_structure: str
    ) -> str:
        """
        Build the link analysis prompt.

        Args:
            company_url: Base URL
            link_data: Formatted link information
            navigation_structure: Additional navigation context

        Returns:
            Prompt string
        """
        prompt = f"""Analyze the internal links from this accounting firm website and identify which pages we should crawl for comprehensive information.

Company URL: {company_url}

INTERNAL LINKS:
{link_data}
"""

        if navigation_structure:
            prompt += f"""
NAVIGATION CONTEXT:
{navigation_structure}
"""

        prompt += """

Please classify these links into categories and determine the optimal crawling order.

Focus especially on finding links that contain:
1. Individual team member profiles or bios
2. Team/staff directories or listings
3. Any pages with decision maker information (partners, directors)

Remember: Look at link TEXT and CONTEXT, not just URL patterns. Accounting websites often have unusual URL structures.

Return your classification as JSON.
"""
        return prompt

    def _fallback_analysis(self, internal_links: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Fallback analysis when LLM fails - use simple pattern matching as backup.

        Args:
            internal_links: List of link dictionaries

        Returns:
            Classification dictionary
        """
        logger.warning("Using fallback link analysis (pattern matching)")

        team_keywords = ["team", "people", "staff", "partner", "director", "our", "meet", "profile"]
        about_keywords = ["about", "company", "story", "mission", "overview", "history"]
        service_keywords = ["service", "what we do", "expertise"]
        contact_keywords = ["contact", "reach", "get in touch"]

        team_links = []
        about_links = []
        service_links = []
        contact_links = []

        for link in internal_links:
            if isinstance(link, dict):
                url = link.get("href", link.get("url", ""))
                text = link.get("text", link.get("content", "")).lower()
            else:
                url = str(link)
                text = ""

            combined = f"{url} {text}".lower()

            # Classify
            if any(kw in combined for kw in team_keywords):
                if url not in team_links:
                    team_links.append(url)
            elif any(kw in combined for kw in about_keywords):
                if url not in about_links:
                    about_links.append(url)
            elif any(kw in combined for kw in service_keywords):
                if url not in service_links:
                    service_links.append(url)
            elif any(kw in combined for kw in contact_keywords):
                if url not in contact_links:
                    contact_links.append(url)

        # Priority: team > about > services > contact
        priority_order = team_links + about_links + service_links + contact_links

        return {
            "team_links": team_links,
            "about_links": about_links,
            "service_links": service_links,
            "contact_links": contact_links,
            "priority_order": priority_order,
            "reasoning": "Fallback pattern-based analysis"
        }
