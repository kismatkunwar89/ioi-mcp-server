"""
Constraint builder — extracts property constraints from SHACL for LLM-driven JSON-LD generation.
Returns the structured context that Claude needs to generate correct JSON-LD.
No hardcoded templates. No deterministic generation. Knowledge only.
"""

import uuid
from typing import Optional

from ioi_mcp.ontology_loader import OntologyLoader
from ioi_mcp.type_inferencer import analyze_csv
from ioi_mcp.extension_gen import (
    generate_turtle_patch,
    get_extension_property_list,
    IOI_EXT_PREFIX,
    IOI_EXT_NS,
    _to_facet_name,
    _to_property_name,
)


# Standard @context for all CASE/UCO JSON-LD
STANDARD_CONTEXT = {
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


def build_generation_context(
    ontology: OntologyLoader,
    artifact_name: str,
    uco_class: str,
    facet_names: list[str],
    csv_path: str,
    column_mapping: dict[str, str],
    description: Optional[str] = None,
) -> dict:
    """
    Build the complete context Claude needs to generate valid JSON-LD.

    Returns:
    {
        "context": {...},                    # @context dict
        "entity_class": "uco-observable:...",
        "facets": [
            {
                "facet_type": "uco-observable:WindowsPrefetchFacet",
                "tier": "official",
                "constraints": [
                    {
                        "property": "uco-observable:timesExecuted",
                        "datatype": "xsd:integer",
                        "objectType": null,
                        "nodeKind": "Literal",
                        "max_count": 1,
                        "is_array": false,
                        "description": "The number of times...",
                        "mapped_csv_column": "RunCount",
                        "sample_value": "157",
                        "shape_hint": "integer_literal",
                    },
                    ...
                ]
            },
            {
                "facet_type": "ioi-ext:PrefetchFacet",
                "tier": "extension",
                "constraints": [...],
            }
        ],
        "turtle_patch": "..." or null,
        "id_pattern": "kb:<type>--<uuid4>",
        "sample_uuid": "a1b2c3d4-...",
        "generation_rules": "...",
    }
    """
    # Analyze CSV
    columns = analyze_csv(csv_path)
    sample_row = _read_first_row(csv_path)

    # Build context dict
    context = dict(STANDARD_CONTEXT)

    # Split columns into mapped and unmapped
    mapped_csv_cols = set(column_mapping.keys())
    unmapped_columns = [c for c in columns if c["column_name"] not in mapped_csv_cols]

    has_extensions = bool(unmapped_columns)
    if has_extensions:
        context[IOI_EXT_PREFIX] = IOI_EXT_NS

    # Reverse mapping: property_local_name → csv_column_name
    prop_to_csv = {}
    for csv_col, uco_prop in column_mapping.items():
        prop_to_csv[uco_prop] = csv_col

    # Build facet constraints from SHACL
    facets = []
    for facet_name in facet_names:
        if facet_name.startswith("ioi-ext:"):
            continue  # Handled below

        props = ontology.get_facet_properties(facet_name)

        # Determine prefixed facet type
        uri = ontology.get_facet_uri(facet_name)
        if uri and "observable/" in uri:
            facet_type = f"uco-observable:{facet_name}"
        elif uri and "core/" in uri:
            facet_type = f"uco-core:{facet_name}"
        else:
            facet_type = facet_name

        constraints = []
        for p in props:
            prop_prefixed = p["name"]

            # Determine shape hint
            if p["range_type"] == "object":
                shape_hint = "linked_object"
                node_kind = "IRI"
                datatype = None
                object_type = p["range"]
                # Determine full prefixed objectType
                if "/" in p["range_iri"]:
                    ns = p["range_iri"].rsplit("/", 1)[0] + "/"
                    local = p["range_iri"].rsplit("/", 1)[1]
                    if "observable/" in ns:
                        object_type = f"uco-observable:{local}"
                    elif "core/" in ns:
                        object_type = f"uco-core:{local}"
                    elif "types/" in ns:
                        object_type = f"uco-types:{local}"
                    else:
                        object_type = local
            else:
                node_kind = "Literal"
                object_type = None
                xsd_range = p["range"]
                if xsd_range in ("integer", "int"):
                    datatype = "xsd:integer"
                    shape_hint = "integer_literal"
                elif xsd_range in ("decimal", "float", "double"):
                    datatype = "xsd:decimal"
                    shape_hint = "number_literal"
                elif xsd_range in ("dateTime",):
                    datatype = "xsd:dateTime"
                    shape_hint = "datetime_literal"
                elif xsd_range == "boolean":
                    datatype = "xsd:boolean"
                    shape_hint = "boolean_literal"
                elif xsd_range == "hexBinary":
                    datatype = "xsd:hexBinary"
                    shape_hint = "hex_literal"
                else:
                    datatype = "xsd:string"
                    shape_hint = "string"

            # Find mapped CSV column and sample value
            mapped_col = prop_to_csv.get(prop_prefixed)
            sample_val = None
            if mapped_col and sample_row:
                sample_val = sample_row.get(mapped_col)

            constraints.append({
                "property": prop_prefixed,
                "description": p.get("description", ""),
                "label": p.get("label", p["local_name"]),
                "datatype": datatype,
                "objectType": object_type,
                "nodeKind": node_kind,
                "max_count": p["max_count"],
                "is_array": p["is_array"],
                "shape_hint": shape_hint,
                "mapped_csv_column": mapped_col,
                "sample_value": str(sample_val) if sample_val else None,
            })

        facets.append({
            "facet_type": facet_type,
            "tier": "official",
            "constraint_count": len(constraints),
            "constraints": constraints,
        })

    # Build extension facet constraints for unmapped columns
    turtle_patch = None
    if unmapped_columns:
        ext_facet_name = _to_facet_name(artifact_name)
        ext_facet_type = f"{IOI_EXT_PREFIX}:{ext_facet_name}"

        ext_constraints = []
        for col in unmapped_columns:
            prop_name = _to_property_name(artifact_name, col["clean_name"])
            prop_prefixed = f"{IOI_EXT_PREFIX}:{prop_name}"

            xsd_type = col["inferred_type"]
            if "integer" in xsd_type:
                shape_hint = "integer_literal"
            elif "dateTime" in xsd_type:
                shape_hint = "datetime_literal"
            elif "decimal" in xsd_type:
                shape_hint = "number_literal"
            elif "boolean" in xsd_type:
                shape_hint = "boolean_literal"
            elif "hexBinary" in xsd_type:
                shape_hint = "hex_literal"
            else:
                shape_hint = "string"

            sample_val = None
            if sample_row:
                sample_val = sample_row.get(col["column_name"])

            ext_constraints.append({
                "property": prop_prefixed,
                "description": f"From CSV column: {col['column_name']}",
                "label": col["column_name"],
                "datatype": xsd_type,
                "objectType": None,
                "nodeKind": "Literal",
                "max_count": 1,
                "is_array": False,
                "shape_hint": shape_hint,
                "mapped_csv_column": col["column_name"],
                "sample_value": str(sample_val) if sample_val else None,
            })

        facets.insert(0, {
            "facet_type": ext_facet_type,
            "tier": "extension",
            "constraint_count": len(ext_constraints),
            "constraints": ext_constraints,
        })

        turtle_patch = generate_turtle_patch(artifact_name, unmapped_columns, description)

    # Generate a sample UUID for the LLM to use as pattern
    sample_uuid = str(uuid.uuid4())

    # Entity local name for @id pattern
    entity_local = uco_class.split(":")[-1].lower() if ":" in uco_class else uco_class.lower()

    # Build generation rules (the system prompt context)
    generation_rules = _build_generation_rules(entity_local, uco_class, facets, sample_uuid)

    return {
        "context": context,
        "entity_class": uco_class,
        "entity_local": entity_local,
        "facets": facets,
        "turtle_patch": turtle_patch,
        "id_pattern": f"kb:{entity_local}--<uuid4>",
        "sample_uuid": sample_uuid,
        "generation_rules": generation_rules,
        "csv_row_count": columns[0]["total_rows"] if columns else 0,
    }


def _build_generation_rules(
    entity_local: str,
    entity_class: str,
    facets: list[dict],
    sample_uuid: str,
) -> str:
    """Build the generation rules string for the LLM."""

    facet_blocks = []
    for f in facets:
        ft = f["facet_type"]
        tier = f["tier"]
        facet_local = ft.split(":")[-1].lower() if ":" in ft else ft.lower()

        lines = [f"    Facet: {ft} (tier={tier})"]
        for c in f["constraints"]:
            prop = c["property"]
            hint = c["shape_hint"]
            mapped = c.get("mapped_csv_column") or "NOT_MAPPED"
            sample = c.get("sample_value") or ""
            dt = c.get("datatype") or ""
            ot = c.get("objectType") or ""

            line = f"      {prop}: shape={hint}"
            if dt:
                line += f" datatype={dt}"
            if ot:
                line += f" objectType={ot}"
            line += f" csv_column={mapped}"
            if sample:
                line += f" sample={sample[:60]}"
            lines.append(line)

        facet_blocks.append("\n".join(lines))

    constraints_text = "\n\n".join(facet_blocks)

    return f"""Generate valid CASE/UCO JSON-LD with these rules:

1) @id format: "kb:<type>--{sample_uuid}" (use SAME UUID for entity and all facets)
2) Entity @type: "{entity_class}"
3) Entity root has ONLY: @id, @type, uco-core:hasFacet
4) Each facet is a separate object inside uco-core:hasFacet array
5) TYPED LITERALS:
   - integer_literal → {{"@type":"xsd:integer","@value":"<value>"}}
   - number_literal → {{"@type":"xsd:decimal","@value":"<value>"}}
   - datetime_literal → {{"@type":"xsd:dateTime","@value":"<ISO-8601>"}}
   - boolean_literal → {{"@type":"xsd:boolean","@value":"true|false"}}
   - hex_literal → {{"@type":"xsd:hexBinary","@value":"<hex>"}}
   - string → plain string value
6) LINKED OBJECTS (shape=linked_object):
   Must be nested: {{"@id":"kb:<type>--<uuid>","@type":"<objectType>","uco-core:name":"<value>"}}
   NEVER emit a plain string for linked_object properties
   If no objectType is given, OMIT that property entirely
7) Omit properties with empty/null values
8) Only emit properties listed below — do NOT invent extra properties
9) UUID must be valid hex only (0-9, a-f), never use g-z

Facets and constraints:

{constraints_text}"""


def _read_first_row(csv_path: str) -> dict | None:
    """Read the first data row from a CSV."""
    import csv as csv_mod
    try:
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv_mod.DictReader(f)
            for row in reader:
                return dict(row)
    except Exception:
        return None
    return None
