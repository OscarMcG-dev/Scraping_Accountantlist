"""
Progress tracker for displaying real-time crawl progress.
"""
import time
from typing import Dict, Any, Optional
from src.logger import get_logger


__all__ = ["ProgressTracker"]

logger = get_logger(__name__)


class ProgressTracker:
    """
    Tracks and displays progress for batch crawling operations.

    Features:
    - Real-time progress display
    - ETA calculation
    - Speed statistics
    - Periodic progress logging
    """

    def __init__(self, total_urls: int, log_interval: int = 10):
        """
        Initialize progress tracker.

        Args:
            total_urls: Total number of URLs to process
            log_interval: Log progress every N URLs
        """
        self.total_urls = total_urls
        self.log_interval = log_interval
        self.processed = 0
        self.successful = 0
        self.out_of_scope = 0
        self.low_confidence = 0
        self.broken = 0
        self.start_time = time.time()
        self.last_log_time = self.start_time

    def update(self, result_type: str, url: Optional[str] = None):
        """
        Update progress with a processed URL.

        Args:
            result_type: Type of result ("successful", "out_of_scope", "low_confidence", "broken")
            url: Optional URL that was processed
        """
        self.processed += 1

        if result_type == "successful":
            self.successful += 1
        elif result_type == "out_of_scope":
            self.out_of_scope += 1
        elif result_type == "low_confidence":
            self.low_confidence += 1
        elif result_type == "broken":
            self.broken += 1

        # Log progress periodically
        if self.processed % self.log_interval == 0 or self.processed == self.total_urls:
            self.log_progress()

    def log_progress(self):
        """Log current progress."""
        current_time = time.time()
        elapsed = current_time - self.start_time

        progress_percent = (self.processed / self.total_urls * 100) if self.total_urls > 0 else 0

        # Calculate speed (URLs per minute)
        speed = (self.processed / elapsed * 60) if elapsed > 0 else 0

        # Calculate ETA
        if speed > 0:
            remaining = self.total_urls - self.processed
            eta_seconds = remaining / (speed / 60)
            eta_minutes = int(eta_seconds / 60)
            eta_str = f"{eta_minutes}m"
        else:
            eta_str = "Unknown"

        logger.info(
            f"Progress: {self.processed}/{self.total_urls} ({progress_percent:.1f}%) | "
            f"Speed: {speed:.1f} URLs/min | ETA: {eta_str} | "
            f"Success: {self.successful} | Out of Scope: {self.out_of_scope} | "
            f"Low Conf: {self.low_confidence} | Broken: {self.broken}"
        )

    def get_statistics(self) -> Dict[str, Any]:
        """
        Get current statistics.

        Returns:
            Dictionary with progress statistics
        """
        elapsed = time.time() - self.start_time
        speed = (self.processed / elapsed * 60) if elapsed > 0 else 0

        remaining = self.total_urls - self.processed
        if speed > 0:
            eta_seconds = remaining / (speed / 60)
            eta_minutes = int(eta_seconds / 60)
        else:
            eta_minutes = None

        return {
            "total": self.total_urls,
            "processed": self.processed,
            "remaining": remaining,
            "successful": self.successful,
            "out_of_scope": self.out_of_scope,
            "low_confidence": self.low_confidence,
            "broken": self.broken,
            "progress_percent": (self.processed / self.total_urls * 100) if self.total_urls > 0 else 0,
            "elapsed_seconds": elapsed,
            "elapsed_minutes": elapsed / 60,
            "speed_urls_per_minute": speed,
            "eta_minutes": eta_minutes
        }

    def log_summary(self):
        """Log final summary statistics."""
        stats = self.get_statistics()
        logger.info("\n" + "=" * 60)
        logger.info("PROGRESS SUMMARY")
        logger.info("=" * 60)
        logger.info(f"Total URLs: {stats['total']}")
        logger.info(f"Processed: {stats['processed']} ({stats['progress_percent']:.1f}%)")
        logger.info(f"Successful: {stats['successful']}")
        logger.info(f"Out of Scope: {stats['out_of_scope']}")
        logger.info(f"Low Confidence: {stats['low_confidence']}")
        logger.info(f"Broken: {stats['broken']}")
        logger.info(f"Elapsed Time: {stats['elapsed_minutes']:.1f} minutes")
        logger.info(f"Average Speed: {stats['speed_urls_per_minute']:.1f} URLs/min")
        if stats['eta_minutes'] is not None:
            logger.info(f"Final ETA: {stats['eta_minutes']} minutes")
        logger.info("=" * 60)
