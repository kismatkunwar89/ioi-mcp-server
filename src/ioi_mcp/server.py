"""
IOI Framework MCP Server
Scalable, case-agnostic CASE/UCO JSON-LD artifact graph generator.

Tools (11 total):
  Core generation:
    resolve_artifact      — Rich context: ontology + ioi-ext properties with descriptions
    analyze_csv           — CSV column analysis: headers, sample values, inferred types
    get_generation_context — Full constraints for LLM-driven JSON-LD generation
    generate_from_csv     — Auto-generate (no mapping needed, for extension artifacts)
    get_facet_properties  — SHACL property extraction for any Facet
    generate_all_rows     — Deterministic batch: one ObservableObject per CSV row
    validate_graph        — Full IRI + SHACL validation

  Contribution workflow:
    scaffold_case         — Assemble CASES/AF-NEW/ directory for a PR
    draft_sparql_context  — Extract property IRIs from graphs for SPARQL rule writing
    generate_test_graph   — Synthetic test graph with specific values
    test_rule             — Execute SPARQL .rq against rdflib Dataset (no Virtuoso)

Flow:
  1. Claude calls resolve_artifact → gets SHACL properties with rdfs:comment descriptions
  2. Claude calls analyze_csv → gets CSV headers with sample values and inferred types
  3. Claude reasons the mapping (CSV column → CASE/UCO property) using both contexts
  4. Claude calls generate_all_rows with the mapping → MCP builds validated JSON-LD
  5. (Optional) scaffold_case → package for PR, draft_sparql_context → write rules, test_rule → verify
"""

import json
import os
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from ioi_mcp.ontology_loader import OntologyLoader
from ioi_mcp.graph_builder import GraphBuilder
from ioi_mcp.constraint_builder import build_generation_context
from ioi_mcp.batch_generator import generate_all_rows
from ioi_mcp.validator import Validator
from ioi_mcp.scaffold_case import scaffold_case as _scaffold_case
from ioi_mcp.sparql_context import extract_sparql_context
from ioi_mcp.test_rule import test_rule as _test_rule, generate_test_graph as _generate_test_graph
from ioi_mcp.type_inferencer import analyze_csv as _analyze_csv

# Initialize server
app = Server("ioi-mcp-server")

# Module singletons (initialized on first use)
_ontology: OntologyLoader | None = None
_builder: GraphBuilder | None = None
_validator: Validator | None = None


def _init():
    """Lazy initialization of all modules."""
    global _ontology, _builder, _validator
    if _ontology is None:
        ext_ttl = os.environ.get("IOI_EXT_TTL")
        _ontology = OntologyLoader(extra_ttl=ext_ttl)
        _builder = GraphBuilder(_ontology)
        _validator = Validator(_ontology)


