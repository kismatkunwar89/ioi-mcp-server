"""
IOI Framework MCP Server
Scalable, case-agnostic CASE/UCO JSON-LD artifact graph generator.

Tools (13 total):
  Core generation:
    identify_artifact     — CSV → artifact type (header matching against 85 artifacts)
    resolve_artifact      — Artifact → SHACL properties + registry canonical mapping
    analyze_csv           — CSV column analysis: headers, sample values, inferred types
    get_generation_context — Full constraints for LLM-driven JSON-LD generation
    generate_from_csv     — Auto-generate (no mapping needed, extension artifacts)
    get_facet_properties  — SHACL property extraction for any Facet
    generate_all_rows     — Deterministic batch: registry path or ontology fallback
    validate_graph        — Full IRI + SHACL validation

  New artifact:
    generate_instantiator — CSV → Python instantiator + templates + registry entry

  Contribution workflow:
    scaffold_case         — CASES/AF-NEW/ + SPARQL rule skeleton for a PR
    draft_sparql_context  — Extract property IRIs from graphs for SPARQL rule writing
    generate_test_graph   — Synthetic test graph for SPARQL rule validation
    test_rule             — Execute SPARQL .rq against rdflib Dataset (no Virtuoso)

Flow — known artifact (registry hit):
  identify_artifact → resolve_artifact → analyze_csv → generate_all_rows
  → validate_graph → draft_sparql_context → test_rule → scaffold_case

Flow — new artifact (registry miss):
  identify_artifact → resolve_artifact → generate_instantiator
  → [registry now populated] → generate_all_rows → validate_graph → scaffold_case
"""

import json
import os
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    Tool, TextContent,
    Prompt, PromptArgument, GetPromptResult, PromptMessage,
    Resource, ResourceTemplate, ReadResourceResult, TextResourceContents,
)

from ioi_mcp.ontology_loader import OntologyLoader
from ioi_mcp.manifest import ManifestRegistry
from ioi_mcp.constraint_builder import build_generation_context
from ioi_mcp.batch_generator import generate_all_rows as _generate_all_rows
from ioi_mcp.validator import Validator
from ioi_mcp.scaffold_case import scaffold_case as _scaffold_case
from ioi_mcp.sparql_context import extract_sparql_context
from ioi_mcp.test_rule import test_rule as _test_rule, generate_test_graph as _generate_test_graph
from ioi_mcp.type_inferencer import analyze_csv as _analyze_csv
from ioi_mcp.instantiator_gen import generate_instantiator as _generate_instantiator

# ── Workflow documentation served as MCP Resources ───────────────────────────
_WORKFLOW_ARTIFACT = """\
# Artifact Workflow Guide

## When to use
Load when working with any forensic artifact CSV.

## Steps

### Step 1 — identify_artifact (if artifact name unknown)
Call: identify_artifact(csv_path)
Returns: ranked artifact matches with confidence scores.

### Step 2 — resolve_artifact
Call: resolve_artifact(artifact_name)
- Registry hit  → canonical_field_types + facet returned. Skip to Step 4.
- Registry miss → extension path. Run generate_instantiator first.

### Step 3 — analyze_csv
Call: analyze_csv(csv_path)
Verify columns against canonical_field_types from Step 2.

### Step 4 — generate_all_rows
Call: generate_all_rows(artifact_name, csv_path, column_mapping={})
For registry artifacts: uses canonical field_types + provenance structure.

### Step 5 — validate_graph (MANDATORY)
Call: validate_graph(jsonld_path)
✓ valid → proceed   ✗ invalid → fix column_mapping, re-run Step 4

### Step 6 — Next
→ To write a SPARQL rule: see workflow://sparql
→ To contribute a PR:     see workflow://contribution
"""

