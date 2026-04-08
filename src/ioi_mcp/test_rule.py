"""
test_rule — load JSON-LD graphs into rdflib Dataset as named graphs,
run a SPARQL .rq file, return results.

No Virtuoso needed. Pure rdflib in-process.
Supports named GRAPH queries, cross-graph JOINs, FILTER, OPTIONAL, BIND, VALUES.
Does NOT support bif:datediff (Virtuoso-specific) — use standard SPARQL datetime comparison.
"""

import json
from pathlib import Path
from typing import Optional

from rdflib import Dataset, URIRef, Graph


def test_rule(
    rule_path: str,
    graphs: list[dict],
) -> dict:
    """
    Load graphs into rdflib Dataset and execute a SPARQL rule.

    Args:
        rule_path: path to .rq file
        graphs: list of {
            "graph_iri": "https://ioi-framework.github.io/cases/AF-NEW/graphs/mft",
            "graph_path": "/path/to/mft_full_graph.jsonld"
        }

    Returns:
        {
            "success": True/False,
            "row_count": N,
            "columns": [...],
            "rows": [{col: val, ...}, ...],
            "error": null or error message,
            "graphs_loaded": [{"iri": ..., "triples": N}, ...],
            "rule_path": "...",
        }
    """
    # Validate inputs
    if not Path(rule_path).exists():
        return {"success": False, "error": f"Rule file not found: {rule_path}"}

    rule_text = Path(rule_path).read_text()

    # Check for Virtuoso-specific functions
    warnings = []
    if "bif:" in rule_text:
        warnings.append(
            "Rule uses bif: functions (Virtuoso-specific). "
            "These will fail in rdflib. Rewrite using standard SPARQL. "
            "Example: Replace ABS(bif:datediff('second', ?t1, ?t2)) <= 2 "
            "with FILTER(ABS(xsd:integer(?t1) - xsd:integer(?t2)) <= 2) "
            "or simple != comparison."
        )

    # Detect cross-graph anti-join pattern (known rdflib limitation)
    import re as _re
    _has_graph = bool(_re.search(r"GRAPH\s*<", rule_text, _re.IGNORECASE))
    _has_antijoin_fenx = bool(_re.search(r"FILTER\s+NOT\s+EXISTS", rule_text, _re.IGNORECASE))
    _has_minus = bool(_re.search(r"\bMINUS\b\s*\{", rule_text, _re.IGNORECASE))
    if _has_graph and _has_antijoin_fenx:
        warnings.append(
            "Rule uses cross-graph FILTER NOT EXISTS with GRAPH clauses. "
            "rdflib may return incorrect results for this pattern. "
            "HOWEVER: oxigraph (playground) handles named-graph FILTER NOT EXISTS correctly. "
            "Recommended: test in the playground first. "
            "For rdflib local testing, use the two-step approach: "
            "query graph A names first, inject via VALUES, then filter."
        )
    if _has_graph and _has_minus:
        warnings.append(
            "Rule uses MINUS subquery with GRAPH clauses. "
            "MINUS subquery is unreliable in both rdflib and oxigraph for cross-graph patterns. "
            "Replace with FILTER NOT EXISTS { GRAPH <IRI> { ... } } instead — "
            "this is verified correct in oxigraph."
        )

    # Create Dataset and load graphs
    ds = Dataset()
    graphs_loaded = []

    for g_info in graphs:
        graph_iri = g_info["graph_iri"]
        graph_path = g_info["graph_path"]

        if not Path(graph_path).exists():
            return {"success": False, "error": f"Graph file not found: {graph_path}"}

        try:
            # Parse JSON-LD into a named graph
            named_graph = ds.graph(URIRef(graph_iri))

            # Read the JSON-LD
            with open(graph_path, "r") as f:
                data = json.load(f)

            # Parse into the named graph
            # rdflib needs the JSON-LD as string
            jsonld_str = json.dumps(data)
            named_graph.parse(data=jsonld_str, format="json-ld")

            graphs_loaded.append({
                "iri": graph_iri,
                "triples": len(named_graph),
                "path": graph_path,
            })

        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to load graph {graph_iri}: {str(e)}",
                "graphs_loaded": graphs_loaded,
            }

    # Execute the SPARQL query
    try:
        results = ds.query(rule_text)

        # Extract column names and rows
        columns = [str(v) for v in results.vars] if results.vars else []
        rows = []
        for row in results:
            row_dict = {}
            for i, var in enumerate(results.vars):
                val = row[i]
                row_dict[str(var)] = str(val) if val is not None else None
            rows.append(row_dict)

        return {
            "success": True,
            "row_count": len(rows),
            "columns": columns,
            "rows": rows[:50],  # Cap at 50 rows for response size
            "total_rows": len(rows),
            "graphs_loaded": graphs_loaded,
            "rule_path": rule_path,
            "warnings": warnings if warnings else None,
            "fired": len(rows) > 0,
        }

    except Exception as e:
        error_msg = str(e)
        # Add helpful hints for common errors
        hints = []
        if "bif:" in error_msg or "bif:" in rule_text:
            hints.append(
                "The rule uses bif: functions which are Virtuoso-specific. "
                "Rewrite using standard SPARQL datetime comparison."
            )
        if "GRAPH" in error_msg:
            hints.append(
                "Check that graph IRIs in the rule match the graph_iri values provided."
            )

        return {
            "success": False,
            "error": error_msg,
            "hints": hints if hints else None,
            "graphs_loaded": graphs_loaded,
            "rule_path": rule_path,
            "warnings": warnings if warnings else None,
        }


