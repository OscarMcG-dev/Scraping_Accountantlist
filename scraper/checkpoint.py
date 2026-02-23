"""Simple JSON checkpoint for resumable scraping across phases."""
import json
from pathlib import Path
from typing import Any, Optional
from datetime import datetime


class Checkpoint:
    """Persist and resume scraping state via a JSON file."""

    def __init__(self, path: str = "data/state/checkpoint.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            with open(self.path, "r") as f:
                return json.load(f)
        return {"phase1": {}, "phase2": {}, "meta": {}}

    def save(self) -> None:
        self._data["meta"]["last_saved"] = datetime.utcnow().isoformat()
        with open(self.path, "w") as f:
            json.dump(self._data, f, indent=2, default=str)

    # -- Phase 1: directory scraping --

    def get_completed_detail_urls(self) -> set:
        return set(self._data.get("phase1", {}).get("completed_urls", []))

    def mark_detail_url_done(self, url: str) -> None:
        completed = self._data.setdefault("phase1", {}).setdefault("completed_urls", [])
        if url not in completed:
            completed.append(url)

    def get_directory_listings(self) -> list:
        return self._data.get("phase1", {}).get("listings", [])

    def save_directory_listings(self, listings: list) -> None:
        self._data.setdefault("phase1", {})["listings"] = listings
        self.save()

    # -- Phase 2: website enrichment --

    def get_enriched_urls(self) -> set:
        return set(self._data.get("phase2", {}).get("enriched_urls", []))

    def mark_enriched(self, url: str) -> None:
        enriched = self._data.setdefault("phase2", {}).setdefault("enriched_urls", [])
        if url not in enriched:
            enriched.append(url)

    def save_enrichment(self, url: str, data: dict) -> None:
        enrichments = self._data.setdefault("phase2", {}).setdefault("enrichments", {})
        enrichments[url] = data
        self.mark_enriched(url)
        self.save()

    def get_enrichment(self, url: str) -> Optional[dict]:
        return self._data.get("phase2", {}).get("enrichments", {}).get(url)

    def get_all_enrichments(self) -> dict:
        return self._data.get("phase2", {}).get("enrichments", {})
