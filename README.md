# IOI Framework MCP Server

Scalable, case-agnostic CASE/UCO JSON-LD artifact graph generator for the [IOI Framework](https://ioi-framework.github.io/).

## Purpose

Removes the ontology expertise barrier for contributors. Users provide a CSV (forensic tool output) and get a valid CASE/UCO JSON-LD knowledge graph back — no SPARQL, no ontology terms, no manual JSON-LD authoring.

## How It Works

The MCP server provides knowledge. Claude (or any MCP client) provides reasoning.

### Core Generation Tools (7)

| Tool | Description |
|------|-------------|
| `resolve_artifact` | Queries CASE/UCO ontology + `ioi-ext.ttl` + forensics.wiki descriptions for full context |
| `analyze_csv` | Extracts column headers, infers xsd types from actual values, returns sample data |
| `get_generation_context` | Returns full constraint context (datatype, objectType, nodeKind, shape hints) for LLM-driven JSON-LD generation |
| `generate_all_rows` | Deterministic batch generation: one ObservableObject per CSV row with typed literals |
| `generate_from_csv` | Auto-generate JSON-LD without manual mapping (extension artifacts) |
| `get_facet_properties` | SHACL property extraction for any Facet |
| `validate_graph` | IRI resolution + @id format + @context completeness + SHACL via `case_validate` |

### Contribution Workflow Tools (4)

| Tool | Description |
|------|-------------|
| `scaffold_case` | Assemble a complete `CASES/AF-NEW/` directory structure for an IoI contribution PR |
| `draft_sparql_context` | Extract property IRIs from JSON-LD graphs for writing SPARQL detection rules |
| `generate_test_graph` | Create synthetic test graphs with specific values for SPARQL rule validation |
| `test_rule` | Execute SPARQL `.rq` files against rdflib Dataset named graphs (no Virtuoso needed) |

## Architecture

- **Ontology-first resolution**: 171 ObservableObject classes + 149 Facets queried at runtime from CASE/UCO 1.4.0
- **Three-tier resolution**: ontology hit → `ioi-ext.ttl` hit → auto-generate extension
- **No duck typing**: Exact ontology lookup for known artifacts, clean extension generation for unknown ones
- **LLM-driven generation**: MCP provides constraints, Claude generates JSON-LD (no hardcoded templates)
- **Fully scalable**: No static artifact lists — any new artifact works through the extension path

## Coverage

85 forensic artifacts indexed across 7 categories:

| Category | Count | Examples |
|----------|-------|----------|
| Windows | 55 | MFT, EVTX, Prefetch, Registry, Amcache, ShimCache, SRUM, JumpList, LNK, WER, RDP Logs |
| macOS | 10 | FSEvents, KnowledgeC, Unified Logs, Launch Agents/Daemons, CrashReporter, Quarantine |
| Network | 6 | PCAP, ZeekLogs, DNS Cache, NetFlow, Proxy Logs, Network Connections |
| Cross-platform | 5 | Email Headers, EXIF, SQLite, OLE Compound, Office Metadata |
| Linux | 4 | Bash History, Crontab, Linux Logs, ext Filesystem |
| Mobile | 3 | iOS Backup, Android SQLite, SIM Card |
| Memory | 2 | Memory Dump, Hiberfil |

Any artifact **not** in the index still works — the server generates extension terms automatically. The index enriches `resolve_artifact` with domain descriptions for better LLM reasoning.

## Installation

```bash
git clone https://github.com/kismatkunwar89/ioi-mcp-server.git
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

## Usage (Programmatic)

```python
from ioi_mcp.ontology_loader import OntologyLoader
from ioi_mcp.batch_generator import generate_all_rows
from ioi_mcp.validator import Validator

ont = OntologyLoader()
validator = Validator(ont)

# Generate JSON-LD from any forensic tool CSV
result = generate_all_rows(
    ontology=ont,
    artifact_name="SRUM",
    csv_path="path/to/srum.csv",
    column_mapping={"AppId": "ioi-ext:appId", "Timestamp": "ioi-ext:timestamp"},
)

# Validate (IRI + SHACL)
v = validator.validate_jsonld(result["jsonld"], result.get("turtle_patch"))
print("Valid:", v.valid)
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

## Acknowledgments

Artifact descriptions in `forensics_wiki_index.json` are derived from the [Forensics Wiki](https://forensics.wiki/), a community-maintained open-source knowledge base for digital forensics. The Forensics Wiki is licensed under Creative Commons and hosted on [GitHub](https://github.com/forensicswiki/wiki). We gratefully acknowledge the contributors and maintainers of the Forensics Wiki for providing comprehensive, freely available documentation of forensic artifact structures, locations, and investigative significance.

This project also builds upon:

- [CASE/UCO](https://caseontology.org/) — The Cyber-investigation Analysis Standard Expression ontology
- [case-utils](https://github.com/casework/CASE-Utilities-Python) — CASE Python utilities and SHACL validation
- [IOI Framework](https://ioi-framework.github.io/) — Indicators of Inconsistency for anti-forensic detection