def generate_test_graph(
    artifact_name: str,
    graph_iri: str,
    synthetic_values: dict[str, dict],
    context: Optional[dict] = None,
) -> dict:
    """
    Generate a minimal synthetic JSON-LD test graph with specific values
    designed to make a SPARQL rule fire.

    Args:
        artifact_name: e.g., "MFT"
        graph_iri: e.g., "https://ioi-framework.github.io/cases/AF-NEW/graphs/mft"
        synthetic_values: {
            "facet_type": "ioi-ext:MftFacet",
            "entity_type": "observable:File",
            "properties": {
                "ioi-ext:entryNumber": {"@type": "xsd:integer", "@value": "12345"},
                "ioi-ext:created0x10": {"@type": "xsd:dateTime", "@value": "2025-02-16T10:15:00"},
                ...
            }
        }
        context: optional @context dict (uses standard if not provided)

    Returns:
        Complete JSON-LD dict ready to save as .jsonld
    """
    import uuid

    if context is None:
        context = {
            "kb": "https://ioi-framework.github.io/kg/kb/",
            "core": "https://ontology.unifiedcyberontology.org/uco/core/",
            "observable": "https://ontology.unifiedcyberontology.org/uco/observable/",
            "ioi-ext": "https://ioi-framework.github.io/ns/ioi-ext/",
            "xsd": "http://www.w3.org/2001/XMLSchema#",
        }

    row_uuid = str(uuid.uuid4())
    entity_type = synthetic_values.get("entity_type", "observable:ObservableObject")
    facet_type = synthetic_values.get("facet_type", f"ioi-ext:{artifact_name}Facet")

    # Build facet with synthetic properties
    facet = {
        "@id": f"kb:{artifact_name.lower()}-facet--{row_uuid}",
        "@type": [facet_type, "core:Facet"],
    }

    for prop_name, prop_value in synthetic_values.get("properties", {}).items():
        facet[prop_name] = prop_value

    # Build entity node
    entity = {
        "@id": f"kb:{artifact_name.lower()}-entry--{row_uuid}",
        "@type": entity_type,
        "core:hasFacet": [facet],
    }

    # Add additional facets if specified
    for extra_facet in synthetic_values.get("extra_facets", []):
        extra = {
            "@id": f"kb:{artifact_name.lower()}-{extra_facet.get('type', 'facet').split(':')[-1].lower()}--{row_uuid}",
            "@type": [extra_facet.get("type", "core:Facet"), "core:Facet"],
        }
        for pn, pv in extra_facet.get("properties", {}).items():
            extra[pn] = pv
        entity["core:hasFacet"].append(extra)

    return {
        "@context": context,
        "@graph": [entity],
    }
