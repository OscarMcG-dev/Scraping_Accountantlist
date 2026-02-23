"""
Checkpoint manager for saving and resuming crawl state.
Allows the scraper to resume from the last successful crawl after interruption.
"""
import json
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime
from src.logger import get_logger


__all__ = ["CheckpointManager"]

logger = get_logger(__name__)


class CheckpointManager:
    """
    Manages checkpointing for crawl operations.

    Features:
    - Save crawl state after each URL processed
    - Resume from last checkpoint
    - Track URLs processed, skipped, and remaining
    - Store progress statistics
    """

    def __init__(self, state_dir: str = "data/state", checkpoint_interval: int = 10):
        """
        Initialize checkpoint manager.

        Args:
            state_dir: Directory to store checkpoint files
            checkpoint_interval: Save checkpoint after every N URLs processed
        """
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_interval = checkpoint_interval
        self.current_checkpoint: Dict[str, Any] = {}
        self.checkpoint_file: Optional[Path] = None

    def initialize_session(self, urls: List[str], checkpoint_name: str = "batch") -> Path:
        """
        Initialize a new checkpoint session.

        Args:
            urls: List of URLs to process
            checkpoint_name: Name for this checkpoint batch

        Returns:
            Path to checkpoint file
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.checkpoint_file = self.state_dir / f"{checkpoint_name}_{timestamp}.json"

        self.current_checkpoint = {
            "session_id": timestamp,
            "checkpoint_name": checkpoint_name,
            "total_urls": len(urls),
            "urls": urls,
            "processed_urls": [],
            "successful_urls": [],
            "out_of_scope_urls": [],
            "low_confidence_urls": [],
            "broken_urls": [],
            "skipped_urls": [],
            "results": [],
            "out_of_scope_records": [],
            "low_confidence_records": [],
            "broken_urls_list": [],
            "current_index": 0,
            "start_time": datetime.now().isoformat(),
            "last_update": datetime.now().isoformat(),
            "completed": False
        }

        self._save_checkpoint()
        logger.info(f"Initialized checkpoint session: {self.checkpoint_file}")
        logger.info(f"Total URLs to process: {len(urls)}")

        return self.checkpoint_file

    def record_url_processed(
        self,
        url: str,
        result_type: str,
        data: Optional[Dict[str, Any]] = None
    ):
        """
        Record that a URL has been processed.

        Args:
            url: URL that was processed
            result_type: Type of result ("successful", "out_of_scope", "low_confidence", "broken")
            data: Optional data associated with the result
        """
        self.current_checkpoint["processed_urls"].append(url)
        self.current_checkpoint["current_index"] += 1
        self.current_checkpoint["last_update"] = datetime.now().isoformat()

        if result_type == "successful":
            self.current_checkpoint["successful_urls"].append(url)
            if data:
                self.current_checkpoint["results"].append(data)
        elif result_type == "out_of_scope":
            self.current_checkpoint["out_of_scope_urls"].append(url)
            if data:
                self.current_checkpoint["out_of_scope_records"].append(data)
        elif result_type == "low_confidence":
            self.current_checkpoint["low_confidence_urls"].append(url)
            if data:
                self.current_checkpoint["low_confidence_records"].append(data)
        elif result_type == "broken":
            self.current_checkpoint["broken_urls"].append(url)

        # Save checkpoint periodically
        if (self.current_checkpoint["current_index"] % self.checkpoint_interval == 0 or
                self.current_checkpoint["current_index"] == self.current_checkpoint["total_urls"]):
            self._save_checkpoint()

    def record_url_skipped(self, url: str, reason: str):
        """
        Record that a URL was skipped (duplicate, already processed, etc.).

        Args:
            url: URL that was skipped
            reason: Reason for skipping
        """
        self.current_checkpoint["skipped_urls"].append({
            "url": url,
            "reason": reason
        })
        logger.info(f"Skipped URL: {url} - {reason}")

    def get_progress(self) -> Dict[str, Any]:
        """
        Get current progress statistics.

        Returns:
            Dictionary with progress information
        """
        total = self.current_checkpoint.get("total_urls", 0)
        processed = len(self.current_checkpoint.get("processed_urls", []))
        remaining = total - processed

        return {
            "total_urls": total,
            "processed": processed,
            "remaining": remaining,
            "successful": len(self.current_checkpoint.get("successful_urls", [])),
            "out_of_scope": len(self.current_checkpoint.get("out_of_scope_urls", [])),
            "low_confidence": len(self.current_checkpoint.get("low_confidence_urls", [])),
            "broken": len(self.current_checkpoint.get("broken_urls", [])),
            "progress_percent": (processed / total * 100) if total > 0 else 0,
            "start_time": self.current_checkpoint.get("start_time"),
            "last_update": self.current_checkpoint.get("last_update")
        }

    def get_remaining_urls(self) -> List[str]:
        """
        Get list of URLs that still need to be processed.

        Returns:
            List of remaining URLs
        """
        processed = set(self.current_checkpoint.get("processed_urls", []))
        all_urls = self.current_checkpoint.get("urls", [])
        return [url for url in all_urls if url not in processed]

    def get_accumulated_results(self) -> Dict[str, Any]:
        """
        Get all accumulated results from the checkpoint.

        Returns:
            Dictionary with all accumulated results
        """
        return {
            "successful": self.current_checkpoint.get("results", []),
            "out_of_scope": self.current_checkpoint.get("out_of_scope_records", []),
            "low_confidence": self.current_checkpoint.get("low_confidence_records", []),
            "broken": self.current_checkpoint.get("broken_urls", [])
        }

    def mark_completed(self):
        """Mark the checkpoint session as completed."""
        self.current_checkpoint["completed"] = True
        self.current_checkpoint["end_time"] = datetime.now().isoformat()
        self._save_checkpoint()
        logger.info(f"Checkpoint session completed: {self.checkpoint_file}")

    def _save_checkpoint(self):
        """Save current checkpoint to file."""
        if self.checkpoint_file:
            with open(self.checkpoint_file, 'w') as f:
                json.dump(self.current_checkpoint, f, indent=2, default=str)
            logger.debug(f"Checkpoint saved: {self.checkpoint_file}")

    @staticmethod
    def load_checkpoint(checkpoint_path: str) -> Optional['CheckpointManager']:
        """
        Load an existing checkpoint.

        Args:
            checkpoint_path: Path to checkpoint file

        Returns:
            CheckpointManager instance with loaded data, or None if file doesn't exist
        """
        path = Path(checkpoint_path)
        if not path.exists():
            logger.warning(f"Checkpoint file not found: {checkpoint_path}")
            return None

        try:
            with open(path, 'r') as f:
                data = json.load(f)

            manager = CheckpointManager(state_dir=str(path.parent))
            manager.checkpoint_file = path
            manager.current_checkpoint = data

            logger.info(f"Loaded checkpoint: {checkpoint_path}")
            progress = manager.get_progress()
            logger.info(f"Progress: {progress['processed']}/{progress['total_urls']} URLs processed ({progress['progress_percent']:.1f}%)")

            return manager
        except Exception as e:
            logger.error(f"Failed to load checkpoint {checkpoint_path}: {e}")
            return None

    @staticmethod
    def list_checkpoints(state_dir: str = "data/state") -> List[str]:
        """
        List all available checkpoints.

        Args:
            state_dir: Directory containing checkpoint files

        Returns:
            List of checkpoint file paths
        """
        state_path = Path(state_dir)
        if not state_path.exists():
            return []

        checkpoints = sorted(state_path.glob("*.json"), reverse=True)
        return [str(c) for c in checkpoints]

    @staticmethod
    def get_latest_checkpoint(state_dir: str = "data/state") -> Optional[str]:
        """
        Get the most recent checkpoint file.

        Args:
            state_dir: Directory containing checkpoint files

        Returns:
            Path to latest checkpoint, or None if no checkpoints exist
        """
        checkpoints = CheckpointManager.list_checkpoints(state_dir)
        return checkpoints[0] if checkpoints else None