# ─── Tool definitions ────────────────────────────────────────────────

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="resolve_artifact",
            description=(
                "Look up a forensic artifact and get FULL context for mapping. "
                "Returns: tier (official/extension), CASE/UCO class, all Facets, "
                "and for each Facet ALL SHACL properties with their rdfs:comment "
                "descriptions, xsd types, and cardinality. Use this FIRST to "
                "understand what properties are available before mapping CSV columns."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "artifact_name": {
                        "type": "string",
                        "description": "Artifact name, e.g., 'Prefetch', 'SRUM', 'EventRecord'",
                    },
                },
                "required": ["artifact_name"],
            },
        ),
        Tool(
            name="analyze_csv",
            description=(
                "Analyze a CSV file to extract column metadata for mapping. "
                "Returns: column headers, inferred xsd types (from actual values), "
                "sample values (first 5 non-empty), and clean camelCase names. "
                "Use this SECOND (after resolve_artifact) to see the CSV structure "
                "before reasoning the column-to-property mapping."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "csv_path": {
                        "type": "string",
                        "description": "Path to the CSV file to analyze",
                    },
                },
                "required": ["csv_path"],
            },
        ),
        Tool(
            name="get_generation_context",
            description=(
                "Get the FULL generation context for producing CASE/UCO JSON-LD. "
                "Returns: @context dict, property constraints (datatype, objectType, nodeKind, "
                "shape hints), mapped CSV values, generation rules, and Turtle patch for extensions. "
                "YOU (Claude) then generate the JSON-LD using these constraints. "
                "After generating, call validate_graph to check your output. "
                "This replaces deterministic generation — the LLM produces better JSON-LD "
                "because it understands property semantics and handles object properties correctly."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "artifact_name": {
                        "type": "string",
                        "description": "Artifact name",
                    },
                    "csv_path": {
                        "type": "string",
                        "description": "Path to the CSV file",
                    },
                    "column_mapping": {
                        "type": "object",
                        "description": (
                            "YOUR mapping of CSV columns to CASE/UCO properties. "
                            "Keys = CSV column headers (exact). "
                            "Values = prefixed CASE/UCO property names. "
                            "Example: {"
                            "'RunCount': 'uco-observable:timesExecuted', "
                            "'ExecutableName': 'uco-observable:applicationFileName', "
                            "'LastRun': 'uco-observable:lastRun', "
                            "'Hash': 'uco-observable:prefetchHash'"
                            "}. "
                            "Columns NOT in this mapping become ioi-ext: properties."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional description of the artifact",
                    },
                },
                "required": ["artifact_name", "csv_path", "column_mapping"],
            },
        ),
        Tool(
            name="generate_from_csv",
            description=(
                "Auto-generate a CASE/UCO JSON-LD graph from a CSV WITHOUT manual mapping. "
                "Best for unknown/extension artifacts where all columns become ioi-ext: properties. "
                "For official artifacts, prefer the resolve_artifact → analyze_csv → generate_graph "
                "flow to get proper CASE/UCO property mapping."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "artifact_name": {
                        "type": "string",
                        "description": "Artifact name",
                    },
                    "csv_path": {
                        "type": "string",
                        "description": "Path to the CSV file",
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional description of the artifact",
                    },
                },
                "required": ["artifact_name", "csv_path"],
            },
        ),
        Tool(
            name="get_facet_properties",
            description=(
                "Get all SHACL-defined properties for a specific CASE/UCO Facet. "
                "Returns property names, rdfs:comment descriptions, xsd types, "
                "and cardinality. Queries the ontology live."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "facet_name": {
                        "type": "string",
                        "description": "Facet name, e.g., 'WindowsPrefetchFacet', 'FileFacet', 'EventRecordFacet'",
                    },
                },
                "required": ["facet_name"],
            },
        ),
        Tool(
            name="generate_all_rows",
            description=(
                "Generate a complete CASE/UCO JSON-LD knowledge graph from ALL rows in a CSV file. "
                "Use this when the user says 'generate JSON-LD', 'create knowledge graph', "
                "'represent artifact as CASE/UCO', 'convert CSV to JSON-LD', or 'generate graph for all rows'. "
                "Takes the column mapping (from your reasoning after resolve_artifact + analyze_csv) "
                "and deterministically generates one ObservableObject per CSV row with correct typed literals. "
                "Handles both official CASE/UCO properties and ioi-ext extension properties. "
                "Outputs a complete @graph file + Turtle patch. Run validate_graph after this."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "artifact_name": {
                        "type": "string",
                        "description": "Artifact name, e.g., 'Prefetch', 'SRUM', 'MFT', 'USNJournal'",
                    },
                    "csv_path": {
                        "type": "string",
                        "description": "Path to the CSV file with forensic data",
                    },
                    "column_mapping": {
                        "type": "object",
                        "description": (
                            "YOUR mapping of CSV columns to CASE/UCO or ioi-ext properties. "
                            "Keys = CSV column headers (exact). "
                            "Values = prefixed property names. "
                            "Example: {'RunCount': 'uco-observable:timesExecuted', "
                            "'EntryNumber': 'ioi-ext:entryNumber'}. "
                            "Columns NOT in this mapping become auto-generated ioi-ext: properties."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional description of the artifact",
                    },
                },
                "required": ["artifact_name", "csv_path", "column_mapping"],
            },
        ),
        Tool(
            name="validate_graph",
            description=(
                "Validate a CASE/UCO JSON-LD file. "
                "Use this after generate_all_rows to check the output. "
                "Checks: IRI resolution, @id format, @context completeness, "
                "rdflib parseability, and SHACL conformance."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "jsonld_path": {
                        "type": "string",
                        "description": "Path to the JSON-LD file to validate",
                    },
                    "turtle_patch_path": {
                        "type": "string",
                        "description": "Optional path to ioi-ext.ttl for extension validation",
                    },
                },
                "required": ["jsonld_path"],
            },
        ),
        # ─── Contribution workflow tools ──────────────────────────
        Tool(
            name="scaffold_case",
            description=(
                "Assemble a complete CASES/AF-NEW/ directory structure for an IoI contribution PR. "
                "Generates: ground_truth.md, mapping.md, README.md, JSON-LD snippets, test graph stubs, "
                "and Jekyll front-matter stubs for the docs site. Requires pre-generated graphs from "
                "generate_all_rows. Use this AFTER generating graphs to package everything for submission."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "output_dir": {
                        "type": "string",
                        "description": "Base directory (e.g., path to the ioi-framework repo clone)",
                    },
                    "case_id": {
                        "type": "string",
                        "description": "Case identifier, e.g. 'AF-NEW' (placeholder, maintainers finalize)",
                    },
                    "title": {
                        "type": "string",
                        "description": "Human-readable title, e.g. 'Timestomping Detection via Prefetch Analysis'",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Plain English description of the anti-forensic technique and detection approach",
                    },
                    "artifacts": {
                        "type": "array",
                        "description": (
                            "List of artifact objects, each with: "
                            "'name' (str), 'graph_path' (str, path to .jsonld), "
                            "'turtle_path' (str, optional path to ext .ttl), "
                            "'column_mapping' (object, CSV col → property), "
                            "'csv_path' (str, path to source CSV)"
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "graph_path": {"type": "string"},
                                "turtle_path": {"type": "string"},
                                "column_mapping": {"type": "object"},
                                "csv_path": {"type": "string"},
                            },
                            "required": ["name", "graph_path", "column_mapping", "csv_path"],
                        },
                    },
                    "contributor": {
                        "type": "string",
                        "description": "GitHub handle of the contributor",
                    },
                    "category": {
                        "type": "string",
                        "description": "IoI category: 'temporal', 'structural', or 'semantic'",
                        "enum": ["temporal", "structural", "semantic"],
                    },
                    "inconsistency_description": {
                        "type": "string",
                        "description": "Description of the contradiction/inconsistency this case detects",
                    },
                },
                "required": ["output_dir", "title", "summary", "artifacts"],
            },
        ),
        Tool(
            name="draft_sparql_context",
            description=(
                "Extract property IRIs from JSON-LD graphs and return structured context "
                "for writing SPARQL IoI detection rules. Returns: all prefixes, types used, "
                "properties with sample values per graph, join candidates (properties in multiple graphs), "
                "and a skeleton SPARQL template. Use this BEFORE writing a SPARQL rule to understand "
                "what properties are available and how to join across artifact graphs."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "graphs": {
                        "type": "array",
                        "description": (
                            "List of graph objects, each with: "
                            "'name' (str, e.g. 'MFT'), 'graph_path' (str, path to .jsonld), "
                            "'graph_iri' (str, optional, e.g. 'http://example.org/mft_caseN')"
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "graph_path": {"type": "string"},
                                "graph_iri": {"type": "string"},
                            },
                            "required": ["name", "graph_path"],
                        },
                    },
                    "contradiction_description": {
                        "type": "string",
                        "description": "What the SPARQL rule should detect (the contradiction)",
                    },
                    "category": {
                        "type": "string",
                        "description": "IoI category: 'temporal', 'structural', or 'semantic'",
                        "enum": ["temporal", "structural", "semantic"],
                    },
                },
                "required": ["graphs"],
            },
        ),
        Tool(
            name="generate_test_graph",
            description=(
                "Generate a minimal synthetic JSON-LD test graph with specific values "
                "designed to make a SPARQL rule fire (or not fire, for negative tests). "
                "Use this to create controlled test data for SPARQL rule validation "
                "without needing real forensic CSVs."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "artifact_name": {
                        "type": "string",
                        "description": "Artifact name, e.g. 'MFT', 'Prefetch'",
                    },
                    "graph_iri": {
                        "type": "string",
                        "description": "Named graph IRI, e.g. 'http://example.org/mft_test'",
                    },
                    "synthetic_values": {
                        "type": "object",
                        "description": (
                            "Object with: 'facet_type' (str, e.g. 'ioi-ext:MftFacet'), "
                            "'entity_type' (str, e.g. 'observable:ObservableObject'), "
                            "'properties' (object mapping property IRIs to typed values: "
                            "{'ioi-ext:created0x10': {'@type': 'xsd:dateTime', '@value': '2025-02-16T10:15:00'}}), "
                            "'extra_facets' (array of additional facets attached to the same entry, "
                            "e.g. [{'type': 'observable:FileFacet', 'properties': "
                            "{'observable:fileName': {'@type': 'xsd:string', '@value': 'malware.exe'}}}] "
                            "— use this to add FileFacet alongside the artifact-specific facet)"
                        ),
                    },
                    "output_path": {
                        "type": "string",
                        "description": "Where to save the .jsonld file (defaults to /tmp/)",
                    },
                },
                "required": ["artifact_name", "graph_iri", "synthetic_values"],
            },
        ),
        Tool(
            name="test_rule",
            description=(
                "Load JSON-LD graphs into an rdflib Dataset as named graphs and execute "
                "a SPARQL .rq rule file against them. Returns: rows matched, column names, values, "
                "and whether the rule 'fired' (found contradictions). Pure rdflib — no Virtuoso needed. "
                "Supports named GRAPH queries, cross-graph JOINs, FILTER, OPTIONAL, BIND, VALUES. "
                "Does NOT support bif: functions (Virtuoso-specific)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "rule_path": {
                        "type": "string",
                        "description": "Path to the .rq SPARQL rule file",
                    },
                    "graphs": {
                        "type": "array",
                        "description": (
                            "List of graph objects, each with: "
                            "'graph_iri' (str, the named graph IRI matching the SPARQL GRAPH clause), "
                            "'graph_path' (str, path to the .jsonld file)"
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "graph_iri": {"type": "string"},
                                "graph_path": {"type": "string"},
                            },
                            "required": ["graph_iri", "graph_path"],
                        },
                    },
                },
                "required": ["rule_path", "graphs"],
            },
        ),
    ]


