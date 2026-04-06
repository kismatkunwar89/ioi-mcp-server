"""
Graph builder — assembles complete, valid CASE/UCO JSON-LD documents.
Case-agnostic: builds from ontology properties + extension facets.
Every output has valid IRIs, UUIDs, and proper @context.
"""

import csv
import json
import uuid
from pathlib import Path
from typing import Optional

from ioi_mcp.ontology_loader import OntologyLoader
from ioi_mcp.type_inferencer import analyze_csv
from ioi_mcp.extension_gen import (
    generate_turtle_patch,
    generate_facet_jsonld,
    get_extension_property_list,
    IOI_EXT_PREFIX,
    IOI_EXT_NS,
)


# Standard @context — always included
BASE_CONTEXT = {
    "kb": "http://example.org/kb/",
    "uco-core": "https://ontology.unifiedcyberontology.org/uco/core/",
    "uco-observable": "https://ontology.unifiedcyberontology.org/uco/observable/",
    "uco-types": "https://ontology.unifiedcyberontology.org/uco/types/",
    "uco-vocabulary": "https://ontology.unifiedcyberontology.org/uco/vocabulary/",
    "uco-action": "https://ontology.unifiedcyberontology.org/uco/action/",
    "uco-tool": "https://ontology.unifiedcyberontology.org/uco/tool/",
    "uco-identity": "https://ontology.unifiedcyberontology.org/uco/identity/",
    "uco-location": "https://ontology.unifiedcyberontology.org/uco/location/",
    "case-investigation": "https://ontology.caseontology.org/case/investigation/",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
}


def _make_id(type_hint: str) -> str:
    """Generate a CASE-compliant @id: kb:<type>-<UUIDv4>."""
    return f"kb:{type_hint}-{uuid.uuid4()}"


def _build_official_facet(
    facet_name: str,
    ontology: OntologyLoader,
    sample_row: Optional[dict] = None,
    column_mapping: Optional[dict] = None,
) -> dict:
    """
    Build a JSON-LD facet instance from SHACL properties.
    Populates with sample_row values if column_mapping is provided.

    Args:
        facet_name: e.g., 'WindowsPrefetchFacet'
        ontology: loaded ontology
        sample_row: optional {column_name: value}
        column_mapping: optional {property_local_name: column_name}
    """
    props = ontology.get_facet_properties(facet_name)
    if not props:
        return {
            "@id": _make_id(facet_name.lower()),
            "@type": f"uco-observable:{facet_name}",
        }

    facet = {
        "@id": _make_id(facet_name.lower()),
        "@type": f"uco-observable:{facet_name}",
    }

    for prop in props:
        prefixed = prop["name"]
        xsd_range = prop["range"]
        is_array = prop["is_array"]

        # Try to get value from sample row
        value = None
        if sample_row and column_mapping and prop["local_name"] in column_mapping:
            col_name = column_mapping[prop["local_name"]]
            value = sample_row.get(col_name)

        # Build the value based on type
        # Skip properties with no value (produces clean, SHACL-compliant output)
        if prop["range_type"] == "object":
            if value:
                if is_array:
                    facet[prefixed] = [{"@id": str(value)}]
                else:
                    facet[prefixed] = {"@id": str(value)}
            # else: omit entirely — empty object refs violate SHACL
        elif xsd_range in ("integer", "int"):
            int_val = _safe_int(value)
            if value is not None:
                typed_val = {"@type": "xsd:integer", "@value": int_val}
                facet[prefixed] = [typed_val] if is_array else typed_val
        elif xsd_range in ("decimal", "float", "double"):
            if value is not None:
                typed_val = {"@type": "xsd:decimal", "@value": _safe_float(value)}
                facet[prefixed] = [typed_val] if is_array else typed_val
        elif xsd_range in ("dateTime", "date"):
            val_str = str(value) if value else None
            if val_str:
                typed_val = {"@type": f"xsd:{xsd_range}", "@value": val_str}
                facet[prefixed] = [typed_val] if is_array else typed_val
        elif xsd_range == "boolean":
            if value is not None:
                typed_val = {"@type": "xsd:boolean", "@value": _safe_bool(value)}
                facet[prefixed] = [typed_val] if is_array else typed_val
        elif xsd_range == "hexBinary":
            if value:
                typed_val = {"@type": "xsd:hexBinary", "@value": str(value)}
                facet[prefixed] = [typed_val] if is_array else typed_val
        else:
            # String — only include if value is non-empty
            if value:
                if is_array:
                    facet[prefixed] = [str(value)]
                else:
                    facet[prefixed] = str(value)

    return facet


def _safe_int(val) -> int:
    if val is None:
        return 0
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0


def _safe_float(val) -> float:
    if val is None:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _safe_bool(val) -> bool:
    if val is None:
        return False
    return str(val).lower() in ("true", "1", "yes")


