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
from ioi_mcp.extension_gen import (
    generate_turtle_patch,
    IOI_EXT_NS,
    IOI_EXT_PREFIX,
    _to_facet_name,
    _to_property_name,
)
from ioi_mcp.type_inferencer import analyze_csv, infer_xsd_type


# Standard @context — uses short prefix names (core:, observable:) to match
# the IOI Framework convention used in CASES/ and RULES/
BASE_CONTEXT = {
    "kb": "http://example.org/kb/",
    "core": "https://ontology.unifiedcyberontology.org/uco/core/",
    "observable": "https://ontology.unifiedcyberontology.org/uco/observable/",
    "uco-action": "https://ontology.unifiedcyberontology.org/uco/action/",
    "ioi-ext": IOI_EXT_NS,
    "xsd": "http://www.w3.org/2001/XMLSchema#",
}

# Types that need typed literal serialization
_TYPED_LITERAL_TYPES = {
    "xsd:integer", "xsd:decimal", "xsd:boolean",
    "xsd:dateTime", "xsd:date", "xsd:hexBinary",
}


def _normalize_datetime(value: str) -> str:
    """Convert non-ISO datetime formats to ISO 8601."""
    import re
    from datetime import datetime
    
    value = value.strip()
    
    # Already ISO 8601
    if re.match(r'^\d{4}-\d{2}-\d{2}', value):
        return value
    
    # US locale: M/D/YYYY H:MM or M/D/YYYY H:MM:SS
    us_match = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})(:(\d{2}))?', value)
    if us_match:
        m, d, y = us_match.group(1), us_match.group(2), us_match.group(3)
        hr, mn = us_match.group(4), us_match.group(5)
        sec = us_match.group(7) or "00"
        return f"{y}-{int(m):02d}-{int(d):02d}T{int(hr):02d}:{mn}:{sec}"
    
    return value


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
            return {"@type": xsd_type, "@value": _normalize_datetime(value)}
        elif xsd_type == "xsd:hexBinary":
            return {"@type": "xsd:hexBinary", "@value": value}
        else:
            return {"@type": xsd_type, "@value": value}
    else:
        # Plain string
        return value