# ─── Tool handlers ───────────────────────────────────────────────────

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    _init()

    handlers = {
        "resolve_artifact": _handle_resolve,
        "analyze_csv": _handle_analyze_csv,
        "get_generation_context": _handle_generation_context,
        "generate_all_rows": _handle_generate_all_rows,
        "generate_from_csv": _handle_generate_csv,
        "get_facet_properties": _handle_facet_properties,
        "validate_graph": _handle_validate,
        "scaffold_case": _handle_scaffold_case,
        "draft_sparql_context": _handle_sparql_context,
        "generate_test_graph": _handle_test_graph,
        "test_rule": _handle_test_rule,
    }

    handler = handlers.get(name)
    if handler:
        return handler(arguments)
    return [TextContent(type="text", text=f"Unknown tool: {name}")]


def _handle_resolve(args: dict) -> list[TextContent]:
    """
    Scalable resolve: ontology-first, fully agnostic.
    1. Check ontology directly for ObservableObject + Facet
    2. Check ioi-ext.ttl for existing extension facets
    3. If nothing found anywhere → extension path
    Always returns full SHACL properties with rdfs:comment descriptions.
    """
    artifact_name = args["artifact_name"]

    # --- Strategy 1: Direct ontology lookup ---
    # Try common name patterns against the ontology index
    candidates = [
        artifact_name,
        f"Windows{artifact_name}",
        artifact_name.replace(" ", ""),
    ]

    obs_class = None
    obs_class_name = None
    for c in candidates:
        if _ontology.observable_exists(c):
            obs_class_name = c
            # Get the proper cased name from the index
            uri = _ontology.get_observable_uri(c)
            obs_class_name = uri.split("/")[-1] if uri else c
            obs_class = f"uco-observable:{obs_class_name}"
            break

    # Find matching facet
    facet_name = None
    facet_candidates = [
        f"{obs_class_name}Facet" if obs_class_name else None,
        f"{artifact_name}Facet",
        f"Windows{artifact_name}Facet",
    ]
    for fc in facet_candidates:
        if fc and _ontology.facet_exists(fc):
            uri = _ontology.get_facet_uri(fc)
            facet_name = uri.split("/")[-1] if uri else fc
            break

    # --- Build response ---
    if obs_class and facet_name:
        # Full ontology match — build facets from SHACL
        facet_names = [facet_name, "FileFacet", "ContentDataFacet"]
        # Also include ioi-ext facets if they exist for this artifact
        ext_facet_key = obs_class_name + "Facet" if obs_class_name else None
        # Check common extension facet name patterns
        for ext_name in [f"{artifact_name}Facet", f"Windows{artifact_name}Facet", 
                         f"{obs_class_name}Facet" if obs_class_name else None]:
            if ext_name and _ontology.get_ext_facet_properties(ext_name):
                if ext_name not in facet_names:
                    facet_names.append(ext_name)
                break

        facet_details = _build_facet_details(facet_names)

        wiki_desc = _ontology.get_artifact_description(artifact_name)

        result = {
            "found": True,
            "source": "ontology",
            "artifact": artifact_name,
            "tier": "official",
            "uco_class": obs_class,
            "artifact_description": wiki_desc,
            "facets": facet_details,
            "next_step": (
                "Call analyze_csv with your CSV file to see column headers and sample values. "
                "Then match CSV columns to the properties listed above based on their descriptions. "
                "Finally call generate_graph with your column_mapping."
            ),
        }

    else:
        # Nothing found via exact match — search for candidates
        # Uses tokenized keyword search against rdfs:label + rdfs:comment
        wiki_desc = _ontology.get_artifact_description(artifact_name)
        # wiki_desc can be a dict or string — extract the description text
        desc_text = ""
        if isinstance(wiki_desc, dict):
            desc_text = wiki_desc.get("description", "")
        elif isinstance(wiki_desc, str):
            desc_text = wiki_desc

        candidates = _ontology.search_candidates(
            artifact_name,
            description=desc_text,
            threshold=25,
        )

        # Still provide FileFacet + ContentDataFacet properties as context
        facet_details = _build_facet_details(["FileFacet", "ContentDataFacet"])

        result = {
            "found": False,
            "source": "none",
            "artifact": artifact_name,
            "tier": "extension",
            "uco_class": "uco-observable:ObservableObject",
            "artifact_description": wiki_desc,
            "message": (
                f"'{artifact_name}' has no dedicated class or facet in CASE/UCO. "
                "All CSV columns will become ioi-ext: extension properties. "
                "The graph will use uco-observable:ObservableObject as the base class."
            ),
            "facets": facet_details,
            "note": (
                "FileFacet and ContentDataFacet are included above — you can still "
                "map file-related CSV columns (timestamps, size, path, hash) to these "
                "official properties. Everything else becomes ioi-ext."
            ),
            "next_step": (
                "Call analyze_csv with your CSV file. Map any file-related columns to "
                "FileFacet/ContentDataFacet properties above. Remaining columns become "
                "ioi-ext: properties. Then call generate_graph with your mapping."
            ),
        }

        # Add ontology candidates if any scored above threshold
        # Auto-expand top facet candidates with full property lists
        # so the LLM can reason about column→property mapping in one call
        if candidates:
            expanded = []
            facets_expanded = 0
            for cand in candidates:
                if cand["type"] == "facet" and facets_expanded < 3:
                    props = _ontology.get_facet_properties(cand["class"])
                    cand["properties"] = [
                        {
                            "name": p["name"],
                            "local_name": p["local_name"],
                            "description": p.get("description", ""),
                            "type": p["range"],
                            "type_category": p["range_type"],
                        }
                        for p in props
                    ]
                    facets_expanded += 1
                elif cand["type"] == "observable" and cand.get("facets"):
                    # Expand facets of observable candidates too
                    for f_info in cand["facets"]:
                        if facets_expanded < 3 and f_info["property_count"] > 0:
                            props = _ontology.get_facet_properties(f_info["facet"])
                            f_info["properties"] = [
                                {
                                    "name": p["name"],
                                    "local_name": p["local_name"],
                                    "description": p.get("description", ""),
                                    "type": p["range"],
                                    "type_category": p["range_type"],
                                }
                                for p in props
                            ]
                            facets_expanded += 1
                expanded.append(cand)

            result["ontology_candidates"] = expanded
            result["candidates_note"] = (
                "Possible CASE/UCO matches found via keyword search. "
                "Properties are listed with descriptions — use them to map "
                "CSV columns to official CASE/UCO properties. Columns that "
                "don't match any listed property become ioi-ext: extensions. "
                "Include matched properties in your column_mapping when calling "
                "generate_all_rows."
            )

    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def _build_facet_details(facet_names: list[str]) -> list[dict]:
    """Build rich facet details with SHACL properties and descriptions."""
    facet_details = []
    for facet_name in facet_names:
        if facet_name.startswith("ioi-ext:"):
            facet_details.append({
                "facet": facet_name,
                "tier": "extension",
                "note": "Extension facet — properties generated from your CSV columns.",
                "properties": [],
            })
        else:
            props = _ontology.get_facet_properties(facet_name)
            # Determine prefix
            uri = _ontology.get_facet_uri(facet_name)
            if uri and "observable/" in uri:
                prefixed = f"uco-observable:{facet_name}"
            elif uri and "core/" in uri:
                prefixed = f"uco-core:{facet_name}"
            else:
                prefixed = facet_name

            facet_details.append({
                "facet": prefixed,
                "tier": "official",
                "property_count": len(props),
                "properties": [
                    {
                        "name": p["name"],
                        "description": p.get("description", ""),
                        "label": p.get("label", p["local_name"]),
                        "type": p["range"],
                        "type_category": p["range_type"],
                        "max_count": p["max_count"],
                        "is_array": p["is_array"],
                    }
                    for p in props
                ],
            })
    return facet_details