_WORKFLOW_SPARQL = """\
# SPARQL Rule Writing Guide

## Prerequisites
At least one validated JSON-LD graph on disk.

## Steps

### Step 1 — draft_sparql_context
Call: draft_sparql_context(graphs=[{name, graph_path, graph_iri}])
graph_iri format: https://ioi-framework.github.io/cases/{case_id}/graphs/{artifact_lower}
Returns: prefixes, properties_used, join_candidates, SPARQL skeleton.

### Step 2 — Write the .rq rule file
Path: RULES/{temporal|structural|semantic}/IOI-NNN_{description}.rq
Must include version header block (rule_id, version, status, category, etc.)
Use join_candidates to cross-join artifact graphs.

### Step 3 — generate_test_graph (positive test)
Call: generate_test_graph(artifact_name, graph_iri, synthetic_values)
synthetic_values: designed to TRIGGER the rule.

### Step 4 — test_rule
Call: test_rule(rule_path, graphs=[{graph_iri, graph_path}])
✓ fired=true  → rule works
✗ fired=false → check FILTER logic, fix rule, re-run

### Step 5 — Negative test
Repeat Step 3 with values that should NOT trigger the rule.
test_rule again → fired must be false.
"""

_WORKFLOW_CONTRIBUTION = """\
# Contribution Workflow

## Prerequisites
validate_graph passed + test_rule fired=true

## Steps

### Step 1 — scaffold_case
Call: scaffold_case(output_dir, case_id, title, summary, artifacts, category)
Produces:
  CASES/AF-NEW/ground_truth.md  (filled from your inputs)
  CASES/AF-NEW/mapping.md       (filled from column_mapping)
  CASES/AF-NEW/test/*.jsonld    (correct graph IRIs)
  CASES/AF-NEW/snippets/        (3-row previews)
  RULES/{category}/IOI-NEW_*.rq (skeleton with version header)

### Step 2 — Verify
- mapping.md: field table must be filled (not placeholder text)
- test/*.jsonld: graph IRI = https://ioi-framework.github.io/cases/{case_id}/graphs/{artifact}
- RULES/*.rq: version header must have all required fields

### Step 3 — Open PR
Branch: add/{case-name}
Files: CASES/AF-NNN/ + RULES/{category}/
"""

_WORKFLOW_NEW_ARTIFACT = """\
# New Artifact Workflow

## When to use
resolve_artifact returned source != registry (artifact not yet registered).

## Steps

### Step 1 — generate_instantiator
Call: generate_instantiator(artifact_name, csv_path)
Produces:
  instantiators/{name}_instantiator.py
  instantiators/templates/{name}/ (4 JSON files)
  Turtle patch for ioi-ext.ttl
  Registry entry appended to data/ioi_registry.json

### Step 2 — Follow the known-artifact flow
resolve_artifact(artifact_name) now returns a registry hit.
Continue from Step 4 of workflow://artifact.

### Step 3 — PR the new artifact to IoI-Framework
Files: instantiators/ + registry.json + ontologies/ioi-ext.ttl
"""

# ── Server init ───────────────────────────────────────────────────────────────
app = Server("ioi-mcp-server")

_ontology: OntologyLoader | None = None
_registry: ManifestRegistry | None = None
_validator: Validator | None = None


def _init():
    global _ontology, _registry, _validator
    if _ontology is None:
        _registry  = ManifestRegistry()
        _ontology  = OntologyLoader(extra_ttl=os.environ.get("IOI_EXT_TTL"))
        _validator = Validator(_ontology)
        # M-11b: warn if bundled registry is behind live framework
        sync_warn = _registry.check_sync()
        if sync_warn:
            print(sync_warn, file=sys.stderr)


# ── MCP Prompts (M-12) ────────────────────────────────────────────────────────
@app.list_prompts()
async def list_prompts() -> list[Prompt]:
    return [
        Prompt(
            name="work-on-artifact",
            description="Start the artifact → JSON-LD → validation workflow for a forensic CSV",
            arguments=[
                PromptArgument(name="artifact_name", description="e.g. MFT, EVTX, Prefetch", required=False),
                PromptArgument(name="csv_path",      description="Path to the CSV file",        required=False),
            ],
        ),
        Prompt(
            name="write-sparql-rule",
            description="Draft and test a SPARQL IoI detection rule for a contradiction",
            arguments=[
                PromptArgument(name="contradiction", description="Describe the anti-forensic contradiction", required=False),
                PromptArgument(name="category",      description="temporal | structural | semantic",          required=False),
            ],
        ),
        Prompt(
            name="contribute-case",
            description="Package a completed case + rule for an IoI-Framework PR",
            arguments=[
                PromptArgument(name="case_title",   description="Short title for the case",        required=False),
                PromptArgument(name="contributor",  description="Your GitHub handle",              required=False),
            ],
        ),
        Prompt(
            name="new-artifact",
            description="Register a new artifact type not yet in the IoI Framework",
            arguments=[
                PromptArgument(name="artifact_name", description="e.g. Prefetch, Amcache, SRUM", required=False),
                PromptArgument(name="csv_path",      description="Path to sample CSV",            required=False),
            ],
        ),
    ]


