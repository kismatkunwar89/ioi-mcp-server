"""
Manifest registry — loads IoI Framework registry.json and exposes
artifact metadata: facet names, field types, graph IRI helpers.

Priority order for registry.json location:
  1. IOI_REGISTRY_PATH env var (path to live IoI-Framework/registry.json)
  2. IOI_EXT_TTL env var directory (look for registry.json alongside it)
  3. Bundled data/ioi_registry.json (ships with MCP, kept in sync with framework)
"""

import json
import os
from pathlib import Path
from typing import Optional


KB_NAMESPACE       = "https://ioi-framework.github.io/kb/"
GRAPH_IRI_PATTERN  = "https://ioi-framework.github.io/cases/{case_id}/graphs/{graph_segment}"


def _find_registry_path() -> str:
    """Find registry.json — live framework path or bundled fallback."""
    # 1. Explicit env var pointing at IoI-Framework/registry.json
    env_path = os.environ.get("IOI_REGISTRY_PATH")
    if env_path and Path(env_path).exists():
        return env_path

    # 2. Look alongside IOI_EXT_TTL (common setup)
    ext_ttl = os.environ.get("IOI_EXT_TTL")
    if ext_ttl:
        candidate = Path(ext_ttl).parent / "registry.json"
        if candidate.exists():
            return str(candidate)

    # 3. Bundled copy
    bundled = Path(__file__).parent / "data" / "ioi_registry.json"
    if bundled.exists():
        return str(bundled)

    raise FileNotFoundError(
        "registry.json not found. Set IOI_REGISTRY_PATH to point at "
        "IoI-Framework/registry.json, or ensure data/ioi_registry.json exists."
    )


class ManifestRegistry:
    """Loads and queries the IoI artifact registry."""

    def __init__(self, registry_path: Optional[str] = None):
        if registry_path is None:
            registry_path = _find_registry_path()

        with open(registry_path) as f:
            self._data: dict = json.load(f)

        self._artifacts: dict = self._data.get("artifacts", {})
        self._registry_path = registry_path

        # Case-insensitive + prefix-stripped lookup index
        self._lookup: dict[str, str] = {}
        for key in self._artifacts:
            self._lookup[key.lower()] = key
            for prefix in ("windows", "$", "uco-observable:"):
                stripped = key.lower().removeprefix(prefix.lower())
                if stripped and stripped != key.lower():
                    self._lookup[stripped] = key

    # ── Lookup ───────────────────────────────────────────────────

    def resolve(self, artifact_name: str) -> Optional[dict]:
        """Return registry entry for artifact_name or None."""
        key = artifact_name.strip().lower()
        canonical = self._lookup.get(key)
        if not canonical:
            return None
        entry = self._artifacts[canonical].copy()
        entry["canonical_name"] = canonical
        return entry

    def list_all(self) -> list[dict]:
        """Return all artifact entries."""
        return [
            {"canonical_name": k, **v}
            for k, v in self._artifacts.items()
        ]

    # ── IRI helpers ──────────────────────────────────────────────

    def make_graph_iri(self, case_id: str, artifact_name: str) -> str:
        """
        Build a named graph IRI for a given case + artifact.
        Graph segment is read from registry 'graph_segment' field so
        artifact_type -> segment mappings (e.g. office_xml -> 'office')
        are driven by the registry, not hardcoded here.

        e.g. make_graph_iri("AF-004", "mft")        → .../graphs/mft
             make_graph_iri("AF-012", "office_xml")  → .../graphs/office
        """
        entry   = self.resolve(artifact_name)
        segment = entry.get("graph_segment", artifact_name.lower()) if entry else artifact_name.lower()
        return GRAPH_IRI_PATTERN.format(case_id=case_id, graph_segment=segment)

    @property
    def kb_namespace(self) -> str:
        return self._data.get("kb_namespace", KB_NAMESPACE)

    @property
    def graph_iri_pattern(self) -> str:
        return self._data.get("graph_iri_pattern", GRAPH_IRI_PATTERN)

    # ── Field type helpers ───────────────────────────────────────

    def get_field_types(self, artifact_name: str) -> Optional[dict]:
        """Return {"integer": [...], "datetime": [...], "boolean": [...], "string": [...]}"""
        entry = self.resolve(artifact_name)
        return entry.get("field_types") if entry else None

    def get_file_facet_columns(self, artifact_name: str) -> list[str]:
        """Return column names that map to observable:FileFacet properties."""
        entry = self.resolve(artifact_name)
        return entry.get("file_facet_columns", []) if entry else []

    def get_facet(self, artifact_name: str) -> Optional[str]:
        """Return the ioi-ext Facet IRI, e.g. "ioi-ext:MftFacet"."""
        entry = self.resolve(artifact_name)
        return entry.get("facet") if entry else None

    # ── Version / sync check ─────────────────────────────────────

    def check_sync(self) -> Optional[str]:
        """
        M-11b: If IOI_REGISTRY_PATH points at a live framework registry,
        compare artifact counts and warn if bundled copy is behind.
        Returns a warning string or None.
        """
        live_path = os.environ.get("IOI_REGISTRY_PATH")
        if not live_path or not Path(live_path).exists():
            return None
        try:
            with open(live_path) as f:
                live = json.load(f)
            live_count = len(live.get("artifacts", {}))
            bundled_count = self.artifact_count
            if live_count > bundled_count:
                return (
                    f"[ioi-mcp] Registry update available: {live_count} artifacts in "
                    f"IoI-Framework vs {bundled_count} bundled. "
                    f"Set IOI_REGISTRY_PATH to use the live registry."
                )
        except Exception:
            pass
        return None

    # ── Counts ───────────────────────────────────────────────────

    @property
    def artifact_count(self) -> int:
        return len(self._artifacts)

    @property
    def schema_version(self) -> str:
        return self._data.get("schema_version", "unknown")