def _handle_analyze_csv(args: dict) -> list[TextContent]:
    """
    Analyze CSV and return rich column metadata for the LLM to reason over.
    """
    csv_path = args["csv_path"]

    if not Path(csv_path).exists():
        return [TextContent(
            type="text",
            text=json.dumps({"error": f"CSV file not found: {csv_path}"}),
        )]

    columns = _analyze_csv(csv_path)

    result = {
        "csv_path": csv_path,
        "column_count": len(columns),
        "columns": [
            {
                "header": col["column_name"],
                "inferred_type": col["inferred_type"],
                "sample_values": col["sample_values"],
                "non_null_count": col["non_null_count"],
                "total_rows_sampled": col["total_rows"],
            }
            for col in columns
        ],
        "next_step": (
            "Match each CSV column to a CASE/UCO property from resolve_artifact results. "
            "Use the property descriptions and xsd types to find the best match. "
            "Columns without a good match will become ioi-ext: extension properties."
        ),
    }

    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def _handle_generation_context(args: dict) -> list[TextContent]:
    """
    Return full constraint context for LLM-driven JSON-LD generation.
    Claude uses this to generate the JSON-LD itself — no deterministic templates.
    """
    artifact_name = args["artifact_name"]
    csv_path = args["csv_path"]
    column_mapping = args.get("column_mapping", {})
    description = args.get("description")

    if not Path(csv_path).exists():
        return [TextContent(
            type="text",
            text=json.dumps({"error": f"CSV file not found: {csv_path}"}),
        )]

    # Ontology-only resolution
    candidates = [artifact_name, f"Windows{artifact_name}", artifact_name.replace(" ", "")]
    obs_class = None
    obs_class_name = None
    for c in candidates:
        if _ontology.observable_exists(c):
            uri = _ontology.get_observable_uri(c)
            obs_class_name = uri.split("/")[-1] if uri else c
            obs_class = f"uco-observable:{obs_class_name}"
            break

    # Determine facets
    facet_names = []
    if obs_class_name:
        # Check for matching facet
        for fc in [f"{obs_class_name}Facet", f"{artifact_name}Facet", f"Windows{artifact_name}Facet"]:
            if _ontology.facet_exists(fc):
                uri = _ontology.get_facet_uri(fc)
                facet_names.append(uri.split("/")[-1] if uri else fc)
                break
        facet_names.extend(["FileFacet", "ContentDataFacet"])
    else:
        obs_class = "uco-observable:ObservableObject"
        facet_names = ["FileFacet", "ContentDataFacet"]

    # Build the full generation context
    gen_ctx = build_generation_context(
        ontology=_ontology,
        artifact_name=artifact_name,
        uco_class=obs_class,
        facet_names=facet_names,
        csv_path=csv_path,
        column_mapping=column_mapping,
        description=description,
    )

    # Save turtle patch if generated
    if gen_ctx.get("turtle_patch"):
        output_dir = Path(csv_path).parent
        ttl_out = output_dir / f"{artifact_name.lower()}_ext.ttl"
        with open(ttl_out, "w") as f:
            f.write(gen_ctx["turtle_patch"])
        gen_ctx["turtle_path"] = str(ttl_out)

    return [TextContent(type="text", text=json.dumps(gen_ctx, indent=2, default=str))]


