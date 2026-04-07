"""
draft_sparql_context — extracts property IRIs from JSON-LD graphs
and returns a structured map for Claude to write SPARQL rules.

Also provides reference prefixes and example patterns from existing rules.
"""

import json
from pathlib import Path
from typing import Optional


# Standard prefixes used in IoI rules
IOI_PREFIXES = {
    "core": "https://ontology.unifiedcyberontology.org/uco/core/",
    "observable": "https://ontology.unifiedcyberontology.org/uco/observable/",
    "ioi-ext": "https://ioi-framework.github.io/ns/ioi-ext/",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
}


def extract_sparql_context(
    graphs: list[dict],
    contradiction_description: str = "",
    category: str = "temporal",
) -> dict:
    """
    Extract everything Claude needs to write a SPARQL IoI rule.

    Args:
        graphs: list of {
            "name": "MFT",
            "graph_path": "/path/to/mft_full_graph.jsonld",
            "graph_iri": "https://ioi-framework.github.io/cases/AF-NEW/graphs/mft"  (optional, auto-generated if missing)
        }
        contradiction_description: what the rule should detect
        category: "temporal", "structural", or "semantic"

    Returns:
        {
            "prefixes": {...},
            "graphs": [
                {
                    "name": "MFT",
                    "graph_iri": "https://ioi-framework.github.io/cases/AF-NEW/graphs/mft",
                    "types_used": ["observable:File", "ioi-ext:MftFacet", ...],
                    "properties_used": [
                        {"property": "ioi-ext:entryNumber", "type": "xsd:integer", "sample": "12345"},
                        ...
                    ]
                }
            ],
            "join_candidates": [...],  # properties that appear in multiple graphs
            "contradiction_description": "...",
            "category": "temporal",
            "sparql_template": "...",  # skeleton SPARQL for Claude to fill
        }
    """
    result_graphs = []
    all_properties = {}  # property -> list of graph names it appears in

    for g_info in graphs:
        name = g_info["name"]
        graph_path = g_info["graph_path"]
        graph_iri = g_info.get("graph_iri", f"https://ioi-framework.github.io/cases/AF-NEW/graphs/{name.lower()}")

        if not Path(graph_path).exists():
            continue

        with open(graph_path, "r") as f:
            data = json.load(f)

        # Extract types and properties from the graph
        types_used = set()
        properties = []
        seen_props = set()

        for node in data.get("@graph", []):
            _extract_from_node(node, types_used, properties, seen_props)

        # Track which properties appear in which graphs (for join candidates)
        for p in properties:
            prop_key = p["property"]
            if prop_key not in all_properties:
                all_properties[prop_key] = []
            all_properties[prop_key].append(name)

        result_graphs.append({
            "name": name,
            "graph_iri": graph_iri,
            "types_used": sorted(types_used),
            "property_count": len(properties),
            "properties_used": properties,
        })

    # Find join candidates — properties in multiple graphs
    join_candidates = [
        {"property": prop, "in_graphs": graph_names}
        for prop, graph_names in all_properties.items()
        if len(set(graph_names)) > 1
    ]

    # Build SPARQL template skeleton
    sparql_template = _build_sparql_template(result_graphs, category)

    return {
        "prefixes": IOI_PREFIXES,
        "graphs": result_graphs,
        "join_candidates": join_candidates,
        "contradiction_description": contradiction_description,
        "category": category,
        "sparql_template": sparql_template,
        "note": (
            "Use the properties_used lists to write your WHERE clauses. "
            "Join graphs using properties that appear in join_candidates. "
            "Write FILTER conditions that detect the contradiction described above. "
            "For temporal rules: compare datetime properties across graphs. "
            "For structural rules: use FILTER NOT EXISTS for missing artifacts. "
            "For semantic rules: match values across graphs (e.g., URL in one but not another). "
            "IMPORTANT: Do NOT use bif:datediff (Virtuoso-specific). Use standard SPARQL datetime comparison."
        ),
    }


