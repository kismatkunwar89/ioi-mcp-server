# IOI Framework MCP Server

Scalable, case-agnostic CASE/UCO JSON-LD artifact graph generator for the [IOI Framework](https://ioi-framework.github.io/).

## Purpose

Removes the ontology expertise barrier for contributors. Users provide a CSV (forensic tool output) and get a valid CASE/UCO JSON-LD knowledge graph back — no SPARQL, no ontology terms, no manual JSON-LD authoring.

## How It Works

The MCP server provides knowledge. Claude (or any MCP client) provides reasoning.

1. **resolve_artifact** — Queries CASE/UCO ontology + existing `ioi-ext.ttl` for properties with `rdfs:comment` descriptions
2. **analyze_csv** — Extracts column headers, infers xsd types from actual values, returns sample data
3. **get_generation_context** — Returns full constraint context (datatype, objectType, nodeKind, shape hints) for LLM-driven JSON-LD generation
4. **validate_graph** — IRI resolution + @id format + @context completeness + SHACL via `case_validate`
5. **list_artifacts** — Browse the manifest (52 artifacts from forensics.wiki)

## Architecture

- **Ontology-first resolution**: 171 ObservableObject classes + 149 Facets queried at runtime from CASE/UCO 1.4.0
- **Existing ioi-ext.ttl**: 5 extension facets (MFT, USN, LNK, EVTX, Office XML) with 19 curated properties loaded automatically
- **No duck typing**: Exact manifest lookup for known artifacts, clean extension generation for unknown ones
- **LLM-driven generation**: MCP provides constraints, Claude generates JSON-LD (no hardcoded templates)

## Coverage

| Source | Artifacts | Behavior |
|--------|-----------|----------|
| CASE/UCO ontology | 29 (Prefetch, Registry, EVTX, etc.) | Official class + SHACL properties |
| Existing ioi-ext.ttl | 5 (MFT, USN, LNK, EVTX, Office XML) | Curated extension properties |
| Manifest (extension) | 18 (SRUM, Amcache, ShimCache, etc.) | Auto-generated ioi-ext terms |
| Unknown artifacts | Unlimited | Full extension generation + Turtle patch |

## Installation

```bash
git clone https://github.com/ioi-framework/ioi-mcp-server.git
cd ioi-mcp-server
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install case-utils rdflib "mcp>=1.0.0"
pip install -e .
```

Verify it works:

```bash
python -c "from ioi_mcp.ontology_loader import OntologyLoader; o = OntologyLoader(); print(f'Classes: {len(o._observable_index)}, Facets: {len(o._facet_index)}')"
# Expected: Classes: 171, Facets: 149
```

## Claude Code Integration

Add the server to Claude Code with one command:

```bash
claude mcp add ioi-mcp /path/to/ioi-mcp-server/.venv/bin/ioi-mcp
```

Replace `/path/to/ioi-mcp-server` with your actual clone path. Verify it registered:

```bash
claude mcp list
```

The server runs via stdio — no ports, no background process needed.

## Claude Desktop / Cursor Integration

Add to your MCP config (`~/.claude/claude_desktop_config.json` or `.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "ioi-mcp": {
      "command": "/path/to/ioi-mcp-server/.venv/bin/ioi-mcp"
    }
  }
}
```

Replace `/path/to/ioi-mcp-server` with your actual clone path.

## Usage (programmatic)

```python
from ioi_mcp.ontology_loader import OntologyLoader
from ioi_mcp.manifest import ManifestRegistry
from ioi_mcp.graph_builder import GraphBuilder
from ioi_mcp.validator import Validator

ont = OntologyLoader()
m = ManifestRegistry()
builder = GraphBuilder(ont, m)
validator = Validator(ont)

# Generate JSON-LD from any forensic tool CSV
result = builder.build_from_csv("SRUM", "path/to/srum.csv")

# Validate (IRI + SHACL)
v = validator.validate_jsonld(result["jsonld"], result.get("turtle_patch"))
print("Valid:", v.valid)

# Output files
import json
with open("srum.jsonld", "w") as f:
    json.dump(result["jsonld"], f, indent=2)
if result.get("turtle_patch"):
    with open("srum_ext.ttl", "w") as f:
        f.write(result["turtle_patch"])
```

## Contribution Alignment

Maps to the [IOI Framework contribution levels](https://ioi-framework.github.io/community/):

- **Level 1**: No change (plain English issues)
- **Level 2**: MCP generates JSON-LD templates + validates → contributor focuses on SPARQL rules
- **Level 3**: MCP replaces hand-written Python mappers for most artifacts

## Dependencies

- `case-utils` — Ships aggregated CASE/UCO .ttl, provides `case_validate`
- `rdflib` — Ontology loading + SPARQL queries
- `mcp` — MCP Python SDK (stdio transport)

No Docker. No Virtuoso. No network at runtime. Self-contained.