@app.get_prompt()
async def get_prompt(name: str, arguments: dict) -> GetPromptResult:
    _init()

    if name == "work-on-artifact":
        artifact = arguments.get("artifact_name", "[artifact name]")
        csv_path = arguments.get("csv_path", "[csv path]")
        reg      = _registry.resolve(artifact) if artifact != "[artifact name]" else None
        status   = "Registry hit — canonical field_types available." if reg else \
                   "Not in registry — run generate_instantiator first OR use extension path."
        text = f"""IOI Framework — Artifact Workflow

Artifact: {artifact}
CSV: {csv_path}
Registry status: {status}

Step 1: resolve_artifact("{artifact}")
Step 2: analyze_csv("{csv_path}")
Step 3: generate_all_rows("{artifact}", "{csv_path}", column_mapping={{}})
Step 4: validate_graph(<jsonld_path>)  ← MANDATORY before proceeding
Step 5: draft_sparql_context or scaffold_case

Invariants:
- validate_graph must pass before scaffold_case
- test_rule must fire before scaffold_case
- All graph IRIs: https://ioi-framework.github.io/cases/{{case_id}}/graphs/{artifact.lower()}
"""

    elif name == "write-sparql-rule":
        contradiction = arguments.get("contradiction", "[describe the contradiction]")
        category      = arguments.get("category", "temporal")
        text = f"""IOI Framework — SPARQL Rule Workflow

Contradiction: {contradiction}
Category: {category}

Step 1: draft_sparql_context(graphs=[{{...}}], contradiction_description="{contradiction}", category="{category}")
Step 2: Write RULES/{category}/IOI-NNN_name.rq with version header
Step 3: generate_test_graph — values that TRIGGER the rule
Step 4: test_rule → fired must be true
Step 5: Repeat with values that should NOT fire → fired must be false

Graph IRI format: https://ioi-framework.github.io/cases/{{case_id}}/graphs/{{artifact_lower}}
Rule must include: rule_id, version, status, category, title, invariant, artifacts, added
"""

    elif name == "contribute-case":
        title       = arguments.get("case_title", "[case title]")
        contributor = arguments.get("contributor", "[your handle]")
        text = f"""IOI Framework — Contribution Workflow

Case: {title}
Contributor: {contributor}

Prerequisites: validate_graph passed + test_rule fired=true

Step 1: scaffold_case(output_dir, case_id="AF-NEW", title="{title}", ...)
Step 2: Verify mapping.md is filled (not placeholder text)
Step 3: Verify test/*.jsonld uses correct graph IRIs
Step 4: PR to IoI-Framework: CASES/AF-NEW/ + RULES/{{category}}/
Branch naming: add/{{short-name}}
"""

    elif name == "new-artifact":
        artifact = arguments.get("artifact_name", "[artifact name]")
        csv_path = arguments.get("csv_path", "[csv path]")
        text = f"""IOI Framework — New Artifact Registration

Artifact: {artifact}
CSV: {csv_path}

Step 1: generate_instantiator("{artifact}", "{csv_path}")
  → Generates: instantiator.py + 4 templates + Turtle patch + registry entry

Step 2: resolve_artifact("{artifact}")
  → Now returns registry hit with canonical field_types

Step 3: Follow the standard artifact workflow (work-on-artifact prompt)

Step 4: PR generated files to IoI-Framework:
  - instantiators/{artifact.lower()}_instantiator.py
  - instantiators/templates/{artifact.lower()}/
  - ontologies/ioi-ext.ttl (append Turtle patch)
  - registry.json (new entry)
"""
    else:
        text = f"Unknown prompt: {name}"

    return GetPromptResult(
        messages=[PromptMessage(role="user", content=TextContent(type="text", text=text))]
    )


