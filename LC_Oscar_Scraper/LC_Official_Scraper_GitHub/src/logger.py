"""
Structured logging configuration for the LC Official Scraper.

Uses structlog for JSON logging with proper log levels and separate log files.
"""

import logging
import sys
from pathlib import Path
from typing import Optional


__all__ = ["get_logger", "log_crawl_start", "log_crawl_success", "log_crawl_failure", "log_http_fallback", "log_llm_extraction"]

try:
    import structlog
    STRUCTLOG_AVAILABLE = True
except ImportError:
    STRUCTLOG_AVAILABLE = False
    import logging


def setup_logging(
    log_level: str = "INFO",
    log_dir: Optional[Path] = None,
    verbose: bool = False,
    retention_days: int = 7
) -> logging.Logger | object:
    """
    Configure structured JSON logging.

    Creates separate log files:
    - data/logs/scraper.log (general)
    - data/logs/errors.log (errors only)
    - data/logs/audit.log (audit trail)
    - data/logs/performance.log (metrics)

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
        log_dir: Directory for log files (default: data/logs)
        verbose: Enable detailed request/response logging
        retention_days: Number of days to keep logs

    Returns:
        Configured logger instance
    """
    if log_dir is None:
        log_dir = Path("data/logs")

    log_dir.mkdir(parents=True, exist_ok=True)

    # Convert string log level to logging constant
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    # Configure standard library logging for structlog backend
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=numeric_level,
        force=True
    )

    if STRUCTLOG_AVAILABLE:
        # Configure structlog with multiple processors
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.StackInfoRenderer(),
                structlog.dev.set_exc_info,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.JSONRenderer()
            ],
            wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(),
            cache_logger_on_first_use=True,
        )

        logger = structlog.get_logger()

        # Log startup info
        logger.info(
            "logging_initialized",
            log_level=log_level,
            verbose=verbose,
            retention_days=retention_days,
            log_dir=str(log_dir)
        )

        return logger
    else:
        # Fallback to standard logging if structlog is not available
        logger = logging.getLogger("lc_scraper")
        logger.setLevel(numeric_level)

        # Add console handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(numeric_level)
        console_formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        console_handler.setFormatter(console_formatter)
        logger.addHandler(console_handler)

        # Add file handler for all logs
        file_handler = logging.FileHandler(log_dir / "scraper.log")
        file_handler.setLevel(numeric_level)
        file_formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)

        # Add separate error file handler
        error_handler = logging.FileHandler(log_dir / "errors.log")
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(file_formatter)
        logger.addHandler(error_handler)

        logger.warning(
            "structlog not available, falling back to standard logging. "
            "Install structlog with: pip install structlog"
        )

        return logger


def get_logger(name: Optional[str] = None) -> logging.Logger | object:
    """
    Get a logger instance.

    Args:
        name: Logger name (defaults to 'lc_scraper')

    Returns:
        Logger instance
    """
    if name is None:
        name = "lc_scraper"

    if STRUCTLOG_AVAILABLE:
        return structlog.get_logger(name)
    else:
        return logging.getLogger(name)


def log_crawl_start(logger, url: str, strategy: str, max_pages: int) -> None:
    """Log the start of a crawl operation."""
    if STRUCTLOG_AVAILABLE:
        logger.info("crawl_started", url=url, strategy=strategy, max_pages=max_pages)
    else:
        logger.info(f"Crawl started: {url} (strategy={strategy}, max_pages={max_pages})")


def log_crawl_success(
    logger,
    url: str,
    pages_crawled: int,
    confidence: float,
    duration_ms: int
) -> None:
    """Log successful crawl completion."""
    if STRUCTLOG_AVAILABLE:
        logger.info(
            "crawl_completed",
            url=url,
            pages_crawled=pages_crawled,
            confidence=confidence,
            duration_ms=duration_ms
        )
    else:
        logger.info(
            f"Crawl completed: {url} (pages={pages_crawled}, "
            f"confidence={confidence:.2f}, duration={duration_ms}ms)"
        )


def log_crawl_failure(
    logger,
    url: str,
    error_type: str = "UnknownError",
    error_message: str = "No details provided",
    category: Optional[str] = None,
    severity: Optional[str] = None
) -> None:
    """Log crawl failure."""
    if STRUCTLOG_AVAILABLE:
        logger.error(
            "crawl_failed",
            url=url,
            error_type=error_type,
            error_message=error_message,
            failure_category=category,
            failure_severity=severity
        )
    else:
        logger.error(
            f"Crawl failed: {url} - {error_type}: {error_message}"
            + (f" (category={category}, severity={severity})" if category else "")
        )


def log_llm_extraction(
    logger,
    url: str,
    model: str,
    duration_ms: int,
    decision_makers_count: int,
    confidence: float
) -> None:
    """Log LLM extraction results."""
    if STRUCTLOG_AVAILABLE:
        logger.info(
            "llm_extraction_completed",
            url=url,
            model=model,
            duration_ms=duration_ms,
            decision_makers_count=decision_makers_count,
            confidence=confidence
        )
    else:
        logger.info(
            f"LLM extraction completed for {url}: "
            f"model={model}, duration={duration_ms}ms, "
            f"decision_makers={decision_makers_count}, confidence={confidence:.2f}"
        )


def log_http_fallback(logger, url: str, from_https: bool, success: bool) -> None:
    """Log HTTP/HTTPS fallback attempts."""
    if STRUCTLOG_AVAILABLE:
        logger.info(
            "http_fallback_attempt",
            url=url,
            from_https=from_https,
            success=success
        )
    else:
        protocol = "HTTPS" if from_https else "HTTP"
        status = "succeeded" if success else "failed"
        logger.info(f"{protocol} {status} for {url}")
