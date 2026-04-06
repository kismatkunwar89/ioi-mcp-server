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
            "graph_iri": "http://example.org/mft_caseN"  (optional, auto-generated if missing)
        }
        contradiction_description: what the rule should detect
        category: "temporal", "structural", or "semantic"

    Returns:
        {
            "prefixes": {...},
            "graphs": [
                {
                    "name": "MFT",
                    "graph_iri": "http://example.org/mft_caseN",
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
        graph_iri = g_info.get("graph_iri", f"http://example.org/{name.lower()}_case")

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
    """Recursively extract types and properties from a JSON-LD node."""
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

    # Extract properties
    for key, value in node.items():
        if key.startswith("@"):
            continue

        prop_key = key
        if prop_key in seen_props:
            continue
        seen_props.add(prop_key)

        # Determine type from value
        if isinstance(value, dict):
            if "@type" in value and "@value" in value:
                xsd_type = value["@type"]
                sample = str(value["@value"])[:50]
                properties.append({
                    "property": prop_key,
                    "type": xsd_type,
                    "sample_value": sample,
                })
            elif "@id" in value:
                properties.append({
                    "property": prop_key,
                    "type": "object_ref",
                    "sample_value": str(value.get("@id", ""))[:50],
                })
            else:
                _extract_from_node(value, types_used, properties, seen_props)
        elif isinstance(value, list):
            if value and isinstance(value[0], dict):
                _extract_from_node(value[0], types_used, properties, seen_props)
        elif isinstance(value, str):
            properties.append({
                "property": prop_key,
                "type": "xsd:string",
                "sample_value": value[:50],
            })


def _build_sparql_template(graphs: list, category: str) -> str:
    """Build a skeleton SPARQL template."""
    prefix_lines = "\n".join(
        f"PREFIX {k}: <{v}>" for k, v in IOI_PREFIXES.items()
    )

    graph_blocks = []
    for g in graphs:
        vars_list = []
        patterns = []
        for p in g["properties_used"][:5]:  # First 5 properties as example
            var_name = p["property"].split(":")[-1]
            patterns.append(f"      ?facet {p['property']} ?{var_name} .")
            vars_list.append(f"?{var_name}")

        block = f"""  GRAPH <{g['graph_iri']}> {{
    ?{g['name'].lower()}Facet a <FACET_TYPE> ;
{chr(10).join(patterns)}
    ?{g['name'].lower()}Entry core:hasFacet ?{g['name'].lower()}Facet .
  }}"""
        graph_blocks.append(block)

    filter_hint = {
        "temporal": "  # FILTER: Compare datetime values across graphs\n  # FILTER(?timestamp1 != ?timestamp2)",
        "structural": "  # FILTER NOT EXISTS: Check for missing artifacts\n  # FILTER NOT EXISTS { GRAPH <...> { ?x a <type> } }",
        "semantic": "  # FILTER: Match/mismatch values across graphs\n  # FILTER(?value1 != ?value2)",
    }

    return f"""{prefix_lines}

SELECT DISTINCT ?result_var1 ?result_var2
WHERE {{
{chr(10).join(graph_blocks)}

{filter_hint.get(category, '  # FILTER: (add detection logic)')}
}}
ORDER BY ?result_var1"""