# ── MCP Resources (M-13) ──────────────────────────────────────────────────────
@app.list_resources()
async def list_resources() -> list[Resource]:
    _init()
    artifacts = _registry.list_all()
    base = [
        Resource(uri="workflow://artifact",     name="Artifact Workflow Guide",   mimeType="text/markdown"),
        Resource(uri="workflow://sparql",        name="SPARQL Rule Writing Guide", mimeType="text/markdown"),
        Resource(uri="workflow://contribution",  name="Contribution Checklist",    mimeType="text/markdown"),
        Resource(uri="workflow://new-artifact",  name="New Artifact Guide",        mimeType="text/markdown"),
        Resource(uri="registry://artifacts",     name="IoI Artifact Registry",     mimeType="application/json",
                 description=f"{len(artifacts)} registered artifacts"),
    ]
    artifact_resources = [
        Resource(
            uri=f"registry://artifact/{a['canonical_name']}",
            name=f"Artifact: {a['canonical_name']}",
            mimeType="application/json",
            description=f"facet={a.get('facet')} | cases={a.get('cases')}",
        )
        for a in artifacts
    ]
    return base + artifact_resources


@app.read_resource()
async def read_resource(uri: str) -> ReadResourceResult:
    _init()

    if uri == "workflow://artifact":
        text = _WORKFLOW_ARTIFACT
    elif uri == "workflow://sparql":
        text = _WORKFLOW_SPARQL
    elif uri == "workflow://contribution":
        text = _WORKFLOW_CONTRIBUTION
    elif uri == "workflow://new-artifact":
        text = _WORKFLOW_NEW_ARTIFACT
    elif uri == "registry://artifacts":
        text = json.dumps({"artifacts": {a["canonical_name"]: a for a in _registry.list_all()}}, indent=2)
    elif uri.startswith("registry://artifact/"):
        name  = uri.split("/")[-1]
        entry = _registry.resolve(name)
        text  = json.dumps(entry, indent=2) if entry else json.dumps({"error": f"Not found: {name}"})
    else:
        text = f"Unknown resource: {uri}"

    return ReadResourceResult(
        contents=[TextResourceContents(uri=uri, text=text,
                                       mimeType="application/json" if "registry" in uri else "text/markdown")]
    )