def generate_all_rows(
    ontology: OntologyLoader,
    artifact_name: str,
    csv_path: str,
    column_mapping: dict[str, str],
    description: Optional[str] = None,
) -> dict:
    """
    Generate a complete JSON-LD @graph with one ObservableObject per CSV row.

    Args:
        ontology: loaded ontology
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
    # Resolve artifact (ontology-only, no manifest)
    name_candidates = [artifact_name, f"Windows{artifact_name}", artifact_name.replace(" ", "")]

    obs_class = None
    for c in name_candidates:
        if ontology.observable_exists(c):
            uri = ontology.get_observable_uri(c)
            obs_name = uri.split("/")[-1] if uri else c
            obs_class = f"observable:{obs_name}"
            break

    # Keyword search fallback for observable class
    if not obs_class:
        _wiki = ontology.get_artifact_description(artifact_name)
        _desc = ""
        if isinstance(_wiki, dict):
            _desc = _wiki.get("description", "")
        elif isinstance(_wiki, str):
            _desc = _wiki
        search_results = ontology.search_candidates(
            artifact_name, description=_desc, threshold=40
        )
        for sr in search_results:
            if sr["type"] == "observable":
                obs_class = f"observable:{sr['class']}"
                break

    if not obs_class:
        obs_class = "observable:ObservableObject"

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
        if uco_prop.startswith("observable:") or uco_prop.startswith("core:") or uco_prop.startswith("uco-observable:") or uco_prop.startswith("uco-core:"):
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

    # Resolve wiki description once for reuse
    wiki_desc = ontology.get_artifact_description(artifact_name)
    desc_text = ""
    if isinstance(wiki_desc, dict):
        desc_text = wiki_desc.get("description", "")
    elif isinstance(wiki_desc, str):
        desc_text = wiki_desc

    # Determine facets from ontology (no manifest)
    official_facets = []
    # Step 1: Check for matching facet by exact name convention
    for fc in [f"{artifact_name}Facet", f"Windows{artifact_name}Facet"]:
        if ontology.facet_exists(fc):
            uri = ontology.get_facet_uri(fc)
            official_facets.append(uri.split("/")[-1] if uri else fc)
            break
    # Step 2: If no exact match, use keyword search to find facets
    #   e.g. "MFT" -> finds MftRecordFacet via tokenized search
    if not official_facets:
        candidates = ontology.search_candidates(
            artifact_name, description=desc_text, threshold=25
        )
        for cand in candidates:
            if cand["type"] == "facet":
                facet_local = cand["class"]
                if ontology.facet_exists(facet_local):
                    official_facets.append(facet_local)
                    break
            elif cand.get("facets"):
                for f_info in cand["facets"]:
                    if f_info["property_count"] > 0:
                        official_facets.append(f_info["facet"])
                break  # Use first observable candidate's facets
    # Step 3: Semantic facet search — find relevant facets via property
    # description keyword matching. These go to available_official_facets
    # (informational for LLM) rather than auto-matching, because SHACL
    # datatype constraints require careful type-aware mapping.
    semantic_facets_found = []
    if desc_text:
        sem_facets = ontology.find_relevant_facets(
            artifact_name, description=desc_text, top_n=3
        )
        for sf in sem_facets:
            if sf["score"] >= 3 and sf["facet"] not in official_facets:
                if sf["facet"] not in ("FileFacet", "ContentDataFacet"):
                    semantic_facets_found.append(sf)
    # Also check ioi-ext facets
    ext_facet_key = _to_facet_name(artifact_name).replace("Facet", "") + "Facet"
    if ontology.get_ext_facet_properties(ext_facet_key):
        pass  # Extension properties will be handled via column mapping
    # Always include FileFacet and ContentDataFacet (deduplicate)
    for default_f in ["FileFacet", "ContentDataFacet"]:
        if default_f not in official_facets:
            official_facets.append(default_f)

    # Pre-compute auto-match: for discovered official facets, match CSV
    # columns to SHACL properties by exact name match (case-insensitive).
    # Skips object properties (they need nested nodes, not flat CSV strings).
    # Matched columns are removed from unmapped_props so they go to the
    # official facet instead of ioi-ext.
    official_auto_match = {}  # {facet_name: {csv_col: {prop_name, xsd_type, shape_hint}}}
    for facet_name in official_facets:
        facet_props = ontology.get_facet_properties(facet_name)
        if not facet_props:
            continue
        auto = {}
        for prop in facet_props:
            if prop.get("range_type") == "object":
                continue  # Skip hash, alternateDataStreams — need nested nodes
            prop_local = prop["local_name"]
            # Gate 1: Verify the property IRI exists in ontology
            prop_iri_valid, _ = ontology.validate_type_iri(prop["name"])
            if not prop_iri_valid:
                continue
            shacl_range = str(prop.get("range", "")).lower()
            for csv_col in list(unmapped_props.keys()):
                col_clean = csv_col.replace(" ", "").lower()
                if prop_local.lower() == col_clean:
                    csv_type = col_types.get(csv_col, "xsd:string")
                    # Gate 2: Datatype compatibility check
                    # SHACL range must be compatible with CSV inferred type
                    compatible = _dtype_compatible(csv_type, shacl_range)
                    if not compatible:
                        break  # Name matches but type doesn't — skip
                    # Use SHACL type if available, else CSV inferred
                    xsd_type = f"xsd:{shacl_range}" if shacl_range and shacl_range in (
                        "string", "integer", "boolean", "datetime", "decimal",
                        "date", "float", "double", "hexbinary",
                    ) else csv_type
                    auto[csv_col] = {
                        "prop_name": prop["name"],
                        "xsd_type": xsd_type,
                        "shape_hint": _shape_from_type(xsd_type),
                    }
                    del unmapped_props[csv_col]
                    break
        if auto:
            official_auto_match[facet_name] = auto

    # Auto-match curated ioi-ext.ttl properties by column name
    # e.g. CSV "EntryNumber" → ioi-ext:entryNumber (from MftFacet in ioi-ext.ttl)
    ext_auto_match = {}  # {csv_col: {prop_name, xsd_type, shape_hint}}
    # Try multiple casing patterns: MFT→MftFacet, MFTFacet, WindowsMftFacet
    art_title = artifact_name.capitalize()  # MFT → Mft, LNK → Lnk
    for candidate_facet in [
        f"{art_title}Facet",
        f"Windows{art_title}Facet",
        f"{artifact_name}Facet",
        f"Windows{artifact_name}Facet",
        _to_facet_name(artifact_name),
    ]:
        ext_props = ontology.get_ext_facet_properties(candidate_facet)
        if not ext_props:
            continue
        for ep in ext_props:
            ep_local = ep["name"].split(":")[-1]
            for csv_col in list(unmapped_props.keys()):
                col_clean = csv_col.replace(" ", "").lower()
                if ep_local.lower() == col_clean:
                    xsd_type = ep.get("range", "string")
                    if not xsd_type.startswith("xsd:"):
                        xsd_type = f"xsd:{xsd_type}"
                    ext_auto_match[csv_col] = {
                        "prop_name": ep["name"],
                        "xsd_type": xsd_type,
                        "shape_hint": _shape_from_type(xsd_type),
                    }
                    del unmapped_props[csv_col]
                    break
        break  # Use first matching facet

    # Layer 0: Use curated ioi-ext.ttl facet name if it exists
    # e.g. MFT → MftFacet (from ioi-ext.ttl), LNK → WindowsLnkFacet
    ext_facet_name = None
    for candidate in [
        f"{art_title}Facet",
        f"Windows{art_title}Facet",
        f"{artifact_name}Facet",
        f"Windows{artifact_name}Facet",
        _to_facet_name(artifact_name),
    ]:
        if ontology.get_ext_facet_properties(candidate):
            ext_facet_name = candidate
            break
    if not ext_facet_name:
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

            # Extension facet (curated ioi-ext.ttl props + unmapped columns)
            if unmapped_props or ext_auto_match:
                ext_facet = {
                    "@id": f"kb:{ext_facet_name.lower()}--{row_uuid}",
                    "@type": [f"{IOI_EXT_PREFIX}:{ext_facet_name}", "core:Facet"],
                }
                # Curated ioi-ext.ttl properties first
                for csv_col, match_info in ext_auto_match.items():
                    val = row.get(csv_col, "")
                    serialized = _serialize_value(val, match_info["xsd_type"], match_info["shape_hint"])
                    if serialized is not None:
                        ext_facet[match_info["prop_name"]] = serialized
                # Remaining unmapped columns as auto-generated properties
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

                facet_type = f"observable:{facet_name}"
                facet_node = {
                    "@id": f"kb:{facet_name.lower()}--{row_uuid}",
                    "@type": [facet_type, "core:Facet"],
                }

                for prop in facet_props:
                    # Check if any explicitly mapped column targets this property
                    for csv_col, prop_info in mapped_props.items():
                        if prop_info["prefixed"] == prop["name"]:
                            val = row.get(csv_col, "")
                            serialized = _serialize_value(
                                val, prop_info["xsd_type"], prop_info["shape_hint"]
                            )
                            if serialized is not None:
                                facet_node[prop["name"]] = serialized
                            break

                # Add auto-matched columns
                auto = official_auto_match.get(facet_name, {})
                for csv_col, match_info in auto.items():
                    val = row.get(csv_col, "")
                    serialized = _serialize_value(
                        val, match_info["xsd_type"], match_info["shape_hint"]
                    )
                    if serialized is not None:
                        facet_node[match_info["prop_name"]] = serialized

                # Only add facet if it has at least one property beyond @id/@type
                if len(facet_node) > 2:
                    facets.append(facet_node)

            # Build the observable object node
            obs_node = {
                "@id": f"kb:{entity_local}--{row_uuid}",
                "@type": obs_class,
                "core:hasFacet": facets,
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

    # Build hint for official facets that were discovered but couldn't
    # auto-match. Return their properties so the LLM can re-call with
    # an explicit column_mapping to use them.
    available_official_facets = {}
    for facet_name in official_facets:
        # Only include facets that weren't auto-matched (no columns went to them)
        if facet_name in official_auto_match:
            continue
        facet_props = ontology.get_facet_properties(facet_name)
        if not facet_props:
            continue
        # Skip object-only facets
        datatype_props = [
            {
                "property": p["name"],
                "local_name": p["local_name"],
                "description": p.get("description", "")[:100],
                "type": p["range"],
            }
            for p in facet_props
            if p.get("range_type") != "object"
        ]
        if datatype_props:
            available_official_facets[facet_name] = datatype_props

    result = {
        "jsonld": jsonld,
        "jsonld_path": str(jsonld_path),
        "turtle_patch": turtle_patch,
        "turtle_path": str(turtle_path) if turtle_path else None,
        "row_count": len(graph_nodes),
        "mapped_columns": list(column_mapping.keys()),
        "unmapped_columns": [c["column_name"] for c in unmapped_cols],
    }

    if available_official_facets:
        result["available_official_facets"] = available_official_facets
        result["mapping_hint"] = (
            "Official CASE/UCO facets were found but could not auto-match "
            "column names. Review the properties below and re-call with a "
            "column_mapping to use official properties instead of ioi-ext. "
            "Example: {'EntryNumber': 'observable:mftFileID'}"
        )

    # Add semantically discovered facets (from property description matching)
    if semantic_facets_found:
        result["semantic_facet_matches"] = semantic_facets_found
        result["semantic_hint"] = (
            "Additional CASE/UCO facets found by matching artifact description "
            "against property descriptions. Review and include relevant properties "
            "in column_mapping to use them."
        )

    return result


def _dtype_compatible(csv_type: str, shacl_range: str) -> bool:
    """Check if a CSV inferred type is compatible with a SHACL range.
    
    Follows gate logic from case_uco.py: string is compatible with string,
    integer with integer/nonNegativeInteger, dateTime with dateTime, etc.
    String columns are compatible with string SHACL ranges.
    Integer columns are NOT compatible with dateTime ranges.
    """
    csv_t = csv_type.lower().replace("xsd:", "")
    shacl_t = shacl_range.lower().replace("xsd:", "")
    
    if not shacl_t or shacl_t in ("", "none"):
        return True  # No SHACL constraint = anything goes
    
    # Exact match
    if csv_t == shacl_t:
        return True
    
    # String CSV values can match string SHACL properties
    if csv_t == "string" and shacl_t == "string":
        return True
    
    # Integer CSV compatible with integer/nonNegativeInteger
    if csv_t == "integer" and shacl_t in ("integer", "nonnegativeinteger", "int", "long"):
        return True
    
    # DateTime CSV compatible with dateTime
    if csv_t == "datetime" and shacl_t in ("datetime", "date"):
        return True
    
    # Boolean
    if csv_t == "boolean" and shacl_t == "boolean":
        return True
    
    # String CSV can coerce to string SHACL (most permissive)
    if shacl_t == "string":
        return True
    
    # Integer→string is acceptable (will be serialized as string)
    if csv_t == "integer" and shacl_t == "string":
        return True
    
    # Everything else is incompatible
    return False


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
