"""
Batch generator — deterministic row-by-row JSON-LD generation.
Takes a column mapping (from Claude's reasoning) + CSV and emits one
ObservableObject per row in a complete @graph.

This is the missing piece between get_generation_context and validate_graph.
Claude reasons the mapping, this tool does the mechanical serialization.
"""

import csv
import json
import uuid
from pathlib import Path
from typing import Optional

from ioi_mcp.ontology_loader import OntologyLoader
from ioi_mcp.manifest import ManifestRegistry
from ioi_mcp.extension_gen import (
    generate_turtle_patch,
    IOI_EXT_NS,
    IOI_EXT_PREFIX,
    _to_facet_name,
    _to_property_name,
)
from ioi_mcp.type_inferencer import analyze_csv, infer_xsd_type


# Standard @context
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
    "ioi-ext": IOI_EXT_NS,
    "xsd": "http://www.w3.org/2001/XMLSchema#",
}

# Types that need typed literal serialization
_TYPED_LITERAL_TYPES = {
    "xsd:integer", "xsd:decimal", "xsd:boolean",
    "xsd:dateTime", "xsd:date", "xsd:hexBinary",
}


def _serialize_value(value: str, xsd_type: str, shape_hint: str):
    """Serialize a CSV cell value into JSON-LD typed literal or plain string."""
    if not value or value.strip() == "":
        return None

    value = value.strip()

    if shape_hint == "linked_object":
        # Object properties need nested node — skip in batch mode
        # (Claude handles these in manual generation)
        return None

    if xsd_type in _TYPED_LITERAL_TYPES:
        # Typed literal
        if xsd_type == "xsd:integer":
            try:
                return {"@type": "xsd:integer", "@value": str(int(value))}
            except ValueError:
                return value
        elif xsd_type == "xsd:decimal":
            try:
                return {"@type": "xsd:decimal", "@value": str(float(value))}
            except ValueError:
                return value
        elif xsd_type == "xsd:boolean":
            bool_val = value.lower() in ("true", "1", "yes")
            return {"@type": "xsd:boolean", "@value": str(bool_val).lower()}
        elif xsd_type in ("xsd:dateTime", "xsd:date"):
            return {"@type": xsd_type, "@value": value}
        elif xsd_type == "xsd:hexBinary":
            return {"@type": "xsd:hexBinary", "@value": value}
        else:
            return {"@type": xsd_type, "@value": value}
    else:
        # Plain string
        return value