# ── Tool definitions ──────────────────────────────────────────────────────────
@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="identify_artifact",
            description=(
                "Identify the forensic artifact type from a CSV file automatically. "
                "Matches column headers against 85 known artifact signatures. "
                "Use FIRST when the user provides a CSV without specifying artifact type. "
                "Returns ranked matches with confidence. Then call resolve_artifact with top match."
            ),
            inputSchema={"type":"object","properties":{"csv_path":{"type":"string"}},"required":["csv_path"]},
        ),
        Tool(
            name="resolve_artifact",
            description=(
                "Look up a forensic artifact. Checks registry first (canonical field_types + facet), "
                "then ioi-ext.ttl, then CASE/UCO ontology, then extension path. "
                "Registry hit: returns canonical_field_types — no re-derivation needed. "
                "Use FIRST before any generation step."
            ),
            inputSchema={"type":"object","properties":{"artifact_name":{"type":"string"}},"required":["artifact_name"]},
        ),
        Tool(
            name="analyze_csv",
            description=(
                "Analyze a CSV file: column headers, inferred xsd types, sample values. "
                "Use SECOND after resolve_artifact to verify columns match canonical_field_types."
            ),
            inputSchema={"type":"object","properties":{"csv_path":{"type":"string"}},"required":["csv_path"]},
        ),
        Tool(
            name="get_generation_context",
            description="Get full SHACL constraint context for LLM-driven JSON-LD generation.",
            inputSchema={
                "type":"object",
                "properties":{
                    "artifact_name":{"type":"string"},
                    "csv_path":{"type":"string"},
                    "column_mapping":{"type":"object"},
                    "description":{"type":"string"},
                },
                "required":["artifact_name","csv_path","column_mapping"],
            },
        ),
        Tool(
            name="generate_from_csv",
            description=(
                "Auto-generate CASE/UCO JSON-LD WITHOUT manual mapping. "
                "Best for unknown/extension artifacts. "
                "For registry artifacts, prefer generate_all_rows with column_mapping."
            ),
            inputSchema={
                "type":"object",
                "properties":{
                    "artifact_name":{"type":"string"},
                    "csv_path":{"type":"string"},
                    "description":{"type":"string"},
                },
                "required":["artifact_name","csv_path"],
            },
        ),
        Tool(
            name="get_facet_properties",
            description="Get all SHACL-defined properties for a specific CASE/UCO Facet.",
            inputSchema={"type":"object","properties":{"facet_name":{"type":"string"}},"required":["facet_name"]},
        ),
        Tool(
            name="generate_all_rows",
            description=(
                "Generate a complete CASE/UCO JSON-LD knowledge graph from ALL rows in a CSV. "
                "For registry artifacts: uses canonical field_types + emits provenance structure. "
                "For new artifacts: uses ontology fallback. "
                "ALWAYS run validate_graph after this."
            ),
            inputSchema={
                "type":"object",
                "properties":{
                    "artifact_name":{"type":"string"},
                    "csv_path":{"type":"string"},
                    "column_mapping":{"type":"object"},
                    "description":{"type":"string"},
                },
                "required":["artifact_name","csv_path","column_mapping"],
            },
        ),
        Tool(
            name="validate_graph",
            description=(
                "Validate a CASE/UCO JSON-LD file. MANDATORY after generate_all_rows. "
                "Checks: IRI resolution, @id format, @context, rdflib parseability, SHACL."
            ),
            inputSchema={
                "type":"object",
                "properties":{
                    "jsonld_path":{"type":"string"},
                    "turtle_patch_path":{"type":"string"},
                },
                "required":["jsonld_path"],
            },
        ),
        Tool(
            name="generate_instantiator",
            description=(
                "Generate everything needed to register a NEW artifact in the IoI Framework. "
                "Use when resolve_artifact returns source != registry. "
                "Produces: Python instantiator script, 4 template JSON files, "
                "Turtle patch for ioi-ext.ttl, and registry entry. "
                "After this, resolve_artifact will return a registry hit."
            ),
            inputSchema={
                "type":"object",
                "properties":{
                    "artifact_name":{"type":"string","description":"e.g. 'Prefetch', 'SRUM', 'Amcache'"},
                    "csv_path":{"type":"string","description":"Path to sample CSV from the artifact parser"},
                    "output_base_dir":{"type":"string","description":"Where to write files (defaults to IOI_FRAMEWORK_PATH or /tmp/)"},
                },
                "required":["artifact_name","csv_path"],
            },
        ),
        Tool(
            name="scaffold_case",
            description=(
                "Assemble a complete CASES/AF-NEW/ directory + SPARQL rule skeleton for a PR. "
                "Requires pre-validated graphs + fired test_rule. "
                "Produces: ground_truth.md, mapping.md, snippets/, test/, rule .rq skeleton."
            ),
            inputSchema={
                "type":"object",
                "properties":{
                    "output_dir":{"type":"string"},
                    "case_id":{"type":"string"},
                    "title":{"type":"string"},
                    "summary":{"type":"string"},
                    "artifacts":{"type":"array","items":{"type":"object"}},
                    "contributor":{"type":"string"},
                    "category":{"type":"string","enum":["temporal","structural","semantic"]},
                    "inconsistency_description":{"type":"string"},
                },
                "required":["output_dir","title","summary","artifacts"],
            },
        ),
        Tool(
            name="draft_sparql_context",
            description=(
                "Extract property IRIs from JSON-LD graphs for writing SPARQL detection rules. "
                "Returns: prefixes, types_used, properties_used, join_candidates, SPARQL skeleton. "
                "Use BEFORE writing a .rq rule file."
            ),
            inputSchema={
                "type":"object",
                "properties":{
                    "graphs":{"type":"array","items":{"type":"object"}},
                    "contradiction_description":{"type":"string"},
                    "category":{"type":"string","enum":["temporal","structural","semantic"]},
                },
                "required":["graphs"],
            },
        ),
        Tool(
            name="generate_test_graph",
            description=(
                "Generate a minimal synthetic JSON-LD test graph with specific values "
                "designed to make a SPARQL rule fire (positive test) or not fire (negative test)."
            ),
            inputSchema={
                "type":"object",
                "properties":{
                    "artifact_name":{"type":"string"},
                    "graph_iri":{"type":"string","description":"e.g. 'https://ioi-framework.github.io/cases/AF-004/graphs/mft'"},
                    "synthetic_values":{"type":"object"},
                    "output_path":{"type":"string"},
                },
                "required":["artifact_name","graph_iri","synthetic_values"],
            },
        ),
        Tool(
            name="test_rule",
            description=(
                "Load JSON-LD graphs into rdflib Dataset as named graphs and execute a SPARQL .rq rule. "
                "Returns: row_count, columns, rows, fired (bool). "
                "fired=true = rule detected the contradiction. No Virtuoso needed."
            ),
            inputSchema={
                "type":"object",
                "properties":{
                    "rule_path":{"type":"string"},
                    "graphs":{"type":"array","items":{"type":"object"}},
                },
                "required":["rule_path","graphs"],
            },
        ),
    ]


