"""
Failure classification module for categorizing and providing insights on crawling failures.
"""
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse
import re

from src.logger import get_logger

logger = get_logger(__name__)


@dataclass
class FailureClassification:
    """Classification result for a failed URL."""
    category: str  # dns, connection, timeout, ssl, 403, 404, 429, redirect_loop, maintenance, unknown
    severity: str  # permanent, temporary, unknown
    details: str
    suggested_action: str
    confidence: float = 0.0  # How confident in classification (0-1)


class FailureClassifier:
    """Classify crawling failures to provide actionable insights."""

    # DNS patterns
    DNS_PATTERNS = [
        r"getaddrinfo failed",
        r"name resolution failed",
        r"nodename nor servname provided",
        r"temporary failure in name resolution",
        r"network is unreachable",
        r"NXDOMAIN",
    ]

    # Connection patterns
    CONNECTION_PATTERNS = [
        r"connection refused",
        r"connection reset",
        r"network is down",
        r"no route to host",
        r"host unreachable",
        r"ECONNREFUSED",
        r"ECONNRESET",
    ]

    # Timeout patterns
    TIMEOUT_PATTERNS = [
        r"timeout",
        r"timed out",
        r"exceeded",
        r"operation timed out",
    ]

    # SSL patterns
    SSL_PATTERNS = [
        r"ssl",
        r"certificate",
        r"certificate verify failed",
        r"handshake",
        r"cert",
    ]

    # HTTP status patterns
    STATUS_403_PATTERNS = [r"403", r"forbidden"]
    STATUS_404_PATTERNS = [r"404", r"not found"]
    STATUS_429_PATTERNS = [r"429", r"too many requests"]

    # Redirect patterns
    REDIRECT_PATTERNS = [
        r"redirect loop",
        r"maximum redirects",
        r"redirect chain too long",
    ]

    @staticmethod
    def classify(url: str, error: Exception) -> FailureClassification:
        """
        Analyze failure to provide actionable insights.

        Args:
            url: URL that failed
            error: Exception object

        Returns:
            FailureClassification with category, severity, details, and suggested action
        """
        error_msg = str(error).lower()
        error_type = type(error).__name__
        url_domain = urlparse(url).netloc

        # Check DNS failures
        if FailureClassifier._matches_any(DNS_PATTERNS, error_msg):
            return FailureClassification(
                category="dns",
                severity="permanent",
                details=f"DNS resolution failed for {url_domain}",
                suggested_action="Remove URL from list - domain may not exist",
                confidence=0.9
            )

        # Check connection failures
        if FailureClassifier._matches_any(CONNECTION_PATTERNS, error_msg):
            return FailureClassification(
                category="connection",
                severity="temporary",
                details=f"Connection refused for {url_domain} - server may be down",
                suggested_action="Retry in 1-2 hours - may be temporary outage",
                confidence=0.8
            )

        # Check timeout failures
        if FailureClassifier._matches_any(TIMEOUT_PATTERNS, error_msg):
            return FailureClassification(
                category="timeout",
                severity="temporary",
                details=f"Request timeout after 30 seconds for {url}",
                suggested_action="Retry with longer timeout (60s) or site may be slow",
                confidence=0.95
            )

        # Check SSL errors
        if FailureClassifier._matches_any(SSL_PATTERNS, error_msg):
            return FailureClassification(
                category="ssl",
                severity="temporary",
                details=f"SSL certificate error for {url_domain}",
                suggested_action="Try HTTP instead or check if certificate expired",
                confidence=0.85
            )

        # Check HTTP 403 Forbidden (bot protection)
        if FailureClassifier._matches_any(STATUS_403_PATTERNS, error_msg):
            return FailureClassification(
                category="403_forbidden",
                severity="unknown",
                details=f"403 Forbidden - possible bot protection for {url}",
                suggested_action="Try changing user agent or adding delay (5-10s)",
                confidence=0.8
            )

        # Check HTTP 404 Not Found
        if FailureClassifier._matches_any(STATUS_404_PATTERNS, error_msg):
            return FailureClassification(
                category="404_not_found",
                severity="permanent",
                details=f"404 Not Found for {url}",
                suggested_action="Remove URL - page doesn't exist",
                confidence=1.0
            )

        # Check HTTP 429 Rate Limited
        if FailureClassifier._matches_any(STATUS_429_PATTERNS, error_msg):
            return FailureClassification(
                category="429_rate_limited",
                severity="temporary",
                details=f"429 Too Many Requests for {url}",
                suggested_action="Wait longer between requests (increase DELAY_BETWEEN_REQUESTS)",
                confidence=0.9
            )

        # Check redirect loops
        if FailureClassifier._matches_any(REDIRECT_PATTERNS, error_msg):
            return FailureClassification(
                category="redirect_loop",
                severity="permanent",
                details=f"Redirect loop detected for {url}",
                suggested_action="Remove URL - misconfigured site",
                confidence=0.85
            )

        # Check for maintenance mode (content-based detection)
        if "maintenance" in error_msg or "under maintenance" in error_msg:
            return FailureClassification(
                category="maintenance",
                severity="temporary",
                details=f"Site {url_domain} appears to be in maintenance mode",
                suggested_action="Retry in 24-48 hours",
                confidence=0.8
            )

        # Crawl4AI-specific errors
        if "crawl4ai" in error_msg.lower() or "browser" in error_msg.lower():
            # Check if it's page.goto timeout (crawl4ai specific)
            if "page.goto" in error_msg and "timeout" in error_msg:
                return FailureClassification(
                    category="timeout",
                    severity="temporary",
                    details=f"Page load timeout for {url} - JavaScript heavy or slow site",
                    suggested_action="Try with longer PAGE_TIMEOUT or different strategy",
                    confidence=0.9
                )

        # Unknown error
        return FailureClassification(
            category="unknown",
            severity="unknown",
            details=f"Unknown error: {error_type} - {str(error)}",
            suggested_action="Check logs and verify site accessibility manually",
            confidence=0.5
        )

    @staticmethod
    def _matches_any(patterns: list[str], text: str) -> bool:
        """Check if text matches any of the patterns."""
        return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def classify_batch(urls_errors: list[tuple[str, Exception]]) -> list[FailureClassification]:
    """
    Classify multiple URL failures at once.

    Args:
        urls_errors: List of (url, error) tuples

    Returns:
        List of FailureClassification
    """
    classifications = []
    for url, error in urls_errors:
        classification = FailureClassifier.classify(url, error)
        classifications.append(classification)
    return classifications