def generate_all_rows(
    ontology: OntologyLoader,
    manifest: ManifestRegistry,
    artifact_name: str,
    csv_path: str,
    column_mapping: dict[str, str],
    description: Optional[str] = None,
) -> dict:
    """
    Generate a complete JSON-LD @graph with one ObservableObject per CSV row.

    Args:
        ontology: loaded ontology
        manifest: manifest registry
        artifact_name: e.g., 'Prefetch', 'SRUM'
        csv_path: path to CSV file
        column_mapping: {csv_column: "uco-observable:propertyName"}
            Columns not in mapping become ioi-ext: extension properties.
        description: optional artifact description

    Returns:
        {
            "jsonld": {...},           # Complete JSON-LD with all rows
            "jsonld_path": "...",      # Where it was saved
            "turtle_patch": "...",     # Turtle string (if extensions)
            "turtle_path": "...",      # Where Turtle was saved
            "row_count": N,            # Number of rows generated
            "mapped_columns": [...],
            "unmapped_columns": [...],
        }
    """
    # Resolve artifact
    entry = manifest.resolve(artifact_name)
    candidates = [artifact_name, f"Windows{artifact_name}", artifact_name.replace(" ", "")]

    obs_class = None
    for c in candidates:
        if ontology.observable_exists(c):
            uri = ontology.get_observable_uri(c)
            obs_name = uri.split("/")[-1] if uri else c
            obs_class = f"uco-observable:{obs_name}"
            break

    if not obs_class:
        obs_class = entry["uco_class"] if entry else "uco-observable:ObservableObject"

    # Analyze CSV for column types
    columns = analyze_csv(csv_path)
    col_types = {c["column_name"]: c["inferred_type"] for c in columns}

    # Build property info for mapped columns
    # {csv_column: {prefixed_prop, xsd_type, shape_hint, facet}}
    mapped_props = {}
    for csv_col, uco_prop in column_mapping.items():
        # Determine type from SHACL
        local_name = uco_prop.split(":")[-1] if ":" in uco_prop else uco_prop
        xsd_type = col_types.get(csv_col, "xsd:string")
        shape_hint = _shape_from_type(xsd_type)

        # Check SHACL for official type override
        if uco_prop.startswith("uco-observable:") or uco_prop.startswith("uco-core:"):
            shacl_type = _get_shacl_type(ontology, local_name)
            if shacl_type:
                xsd_type = shacl_type
                shape_hint = _shape_from_type(xsd_type)

        mapped_props[csv_col] = {
            "prefixed": uco_prop,
            "xsd_type": xsd_type,
            "shape_hint": shape_hint,
        }

    # Build unmapped column info (become ioi-ext)
    mapped_set = set(column_mapping.keys())
    unmapped_cols = [c for c in columns if c["column_name"] not in mapped_set]

    unmapped_props = {}
    for col in unmapped_cols:
        prop_name = _to_property_name(artifact_name, col["clean_name"])
        unmapped_props[col["column_name"]] = {
            "prefixed": f"{IOI_EXT_PREFIX}:{prop_name}",
            "xsd_type": col["inferred_type"],
            "shape_hint": _shape_from_type(col["inferred_type"]),
        }

    # Determine facets from manifest
    facet_names = entry.get("uco_facets", []) if entry else []
    official_facets = [f for f in facet_names if not f.startswith("ioi-ext:")]
    if not official_facets:
        official_facets = ["FileFacet", "ContentDataFacet"]

    # Group mapped properties by their target facet
    # For now: all mapped props go into the appropriate official facet
    # All unmapped go into the extension facet
    ext_facet_name = _to_facet_name(artifact_name)

    # Read all CSV rows and generate graph
    context = dict(BASE_CONTEXT)
    graph_nodes = []

    entity_local = obs_class.split(":")[-1].lower() if ":" in obs_class else artifact_name.lower()

    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row_idx, row in enumerate(reader):
            row_uuid = str(uuid.uuid4())

            # Build official facets
            facets = []

            # Extension facet (unmapped columns)
            if unmapped_props:
                ext_facet = {
                    "@id": f"kb:{ext_facet_name.lower()}--{row_uuid}",
                    "@type": [f"{IOI_EXT_PREFIX}:{ext_facet_name}", "uco-core:Facet"],
                }
                for csv_col, prop_info in unmapped_props.items():
                    val = row.get(csv_col, "")
                    serialized = _serialize_value(val, prop_info["xsd_type"], prop_info["shape_hint"])
                    if serialized is not None:
                        ext_facet[prop_info["prefixed"]] = serialized
                facets.append(ext_facet)

            # Official facets with mapped properties
            for facet_name in official_facets:
                facet_props = ontology.get_facet_properties(facet_name)
                if not facet_props:
                    continue

                facet_type = f"uco-observable:{facet_name}"
                facet_node = {
                    "@id": f"kb:{facet_name.lower()}--{row_uuid}",
                    "@type": [facet_type, "uco-core:Facet"],
                }

                for prop in facet_props:
                    # Find if any mapped column targets this property
                    for csv_col, prop_info in mapped_props.items():
                        if prop_info["prefixed"] == prop["name"]:
                            val = row.get(csv_col, "")
                            serialized = _serialize_value(
                                val, prop_info["xsd_type"], prop_info["shape_hint"]
                            )
                            if serialized is not None:
                                facet_node[prop["name"]] = serialized
                            break

                # Only add facet if it has at least one property beyond @id/@type
                if len(facet_node) > 2:
                    facets.append(facet_node)

            # Build the observable object node
            obs_node = {
                "@id": f"kb:{entity_local}--{row_uuid}",
                "@type": obs_class,
                "uco-core:hasFacet": facets,
            }
            graph_nodes.append(obs_node)

    # Also include ioi-ext properties from existing ioi-ext.ttl
    ext_facet_key = ext_facet_name.replace("Facet", "") + "Facet"
    existing_ext_props = ontology.get_ext_facet_properties(ext_facet_key)
    # (These are already covered if the column names match — no duplication needed)

    jsonld = {
        "@context": context,
        "@graph": graph_nodes,
    }

    # Generate Turtle patch for extension properties
    turtle_patch = None
    if unmapped_cols:
        turtle_patch = generate_turtle_patch(artifact_name, unmapped_cols, description)

    # Save files
    output_dir = Path(csv_path).parent
    jsonld_path = output_dir / f"{artifact_name.lower()}_full_graph.jsonld"
    with open(jsonld_path, "w") as f:
        json.dump(jsonld, f, indent=2, ensure_ascii=False)

    turtle_path = None
    if turtle_patch:
        turtle_path = output_dir / f"{artifact_name.lower()}_ext.ttl"
        with open(turtle_path, "w") as f:
            f.write(turtle_patch)

    return {
        "jsonld": jsonld,
        "jsonld_path": str(jsonld_path),
        "turtle_patch": turtle_patch,
        "turtle_path": str(turtle_path) if turtle_path else None,
        "row_count": len(graph_nodes),
        "mapped_columns": list(column_mapping.keys()),
        "unmapped_columns": [c["column_name"] for c in unmapped_cols],
    }


def _shape_from_type(xsd_type: str) -> str:
    """Derive shape hint from xsd type."""
    if "integer" in xsd_type:
        return "integer_literal"
    elif "dateTime" in xsd_type or "date" in xsd_type:
        return "datetime_literal"
    elif "decimal" in xsd_type or "float" in xsd_type:
        return "number_literal"
    elif "boolean" in xsd_type:
        return "boolean_literal"
    elif "hexBinary" in xsd_type:
        return "hex_literal"
    return "string"


def _get_shacl_type(ontology: OntologyLoader, local_name: str) -> Optional[str]:
    """Try to find the SHACL-defined type for a property across all facets."""
    # Search common facets for this property
    for facet_name in ["FileFacet", "ContentDataFacet", "WindowsPrefetchFacet",
                       "EventRecordFacet", "ProcessFacet", "NetworkConnectionFacet",
                       "URLHistoryFacet", "UserAccountFacet", "DeviceFacet",
                       "DiskFacet", "MemoryFacet", "MftRecordFacet"]:
        for prop in ontology.get_facet_properties(facet_name):
            if prop["local_name"] == local_name:
                r = prop["range"]
                if r in ("integer", "int"):
                    return "xsd:integer"
                elif r == "dateTime":
                    return "xsd:dateTime"
                elif r == "boolean":
                    return "xsd:boolean"
                elif r == "decimal":
                    return "xsd:decimal"
                elif r == "string":
                    return "xsd:string"
    return None