# ── Tool handlers ─────────────────────────────────────────────────────────────
@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    _init()

    handlers = {
        "identify_artifact":   _handle_identify,
        "resolve_artifact":    _handle_resolve,
        "analyze_csv":         _handle_analyze_csv,
        "get_generation_context": _handle_generation_context,
        "generate_all_rows":   _handle_generate_all_rows,
        "generate_from_csv":   _handle_generate_csv,
        "get_facet_properties": _handle_facet_properties,
        "validate_graph":      _handle_validate,
        "generate_instantiator": _handle_generate_instantiator,
        "scaffold_case":       _handle_scaffold_case,
        "draft_sparql_context": _handle_sparql_context,
        "generate_test_graph": _handle_test_graph,
        "test_rule":           _handle_test_rule,
    }
    handler = handlers.get(name)
    if handler:
        return handler(arguments)
    return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]


# ── Handler implementations ───────────────────────────────────────────────────

def _handle_identify(args: dict) -> list[TextContent]:
    csv_path = args["csv_path"]
    if not Path(csv_path).exists():
        return [TextContent(type="text", text=json.dumps({"error": f"CSV not found: {csv_path}"}))]
    import csv as _csv
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        headers = [h.strip() for h in next(_csv.reader(f))]
    matches = _ontology.identify_artifact_from_headers(headers)
    result = {
        "csv_path":     csv_path,
        "column_count": len(headers),
        "matches":      matches,
        "flow_state": {
            "step_completed": "identify_artifact",
            "next": {
                "primary": f"resolve_artifact('{matches[0]['artifact']}') — top match" if matches else "resolve_artifact with artifact name",
                "if_wrong_match": "Call resolve_artifact with the correct artifact name",
            },
        },
    }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def _handle_resolve(args: dict) -> list[TextContent]:
    artifact_name = args["artifact_name"]

    # ── Registry first (M-2) ────────────────────────────────────────────────
    reg_entry = _registry.resolve(artifact_name)
    if reg_entry:
        result = {
            "found":                True,
            "source":               "registry",
            "artifact":             artifact_name,
            "canonical_name":       reg_entry["canonical_name"],
            "tier":                 "validated",
            "facet":                reg_entry.get("facet"),
            "canonical_field_types": reg_entry.get("field_types", {}),
            "file_facet_columns":   reg_entry.get("file_facet_columns", []),
            "cases":                reg_entry.get("cases", []),
            "rules":                reg_entry.get("rules", []),
            "template_dir":         reg_entry.get("template_dir"),
            "graph_iri_example":    _registry.make_graph_iri("AF-NEW", artifact_name),
            "flow_state": {
                "step_completed": "resolve_artifact",
                "path":           "known_artifact",
                "next": {
                    "primary":          f"analyze_csv(csv_path) — verify columns against canonical_field_types",
                    "skip_analyze_if":  "You already know the CSV columns — call generate_all_rows directly",
                },
                "invariant": "validate_graph must pass before scaffold_case",
            },
        }
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    # ── Ontology fallback ────────────────────────────────────────────────────
    candidates = [artifact_name, f"Windows{artifact_name}", artifact_name.replace(" ", "")]
    obs_class = None
    for c in candidates:
        if _ontology.observable_exists(c):
            uri = _ontology.get_observable_uri(c)
            obs_class = f"uco-observable:{uri.split('/')[-1]}" if uri else f"uco-observable:{c}"
            break

    facet_name = None
    for fc in [f"{artifact_name}Facet", f"Windows{artifact_name}Facet"]:
        if _ontology.facet_exists(fc):
            uri = _ontology.get_facet_uri(fc)
            facet_name = uri.split("/")[-1] if uri else fc
            break

    wiki_desc = _ontology.get_artifact_description(artifact_name)
    facet_details = _build_facet_details(["FileFacet", "ContentDataFacet"] +
                                         ([facet_name] if facet_name else []))

    result = {
        "found":        bool(obs_class or facet_name),
        "source":       "ontology" if obs_class else "extension",
        "artifact":     artifact_name,
        "tier":         "official" if obs_class else "extension",
        "uco_class":    obs_class or "uco-observable:ObservableObject",
        "artifact_description": wiki_desc,
        "facets":       facet_details,
        "flow_state": {
            "step_completed": "resolve_artifact",
            "path":           "new_artifact",
            "next": {
                "primary":     f"generate_instantiator('{artifact_name}', csv_path) — register artifact in registry",
                "alternative": f"generate_all_rows('{artifact_name}', csv_path, column_mapping={{}}) — proceed without registration (extension path)",
            },
            "note": "After generate_instantiator, resolve_artifact will return a registry hit",
        },
    }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def _build_facet_details(facet_names: list) -> list:
    details = []
    for fn in facet_names:
        props = _ontology.get_facet_properties(fn)
        if props:
            details.append({
                "facet":      fn,
                "properties": [{"name": p["name"], "local_name": p["local_name"],
                                 "description": p.get("description","")[:100],
                                 "type": p["range"]} for p in props],
            })
    return details