def _handle_generate_all_rows(args: dict) -> list[TextContent]:
    """
    Deterministic batch generation: one ObservableObject per CSV row.
    Claude provides the column mapping, this tool does the serialization.
    """
    artifact_name = args["artifact_name"]
    csv_path = args["csv_path"]
    column_mapping = args.get("column_mapping", {})
    description = args.get("description")

    if not Path(csv_path).exists():
        return [TextContent(
            type="text",
            text=json.dumps({"error": f"CSV file not found: {csv_path}"}),
        )]

    result = generate_all_rows(
        ontology=_ontology,
        artifact_name=artifact_name,
        csv_path=csv_path,
        column_mapping=column_mapping,
        description=description,
    )

    # Auto-validate the output
    validation = _validator.validate_jsonld(
        result["jsonld"],
        turtle_patch=result.get("turtle_patch"),
    )

    # Return summary (not the full graph — it could be huge)
    summary = {
        "success": True,
        "artifact_name": artifact_name,
        "row_count": result["row_count"],
        "jsonld_path": result["jsonld_path"],
        "turtle_path": result.get("turtle_path"),
        "mapped_columns": result["mapped_columns"],
        "unmapped_columns": result["unmapped_columns"],
        "validation": validation.to_dict(),
    }

    return [TextContent(type="text", text=json.dumps(summary, indent=2, default=str))]


