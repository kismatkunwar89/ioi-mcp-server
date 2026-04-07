"""
Batch generator — deterministic row-by-row JSON-LD generation.

Registry path (M-4): When a registry_entry is passed, uses canonical
field_types and facet directly — no ontology derivation needed.

Provenance path (M-5): Emits source-file + entry + action nodes
matching the IoI Framework instantiator structure.
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

# ─── Constants ────────────────────────────────────────────────────────────────

KB_NAMESPACE = "https://ioi-framework.github.io/kb/"

BASE_CONTEXT = {
    "kb":          KB_NAMESPACE,
    "core":        "https://ontology.unifiedcyberontology.org/uco/core/",
    "observable":  "https://ontology.unifiedcyberontology.org/uco/observable/",
    "uco-action":  "https://ontology.unifiedcyberontology.org/uco/action/",
    "ioi-ext":     IOI_EXT_NS,
    "xsd":         "http://www.w3.org/2001/XMLSchema#",
}

_TYPED_LITERAL_TYPES = {
    "xsd:integer", "xsd:decimal", "xsd:boolean",
    "xsd:dateTime", "xsd:date", "xsd:hexBinary",
}

# FileFacet property → observable: prefix mapping (standard CASE/UCO)
_FILE_FACET_PROP_MAP = {
    "FileName":        "observable:fileName",
    "Extension":       "observable:extension",
    "FileSize":        "observable:sizeInBytes",
    "IsDirectory":     "observable:isDirectory",
    "ReferenceCount":  "observable:ntfsHardLinkCount",
    "SecurityId":      "observable:ntfsOwnerSID",
    "Name":            "observable:fileName",
    "ParentPath":      "observable:filePath",
    "SourceFile":      "observable:fileName",
    "FilePath":        "observable:filePath",
}

# ─── Serialisation helpers ────────────────────────────────────────────────────

def _normalize_datetime(value: str) -> str:
    import re
    value = value.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", value):
        return re.sub(r"^(\d{4}-\d{2}-\d{2})\s+(\d)", r"T", value)
    us = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?", value)
    if us:
        m, d, y = us.group(1), us.group(2), us.group(3)
        hr, mn  = us.group(4), us.group(5)
        sec     = us.group(6) or "00"
        return f"{y}-{int(m):02d}-{int(d):02d}T{int(hr):02d}:{mn}:{sec}"
    return value


def _serialize(value: str, xsd_type: str, shape_hint: str):
    if not value or value.strip() == "":
        return None
    value = value.strip()
    if shape_hint == "linked_object":
        return None
    if xsd_type in _TYPED_LITERAL_TYPES:
        if xsd_type == "xsd:integer":
            try:   return {"@type": "xsd:integer", "@value": str(int(float(value)))}
            except ValueError: return value
        if xsd_type == "xsd:decimal":
            try:   return {"@type": "xsd:decimal", "@value": str(float(value))}
            except ValueError: return value
        if xsd_type == "xsd:boolean":
            return {"@type": "xsd:boolean",
                    "@value": str(value.lower() in ("true", "1", "yes")).lower()}
        if xsd_type in ("xsd:dateTime", "xsd:date"):
            return {"@type": xsd_type, "@value": _normalize_datetime(value)}
        if xsd_type == "xsd:hexBinary":
            return {"@type": "xsd:hexBinary", "@value": value}
        return {"@type": xsd_type, "@value": value}
    return value


def _shape(xsd_type: str) -> str:
    if "integer" in xsd_type: return "integer_literal"
    if "dateTime" in xsd_type or "date" in xsd_type: return "datetime_literal"
    if "decimal" in xsd_type or "float" in xsd_type: return "number_literal"
    if "boolean" in xsd_type: return "boolean_literal"
    if "hexBinary" in xsd_type: return "hex_literal"
    return "string"


# ─── Registry-aware fast path ─────────────────────────────────────────────────

def _generate_registry_path(
    artifact_name: str,
    csv_path: str,
    column_mapping: dict,
    registry_entry: dict,
    include_provenance: bool = True,
) -> tuple[list, list]:
    """
    Fast path for registry-known artifacts.
    Returns (graph_nodes, unmapped_cols_for_turtle_patch).
    """
    field_types  = registry_entry.get("field_types", {})
    file_cols    = set(registry_entry.get("file_facet_columns", []))
    facet_pref   = registry_entry.get("facet", f"ioi-ext:{_to_facet_name(artifact_name)}")
    facet_local  = facet_pref.split(":")[-1]  # e.g. MftFacet

    int_cols  = set(field_types.get("integer", []))
    dt_cols   = set(field_types.get("datetime", []))
    bool_cols = set(field_types.get("boolean", []))
    str_cols  = set(field_types.get("string", []))

    def xsd_for(col):
        if col in int_cols:  return "xsd:integer"
        if col in dt_cols:   return "xsd:dateTime"
        if col in bool_cols: return "xsd:boolean"
        return "xsd:string"

    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader  = csv.DictReader(f)
        headers = reader.fieldnames or []
        rows    = list(reader)

    # Columns not in field_types at all → become auto-generated ioi-ext properties
    all_known = int_cols | dt_cols | bool_cols | str_cols | file_cols
    truly_unmapped = [h for h in headers if h not in all_known and h not in column_mapping]

    art_lower = artifact_name.lower().replace(" ", "_")

    # Optional provenance: one source node + N action nodes
    source_uuid = str(uuid.uuid4())
    source_node = None
    if include_provenance:
        source_node = {
            "@id":  f"kb:source-file--{source_uuid}",
            "@type": "observable:File",
            "core:hasFacet": [{
                "@id":   f"kb:source-file-facet--{source_uuid}",
                "@type": "observable:FileFacet",
                "observable:fileName": Path(csv_path).name,
                "observable:filePath": str(csv_path),
                "observable:extension": ".csv",
            }],
        }

    entry_nodes  = []
    action_nodes = []

    for row in rows:
        entry_uuid = str(uuid.uuid4())

        # ── FileFacet ────────────────────────────────────────────
        file_facet = {
            "@id":   f"kb:{art_lower}-file-facet--{entry_uuid}",
            "@type": "observable:FileFacet",
        }
        for col in file_cols:
            val = row.get(col, "")
            prop = column_mapping.get(col) or _FILE_FACET_PROP_MAP.get(col)
            if prop and val:
                xsd = xsd_for(col)
                s   = _serialize(val, xsd, _shape(xsd))
                if s is not None:
                    file_facet[prop] = s

        # ── Artifact Facet ───────────────────────────────────────
        art_facet = {
            "@id":   f"kb:{facet_local.lower()}--{entry_uuid}",
            "@type": [facet_pref, "core:Facet"],
        }
        for col, val in row.items():
            if col in file_cols:
                continue
            # Explicit mapping overrides
            if col in column_mapping:
                prop = column_mapping[col]
            else:
                prop_local = _to_property_name(artifact_name, col.replace(" ", ""))
                prop = f"{IOI_EXT_PREFIX}:{prop_local}"
            xsd = xsd_for(col)
            s   = _serialize(str(val), xsd, _shape(xsd))
            if s is not None:
                art_facet[prop] = s

        facets = [f for f in [file_facet, art_facet] if len(f) > 2]

        entry = {
            "@id":          f"kb:{art_lower}--{entry_uuid}",
            "@type":        "observable:File",
            "core:hasFacet": facets,
        }
        entry_nodes.append(entry)

        if include_provenance and source_node:
            action_uuid = str(uuid.uuid4())
            action_nodes.append({
                "@id":   f"kb:action--{action_uuid}",
                "@type": "uco-action:InvestigativeAction",
                "core:source": {"@id": source_node["@id"]},
                "core:target": {"@id": entry["@id"]},
            })

    all_nodes = []
    if source_node:
        all_nodes.append(source_node)
    all_nodes.extend(entry_nodes)
    all_nodes.extend(action_nodes)

    return all_nodes, truly_unmapped


# ─── Main generation function ─────────────────────────────────────────────────

def generate_all_rows(
    ontology: OntologyLoader,
    artifact_name: str,
    csv_path: str,
    column_mapping: dict[str, str],
    description: Optional[str] = None,
    registry_entry: Optional[dict] = None,
    include_provenance: bool = True,
) -> dict:
    """
    Generate a complete CASE/UCO JSON-LD @graph from all rows in a CSV.

    If registry_entry is provided (from ManifestRegistry.resolve()),
    uses the canonical field_types directly — no ontology search.
    Otherwise falls back to the ontology derivation path.

    Args:
        ontology: loaded OntologyLoader
        artifact_name: e.g. "MFT", "Prefetch"
        csv_path: path to CSV file
        column_mapping: {csv_column: prefixed_property} — explicit overrides
        description: optional human description
        registry_entry: dict from ManifestRegistry.resolve() (optional)
        include_provenance: emit source-file + action nodes (default True for registry path)

    Returns dict with jsonld, jsonld_path, turtle_patch, turtle_path,
    row_count, mapped_columns, unmapped_columns, flow_state.
    """

    # ── Registry fast path ───────────────────────────────────────────────────
    if registry_entry:
        graph_nodes, unmapped_headers = _generate_registry_path(
            artifact_name, csv_path, column_mapping,
            registry_entry, include_provenance,
        )
        columns = analyze_csv(csv_path)
        unmapped_cols = [c for c in columns if c["column_name"] in unmapped_headers]

        turtle_patch = None
        if unmapped_cols:
            turtle_patch = generate_turtle_patch(artifact_name, unmapped_cols, description)

        jsonld = {"@context": dict(BASE_CONTEXT), "@graph": graph_nodes}
        output_dir  = Path(csv_path).parent
        jsonld_path = output_dir / f"{artifact_name.lower()}_full_graph.jsonld"
        turtle_path = None

        with open(jsonld_path, "w") as f:
            json.dump(jsonld, f, indent=2, ensure_ascii=False)

        if turtle_patch:
            turtle_path = output_dir / f"{artifact_name.lower()}_ext.ttl"
            with open(turtle_path, "w") as f:
                f.write(turtle_patch)

        # Entry count = total nodes minus source (1) and action nodes (N)
        entry_count = sum(
            1 for n in graph_nodes
            if n.get("@id", "").startswith(f"kb:{artifact_name.lower()}--")
        )

        return {
            "jsonld":           jsonld,
            "jsonld_path":      str(jsonld_path),
            "turtle_patch":     turtle_patch,
            "turtle_path":      str(turtle_path) if turtle_path else None,
            "row_count":        entry_count,
            "mapped_columns":   list(column_mapping.keys()),
            "unmapped_columns": [c["column_name"] for c in unmapped_cols],
            "provenance":       include_provenance,
            "flow_state": {
                "step_completed": "generate_all_rows",
                "path": "known_artifact",
                "next": {
                    "primary": f"validate_graph('{jsonld_path}') — must pass before proceeding",
                    "if_invalid": "fix column_mapping, re-run generate_all_rows",
                },
                "invariant": "validate_graph must pass before scaffold_case or draft_sparql_context",
            },
        }

    # ── Ontology fallback path (unknown artifacts) ───────────────────────────
    # Resolve artifact class
    obs_class = None
    for c in [artifact_name, f"Windows{artifact_name}", artifact_name.replace(" ", "")]:
        if ontology.observable_exists(c):
            uri = ontology.get_observable_uri(c)
            obs_class = f"observable:{uri.split('/')[-1]}" if uri else f"observable:{c}"
            break
    if not obs_class:
        wiki = ontology.get_artifact_description(artifact_name)
        desc = wiki.get("description", "") if isinstance(wiki, dict) else (wiki or "")
        for sr in ontology.search_candidates(artifact_name, description=desc, threshold=40):
            if sr["type"] == "observable":
                obs_class = f"observable:{sr['class']}"
                break
    if not obs_class:
        obs_class = "observable:File"

    columns    = analyze_csv(csv_path)
    col_types  = {c["column_name"]: c["inferred_type"] for c in columns}
    mapped_set = set(column_mapping.keys())
    unmapped_cols = [c for c in columns if c["column_name"] not in mapped_set]

    art_title = artifact_name.capitalize()
    ext_facet_name = None
    for candidate in [f"{art_title}Facet", f"Windows{art_title}Facet",
                      f"{artifact_name}Facet", _to_facet_name(artifact_name)]:
        if ontology.get_ext_facet_properties(candidate):
            ext_facet_name = candidate
            break
    if not ext_facet_name:
        ext_facet_name = _to_facet_name(artifact_name)

    unmapped_props = {}
    for col in unmapped_cols:
        prop_name = _to_property_name(artifact_name, col["clean_name"])
        unmapped_props[col["column_name"]] = {
            "prefixed":   f"{IOI_EXT_PREFIX}:{prop_name}",
            "xsd_type":   col["inferred_type"],
            "shape_hint": _shape(col["inferred_type"]),
        }

    mapped_props = {}
    for csv_col, uco_prop in column_mapping.items():
        xsd_type   = col_types.get(csv_col, "xsd:string")
        shape_hint = _shape(xsd_type)
        mapped_props[csv_col] = {
            "prefixed": uco_prop, "xsd_type": xsd_type, "shape_hint": shape_hint,
        }

    graph_nodes = []
    entity_local = obs_class.split(":")[-1].lower()

    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row_uuid = str(uuid.uuid4())

            ext_facet = {
                "@id":   f"kb:{ext_facet_name.lower()}--{row_uuid}",
                "@type": [f"{IOI_EXT_PREFIX}:{ext_facet_name}", "core:Facet"],
            }
            for csv_col, prop_info in unmapped_props.items():
                val = row.get(csv_col, "")
                s   = _serialize(val, prop_info["xsd_type"], prop_info["shape_hint"])
                if s is not None:
                    ext_facet[prop_info["prefixed"]] = s

            facets = [ext_facet] if len(ext_facet) > 2 else []

            for csv_col, prop_info in mapped_props.items():
                val = row.get(csv_col, "")
                s   = _serialize(val, prop_info["xsd_type"], prop_info["shape_hint"])
                if s is not None:
                    if not facets:
                        facets.append({"@id": f"kb:facet--{row_uuid}", "@type": "core:Facet"})
                    facets[-1][prop_info["prefixed"]] = s

            obs = {
                "@id":          f"kb:{entity_local}--{row_uuid}",
                "@type":        obs_class,
                "core:hasFacet": facets,
            }
            graph_nodes.append(obs)

    turtle_patch = generate_turtle_patch(artifact_name, unmapped_cols, description) if unmapped_cols else None
    jsonld       = {"@context": dict(BASE_CONTEXT), "@graph": graph_nodes}
    output_dir   = Path(csv_path).parent
    jsonld_path  = output_dir / f"{artifact_name.lower()}_full_graph.jsonld"
    turtle_path  = None

    with open(jsonld_path, "w") as f:
        json.dump(jsonld, f, indent=2, ensure_ascii=False)
    if turtle_patch:
        turtle_path = output_dir / f"{artifact_name.lower()}_ext.ttl"
        with open(turtle_path, "w") as f:
            f.write(turtle_patch)

    return {
        "jsonld":           jsonld,
        "jsonld_path":      str(jsonld_path),
        "turtle_patch":     turtle_patch,
        "turtle_path":      str(turtle_path) if turtle_path else None,
        "row_count":        len(graph_nodes),
        "mapped_columns":   list(column_mapping.keys()),
        "unmapped_columns": [c["column_name"] for c in unmapped_cols],
        "provenance":       False,
        "flow_state": {
            "step_completed": "generate_all_rows",
            "path": "new_artifact",
            "next": {
                "primary": f"validate_graph('{jsonld_path}')",
                "recommended": "generate_instantiator to register this artifact for future sessions",
            },
        },
    }


# ── Private helpers for the ontology fallback path ────────────────────────────

def _dtype_compatible(csv_type: str, shacl_range: str) -> bool:
    csv_t    = csv_type.lower().replace("xsd:", "")
    shacl_t  = shacl_range.lower().replace("xsd:", "")
    if not shacl_t: return True
    if csv_t == shacl_t: return True
    if csv_t == "integer" and shacl_t in ("integer","nonnegativeinteger","int","long"): return True
    if csv_t == "datetime" and shacl_t in ("datetime","date"): return True
    if csv_t == "boolean"  and shacl_t == "boolean": return True
    if shacl_t == "string": return True
    return False


def _get_shacl_type(ontology: OntologyLoader, local_name: str) -> Optional[str]:
    for facet_name in ["FileFacet","ContentDataFacet","WindowsPrefetchFacet",
                       "EventRecordFacet","ProcessFacet","NetworkConnectionFacet"]:
        for prop in ontology.get_facet_properties(facet_name):
            if prop["local_name"] == local_name:
                r = prop["range"]
                mapping = {"integer":"xsd:integer","int":"xsd:integer",
                           "dateTime":"xsd:dateTime","boolean":"xsd:boolean",
                           "decimal":"xsd:decimal","string":"xsd:string"}
                return mapping.get(r)
    return None