def _extract_from_node(node: dict, types_used: set, properties: list, seen_props: set):
    """Recursively extract types and properties from ALL nested nodes.
    
    Walks into core:hasFacet arrays to reach facet nodes where
    ioi-ext: properties live. The seen_props set prevents duplicate
    property entries but does NOT block recursion into child nodes.
    """
    if not isinstance(node, dict):
        return

    # Extract @type
    node_type = node.get("@type")
    if node_type:
        if isinstance(node_type, list):
            for t in node_type:
                if isinstance(t, str) and ":" in t:
                    types_used.add(t)
        elif isinstance(node_type, str) and ":" in node_type:
            types_used.add(node_type)

    # Extract properties from this node
    for key, value in node.items():
        if key.startswith("@"):
            continue

        prop_key = key

        # Determine type from value and record if not seen
        if isinstance(value, dict):
            if "@type" in value and "@value" in value:
                if prop_key not in seen_props:
                    seen_props.add(prop_key)
                    xsd_type = value["@type"]
                    sample = str(value["@value"])[:50]
                    properties.append({
                        "property": prop_key,
                        "type": xsd_type,
                        "sample_value": sample,
                    })
            elif "@id" in value:
                if prop_key not in seen_props:
                    seen_props.add(prop_key)
                    properties.append({
                        "property": prop_key,
                        "type": "object_ref",
                        "sample_value": str(value.get("@id", ""))[:50],
                    })
            else:
                # Always recurse into nested objects (facet nodes etc.)
                _extract_from_node(value, types_used, properties, seen_props)
        elif isinstance(value, list):
            # Recurse into ALL list items, not just the first
            for item in value:
                if isinstance(item, dict):
                    _extract_from_node(item, types_used, properties, seen_props)
        elif isinstance(value, str):
            if prop_key not in seen_props:
                seen_props.add(prop_key)
                properties.append({
                    "property": prop_key,
                    "type": "xsd:string",
                    "sample_value": value[:50],
                })


def _build_sparql_template(graphs: list, category: str) -> str:
    """Build a skeleton SPARQL template.
    
    Always wraps patterns in GRAPH ?g {} for rdflib Dataset compatibility.
    Uses correct triple structure: ?entry core:hasFacet ?facet, then
    ?facet <property> ?value.
    """
    prefix_lines = "\n".join(
        f"PREFIX {k}: <{v}>" for k, v in IOI_PREFIXES.items()
    )

    # Collect all select variables and build pattern blocks
    select_vars = ["?entry"]
    all_patterns = []

    for g in graphs:
        patterns = []
        for p in g["properties_used"][:8]:  # First 8 properties
            var_name = p["property"].split(":")[-1]
            if f"?{var_name}" not in select_vars:
                select_vars.append(f"?{var_name}")
            patterns.append(f"    ?facet {p['property']} ?{var_name} .")

        if patterns:
            all_patterns.extend(patterns)

    # Single GRAPH ?g block (works for both single-graph and multi-graph queries)
    pattern_block = "\n".join(all_patterns)

    filter_hint = {
        "temporal": (
            "  # FILTER: Compare datetime values\n"
            "  # FILTER(?timestamp1 != ?timestamp2)"
        ),
        "structural": (
            "  # FILTER NOT EXISTS: Check for missing data\n"
            "  # FILTER NOT EXISTS { ?facet <property> ?val }"
        ),
        "semantic": (
            "  # FILTER: Match/mismatch values\n"
            "  # FILTER(?value1 != ?value2)"
        ),
    }

    return f"""{prefix_lines}

SELECT DISTINCT {' '.join(select_vars[:6])}
WHERE {{
  GRAPH ?g {{
    ?entry core:hasFacet ?facet .
{pattern_block}
  }}

{filter_hint.get(category, '  # FILTER: (add detection logic)')}
}}
ORDER BY ?entry"""