def _handle_analyze_csv(args: dict) -> list[TextContent]:
    csv_path = args["csv_path"]
    if not Path(csv_path).exists():
        return [TextContent(type="text", text=json.dumps({"error": f"CSV not found: {csv_path}"}))]
    columns = _analyze_csv(csv_path)
    result = {
        "csv_path":     csv_path,
        "column_count": len(columns),
        "columns":      columns,
        "flow_state": {
            "step_completed": "analyze_csv",
            "next": {
                "primary": "generate_all_rows(artifact_name, csv_path, column_mapping) — use canonical_field_types from resolve_artifact as the mapping guide",
            },
        },
    }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def _handle_generation_context(args: dict) -> list[TextContent]:
    artifact_name  = args["artifact_name"]
    csv_path       = args["csv_path"]
    column_mapping = args["column_mapping"]
    description    = args.get("description", "")
    if not Path(csv_path).exists():
        return [TextContent(type="text", text=json.dumps({"error": f"CSV not found: {csv_path}"}))]
    ctx = build_generation_context(_ontology, artifact_name, csv_path, column_mapping, description)
    ctx["flow_state"] = {
        "step_completed": "get_generation_context",
        "next": {"primary": "generate_all_rows with your column_mapping, then validate_graph"},
    }
    return [TextContent(type="text", text=json.dumps(ctx, indent=2))]