def _handle_generate_csv(args: dict) -> list[TextContent]:
    """Auto-generate without manual mapping (extension path)."""
    artifact_name = args["artifact_name"]
    csv_path = args["csv_path"]
    description = args.get("description")

    if not Path(csv_path).exists():
        return [TextContent(
            type="text",
            text=json.dumps({"error": f"CSV file not found: {csv_path}"}),
        )]

    # Note: generate_from_csv uses the old graph_builder (kept for backward compat)
    # Prefer generate_all_rows for production use
    result = _builder.build_from_csv(artifact_name, csv_path, description)

    validation = _validator.validate_jsonld(
        result["jsonld"],
        turtle_patch=result.get("turtle_patch"),
    )
    result["validation"] = validation.to_dict()

    output_dir = Path(csv_path).parent
    jsonld_out = output_dir / f"{artifact_name.lower()}_template.jsonld"
    with open(jsonld_out, "w") as f:
        json.dump(result["jsonld"], f, indent=2)
    result["jsonld_path"] = str(jsonld_out)

    if result.get("turtle_patch"):
        ttl_out = output_dir / f"{artifact_name.lower()}_ext.ttl"
        with open(ttl_out, "w") as f:
            f.write(result["turtle_patch"])
        result["turtle_path"] = str(ttl_out)

    summary = {k: v for k, v in result.items() if k != "jsonld"}
    return [TextContent(type="text", text=json.dumps(summary, indent=2, default=str))]