class GraphBuilder:
    """Builds complete CASE/UCO JSON-LD graphs."""

    def __init__(self, ontology: OntologyLoader, manifest=None):
        self.ontology = ontology
        self.manifest = manifest

    def build_from_manifest(
        self,
        artifact_name: str,
        sample_row: Optional[dict] = None,
        column_mapping: Optional[dict] = None,
    ) -> Optional[dict]:
        """
        Build a JSON-LD graph for a known artifact (manifest hit).
        Returns complete graph with @context and @graph, or None if not found.
        """
        entry = self.manifest.resolve(artifact_name) if self.manifest else None
        if not entry:
            return None

        context = dict(BASE_CONTEXT)
        if entry.get("tier") == "extension":
            context[IOI_EXT_PREFIX] = IOI_EXT_NS

        # Build the main observable object
        uco_class = entry["uco_class"]
        facet_names = entry["uco_facets"]

        obs_id = _make_id("observable-object")
        facets = []

        for facet_name in facet_names:
            if facet_name.startswith("ioi-ext:"):
                # Extension facet — empty template
                facets.append({
                    "@id": _make_id(facet_name.split(":")[1].lower()),
                    "@type": facet_name,
                })
            else:
                # Official facet — build from SHACL
                facet = _build_official_facet(
                    facet_name, self.ontology, sample_row, column_mapping
                )
                facets.append(facet)

        graph_node = {
            "@id": obs_id,
            "@type": uco_class,
            "uco-core:hasFacet": facets,
        }

        return {
            "@context": context,
            "@graph": [graph_node],
        }

    def build_with_mapping(
        self,
        artifact_name: str,
        csv_path: str,
        column_mapping: dict[str, str],
        description: Optional[str] = None,
    ) -> dict:
        """
        Build JSON-LD using an explicit column mapping from the LLM.

        Args:
            artifact_name: e.g., 'Prefetch'
            csv_path: path to the CSV
            column_mapping: {csv_column: 'uco-observable:propertyName'}
                Columns not in mapping become ioi-ext: extension properties.
            description: optional artifact description

        Returns dict with jsonld, turtle_patch (if needed), validation info.
        """
        columns = analyze_csv(csv_path)
        sample_row = self._read_first_row(csv_path)
        entry = self.manifest.resolve(artifact_name) if self.manifest else None

        context = dict(BASE_CONTEXT)
        uco_class = entry["uco_class"] if entry else "uco-observable:ObservableObject"
        facet_names = entry.get("uco_facets", []) if entry else []

        # Split columns into mapped (official) and unmapped (extension)
        mapped_cols = {}    # {property_local_name: csv_column}
        unmapped_cols = []   # columns that become ioi-ext

        # Reverse the mapping: uco-observable:timesExecuted -> timesExecuted
        prop_to_csv = {}  # {property_local_name: csv_column_name}
        for csv_col, uco_prop in column_mapping.items():
            # Extract local name from prefixed form
            if ":" in uco_prop:
                local = uco_prop.split(":", 1)[1]
            else:
                local = uco_prop
            prop_to_csv[local] = csv_col

        # Find which columns are not in the mapping
        mapped_csv_cols = set(column_mapping.keys())
        for col in columns:
            if col["column_name"] not in mapped_csv_cols:
                unmapped_cols.append(col)

        # Build official facets with the mapping
        facets = []
        has_extension = bool(unmapped_cols)

        for facet_name in facet_names:
            if facet_name.startswith("ioi-ext:"):
                continue  # Will be handled by extension path
            facet = _build_official_facet(
                facet_name, self.ontology, sample_row, prop_to_csv
            )
            facets.append(facet)

        # If no facet_names in manifest, add standard facets
        if not facet_names:
            facets.append(_build_official_facet("FileFacet", self.ontology, sample_row, prop_to_csv))
            facets.append(_build_official_facet("ContentDataFacet", self.ontology, sample_row, prop_to_csv))

        # Build extension facet for unmapped columns
        turtle_patch = None
        if unmapped_cols:
            has_extension = True
            context[IOI_EXT_PREFIX] = IOI_EXT_NS

            ext_facet = generate_facet_jsonld(artifact_name, unmapped_cols, sample_row)
            facets.insert(0, ext_facet)  # Extension facet first

            turtle_patch = generate_turtle_patch(artifact_name, unmapped_cols, description)

        if entry and entry.get("tier") == "extension":
            context[IOI_EXT_PREFIX] = IOI_EXT_NS

        obs_id = _make_id("observable-object")
        graph_node = {
            "@id": obs_id,
            "@type": uco_class,
            "uco-core:hasFacet": facets,
        }

        jsonld = {
            "@context": context,
            "@graph": [graph_node],
        }

        return {
            "jsonld": jsonld,
            "turtle_patch": turtle_patch,
            "column_analysis": columns,
            "tier": "official" if not has_extension else "hybrid",
            "artifact_name": artifact_name,
            "mapped_columns": list(column_mapping.keys()),
            "unmapped_columns": [c["column_name"] for c in unmapped_cols],
            "extension_properties": (
                get_extension_property_list(artifact_name, unmapped_cols)
                if unmapped_cols else []
            ),
        }

    def build_from_csv(
        self,
        artifact_name: str,
        csv_path: str,
        description: Optional[str] = None,
    ) -> dict:
        """
        Build a complete JSON-LD graph from a CSV file.
        For known artifacts: maps CSV columns to official SHACL properties.
        For unknown artifacts: generates ioi-ext extension terms.

        Returns:
        {
            "jsonld": {...},           # Complete JSON-LD graph
            "turtle_patch": "...",     # Turtle string (only for extensions)
            "column_analysis": [...],  # Type inference results
            "tier": "official"|"extension",
            "artifact_name": "...",
        }
        """
        # Analyze CSV
        columns = analyze_csv(csv_path)

        # Read first data row for populating example
        sample_row = self._read_first_row(csv_path)

        # Check manifest (if available)
        entry = self.manifest.resolve(artifact_name) if self.manifest else None

        if entry and entry.get("tier") == "official":
            # Known official artifact
            return self._build_official_from_csv(
                artifact_name, entry, columns, sample_row
            )
        elif entry and entry.get("tier") == "extension":
            # Known extension artifact (in manifest but needs ioi-ext)
            return self._build_extension_from_csv(
                artifact_name, entry, columns, sample_row, description
            )
        else:
            # Completely unknown artifact
            return self._build_extension_from_csv(
                artifact_name, None, columns, sample_row, description
            )

    def _build_official_from_csv(
        self,
        artifact_name: str,
        entry: dict,
        columns: list[dict],
        sample_row: Optional[dict],
    ) -> dict:
        """Build graph for an official CASE/UCO artifact."""
        # For official artifacts, we try to map CSV columns to SHACL properties
        # by matching column names to property local names
        facet_names = entry["uco_facets"]

        # Gather all available properties across all facets
        all_props = {}
        for facet_name in facet_names:
            if not facet_name.startswith("ioi-ext:"):
                for prop in self.ontology.get_facet_properties(facet_name):
                    all_props[prop["local_name"].lower()] = prop

        # Build column mapping: csv_column -> property_local_name
        column_mapping = {}
        unmapped_columns = []
        for col in columns:
            col_lower = col["clean_name"].lower()
            if col_lower in all_props:
                column_mapping[all_props[col_lower]["local_name"]] = col["column_name"]
            else:
                unmapped_columns.append(col)

        # Build the graph
        jsonld = self.build_from_manifest(artifact_name, sample_row, column_mapping)

        return {
            "jsonld": jsonld,
            "turtle_patch": None,
            "column_analysis": columns,
            "tier": "official",
            "artifact_name": artifact_name,
            "mapped_columns": len(column_mapping),
            "unmapped_columns": [c["column_name"] for c in unmapped_columns],
        }

    def _build_extension_from_csv(
        self,
        artifact_name: str,
        entry: Optional[dict],
        columns: list[dict],
        sample_row: Optional[dict],
        description: Optional[str] = None,
    ) -> dict:
        """Build graph for an extension artifact (ioi-ext)."""
        context = dict(BASE_CONTEXT)
        context[IOI_EXT_PREFIX] = IOI_EXT_NS

        # Generate extension facet
        ext_facet = generate_facet_jsonld(artifact_name, columns, sample_row)

        # Build the observable object
        uco_class = "uco-observable:ObservableObject"
        if entry:
            uco_class = entry.get("uco_class", uco_class)

        obs_id = _make_id("observable-object")
        facets = [ext_facet]

        # Add standard facets if this is a file-based artifact
        official_facets = entry.get("uco_facets", []) if entry else []
        for facet_name in official_facets:
            if not facet_name.startswith("ioi-ext:"):
                facet = _build_official_facet(facet_name, self.ontology, sample_row)
                facets.append(facet)

        # If no official facets specified, add FileFacet + ContentDataFacet as defaults
        if not any(f for f in official_facets if not f.startswith("ioi-ext:")):
            facets.append(_build_official_facet("FileFacet", self.ontology))
            facets.append(_build_official_facet("ContentDataFacet", self.ontology))

        graph_node = {
            "@id": obs_id,
            "@type": uco_class,
            "uco-core:hasFacet": facets,
        }

        jsonld = {
            "@context": context,
            "@graph": [graph_node],
        }

        # Generate Turtle patch
        turtle = generate_turtle_patch(artifact_name, columns, description)

        return {
            "jsonld": jsonld,
            "turtle_patch": turtle,
            "column_analysis": columns,
            "tier": "extension",
            "artifact_name": artifact_name,
            "extension_properties": get_extension_property_list(artifact_name, columns),
        }

    def _read_first_row(self, csv_path: str) -> Optional[dict]:
        """Read the first data row from a CSV."""
        try:
            with open(csv_path, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    return dict(row)
        except Exception:
            return None
        return None