def _handle_generate_all_rows(args: dict) -> list[TextContent]:
    artifact_name  = args["artifact_name"]
    csv_path       = args["csv_path"]
    column_mapping = args.get("column_mapping", {})
    description    = args.get("description")
    if not Path(csv_path).exists():
        return [TextContent(type="text", text=json.dumps({"error": f"CSV not found: {csv_path}"}))]
    reg_entry = _registry.resolve(artifact_name)
    result = _generate_all_rows(
        _ontology, artifact_name, csv_path, column_mapping,
        description=description, registry_entry=reg_entry,
    )
    # Remove the full jsonld from response (too large) — path is sufficient
    result.pop("jsonld", None)
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def _handle_generate_csv(args: dict) -> list[TextContent]:
    """generate_from_csv — calls generate_all_rows with empty column_mapping."""
    artifact_name = args["artifact_name"]
    csv_path      = args["csv_path"]
    description   = args.get("description")
    if not Path(csv_path).exists():
        return [TextContent(type="text", text=json.dumps({"error": f"CSV not found: {csv_path}"}))]
    reg_entry = _registry.resolve(artifact_name)
    result = _generate_all_rows(
        _ontology, artifact_name, csv_path, {},
        description=description, registry_entry=reg_entry,
    )
    result.pop("jsonld", None)
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def _handle_facet_properties(args: dict) -> list[TextContent]:
    facet_name = args["facet_name"]
    props = _ontology.get_facet_properties(facet_name)
    result = {
        "facet_name": facet_name,
        "property_count": len(props),
        "properties": props,
    }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def _handle_validate(args: dict) -> list[TextContent]:
    jsonld_path       = args["jsonld_path"]
    turtle_patch_path = args.get("turtle_patch_path")
    if not Path(jsonld_path).exists():
        return [TextContent(type="text", text=json.dumps({"error": f"File not found: {jsonld_path}"}))]
    turtle_patch = None
    if turtle_patch_path and Path(turtle_patch_path).exists():
        turtle_patch = Path(turtle_patch_path).read_text()
    import json as _json
    with open(jsonld_path) as f:
        data = _json.load(f)
    v = _validator.validate_jsonld(data, turtle_patch)
    result = {
        "valid":    v.valid,
        "errors":   v.errors,
        "warnings": v.warnings,
        "flow_state": {
            "step_completed": "validate_graph",
            "next": {
                "if_valid":   "draft_sparql_context (to write a rule) OR scaffold_case (to contribute PR)",
                "if_invalid": "fix column_mapping in generate_all_rows, re-run, re-validate",
            },
            "invariant": "Rule must be written and test_rule must fire before scaffold_case",
        },
    }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def _handle_generate_instantiator(args: dict) -> list[TextContent]:
    artifact_name   = args["artifact_name"]
    csv_path        = args["csv_path"]
    output_base_dir = args.get("output_base_dir")
    if not Path(csv_path).exists():
        return [TextContent(type="text", text=json.dumps({"error": f"CSV not found: {csv_path}"}))]
    result = _generate_instantiator(artifact_name, csv_path, output_base_dir)
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def _handle_scaffold_case(args: dict) -> list[TextContent]:
    result = _scaffold_case(
        output_dir                 = args["output_dir"],
        case_id                    = args.get("case_id", "AF-NEW"),
        title                      = args["title"],
        summary                    = args["summary"],
        artifacts                  = args["artifacts"],
        contributor                = args.get("contributor", "contributor"),
        category                   = args.get("category", "temporal"),
        inconsistency_description  = args.get("inconsistency_description", ""),
    )
    result["flow_state"] = {
        "step_completed": "scaffold_case",
        "next": {
            "primary": "Open PR to IoI-Framework: CASES/AF-NEW/ + RULES/{category}/",
            "verify":  "Check mapping.md is filled, test/*.jsonld uses correct graph IRIs",
        },
    }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def _handle_sparql_context(args: dict) -> list[TextContent]:
    result = extract_sparql_context(
        graphs                    = args["graphs"],
        contradiction_description = args.get("contradiction_description", ""),
        category                  = args.get("category", "temporal"),
    )
    result["flow_state"] = {
        "step_completed": "draft_sparql_context",
        "next": {
            "primary": "Write RULES/{category}/IOI-NNN_name.rq using sparql_template as skeleton",
            "then":    "generate_test_graph → test_rule → confirm fired=true",
        },
    }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def _handle_test_graph(args: dict) -> list[TextContent]:
    result = _generate_test_graph(
        artifact_name   = args["artifact_name"],
        graph_iri       = args["graph_iri"],
        synthetic_values= args["synthetic_values"],
        context         = args.get("context"),
    )
    output_path = args.get("output_path", f"/tmp/{args['artifact_name'].lower()}_test.jsonld")
    import json as _json
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        _json.dump(result, f, indent=2)
    return [TextContent(type="text", text=json.dumps({
        "generated": True,
        "output_path": output_path,
        "graph_iri": args["graph_iri"],
        "node_count": len(result.get("@graph", [])),
        "flow_state": {
            "step_completed": "generate_test_graph",
            "next": {"primary": f"test_rule(rule_path, graphs=[{{graph_iri:'{args['graph_iri']}', graph_path:'{output_path}'}}])"},
        },
    }, indent=2))]


def _handle_test_rule(args: dict) -> list[TextContent]:
    result = _test_rule(rule_path=args["rule_path"], graphs=args["graphs"])
    if "flow_state" not in result:
        result["flow_state"] = {
            "step_completed": "test_rule",
            "next": {
                "if_fired":     "Rule works — run scaffold_case to package PR",
                "if_not_fired": "Rule did not fire — check FILTER logic in the .rq file, fix, re-run test_rule",
                "negative_test":"Also test with values that should NOT fire to avoid over-broad rules",
            },
        }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


# ── Entry point ───────────────────────────────────────────────────────────────
async def _run():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def main():
    import asyncio
    asyncio.run(_run())


if __name__ == "__main__":
    main()
