"""
Manifest registry — the curated mapping of forensic artifacts to CASE/UCO terms.
Exact key lookup only. No fuzzy matching. No duck typing.
The manifest is data, not code. New artifacts = new JSON entries, zero code changes.
"""

import json
from pathlib import Path
from typing import Optional


class ManifestRegistry:
    """Loads and queries the artifact manifest."""

    def __init__(self, manifest_path: Optional[str] = None):
        if manifest_path is None:
            manifest_path = str(
                Path(__file__).parent / "data" / "ioi_artifact_manifest.json"
            )

        with open(manifest_path, "r") as f:
            self._manifest: dict = json.load(f)

        # Build normalized lookup (case-insensitive, strip common prefixes)
        self._lookup: dict[str, str] = {}
        for key in self._manifest:
            self._lookup[key.lower()] = key
            # Also index without common prefixes
            for prefix in ("windows", "$", "uco-observable:"):
                stripped = key.lower().removeprefix(prefix.lower())
                if stripped and stripped != key.lower():
                    self._lookup[stripped] = key

    def resolve(self, artifact_name: str) -> Optional[dict]:
        """
        Exact resolve of an artifact name.
        Returns manifest entry or None.
        """
        key = artifact_name.strip().lower()
        if key in self._lookup:
            canonical = self._lookup[key]
            entry = self._manifest[canonical].copy()
            entry["canonical_name"] = canonical
            return entry
        return None

    def list_all(self, category: Optional[str] = None, tier: Optional[str] = None) -> list[dict]:
        """List manifest entries, optionally filtered."""
        results = []
        for name, entry in self._manifest.items():
            if category and entry.get("category") != category:
                continue
            if tier and entry.get("tier") != tier:
                continue
            item = entry.copy()
            item["name"] = name
            results.append(item)
        return results

    @property
    def artifact_count(self) -> int:
        return len(self._manifest)

    @property
    def official_count(self) -> int:
        return sum(1 for v in self._manifest.values() if v.get("tier") == "official")

    @property
    def extension_count(self) -> int:
        return sum(1 for v in self._manifest.values() if v.get("tier") == "extension")