def _handle_facet_properties(args: dict) -> list[TextContent]:
    facet_name = args["facet_name"]
    props = _ontology.get_facet_properties(facet_name)

    result = {
        "facet": facet_name,
        "exists": _ontology.facet_exists(facet_name),
        "property_count": len(props),
        "properties": [
            {
                "name": p["name"],
                "label": p.get("label", ""),
                "description": p.get("description", ""),
                "type": p["range"],
                "type_category": p["range_type"],
                "max_count": p["max_count"],
                "is_array": p["is_array"],
            }
            for p in props
        ],
    }

    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def _handle_validate(args: dict) -> list[TextContent]:
    jsonld_path = args["jsonld_path"]
    ttl_path = args.get("turtle_patch_path")

    if not Path(jsonld_path).exists():
        return [TextContent(
            type="text",
            text=json.dumps({"error": f"File not found: {jsonld_path}"}),
        )]

    with open(jsonld_path, "r") as f:
        jsonld = json.load(f)

    turtle_patch = None
    if ttl_path and Path(ttl_path).exists():
        with open(ttl_path, "r") as f:
            turtle_patch = f.read()

    result = _validator.validate_jsonld(jsonld, turtle_patch)
    return [TextContent(type="text", text=json.dumps(result.to_dict(), indent=2))]




# ─── Entry point ─────────────────────────────────────────────────────

async def run():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def main():
    import asyncio
    asyncio.run(run())


if __name__ == "__main__":
    main()


# ─── New contribution workflow tools ─────────────────────────────

def _handle_scaffold_case(args: dict) -> list[TextContent]:
    """Assemble a complete CASES/AF-NEW/ directory for a PR."""
    result = _scaffold_case(
        output_dir=args["output_dir"],
        case_id=args.get("case_id", "AF-NEW"),
        title=args["title"],
        summary=args["summary"],
        artifacts=args["artifacts"],
        contributor=args.get("contributor", "community"),
        category=args.get("category", "temporal"),
        inconsistency_description=args.get("inconsistency_description", ""),
    )
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def _handle_sparql_context(args: dict) -> list[TextContent]:
    """Extract property IRIs from graphs for SPARQL rule writing."""
    result = extract_sparql_context(
        graphs=args["graphs"],
        contradiction_description=args.get("contradiction_description", ""),
        category=args.get("category", "temporal"),
    )
    return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]


def _handle_test_graph(args: dict) -> list[TextContent]:
    """Generate a synthetic test graph with specific values."""
    graph = _generate_test_graph(
        artifact_name=args["artifact_name"],
        graph_iri=args["graph_iri"],
        synthetic_values=args["synthetic_values"],
    )
    # Save to file
    output_path = args.get("output_path", f"/tmp/{args['artifact_name'].lower()}_test.jsonld")
    with open(output_path, "w") as f:
        json.dump(graph, f, indent=2)
    return [TextContent(type="text", text=json.dumps({
        "success": True,
        "output_path": output_path,
        "node_count": len(graph.get("@graph", [])),
    }, indent=2))]


def _handle_test_rule(args: dict) -> list[TextContent]:
    """Load graphs into rdflib Dataset and execute a SPARQL rule."""
    result = _test_rule(
        rule_path=args["rule_path"],
        graphs=args["graphs"],
    )
    return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]
