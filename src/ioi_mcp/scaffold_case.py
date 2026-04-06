"""
scaffold_case — assembles a complete CASES/AF-NEW/ directory structure
from pre-generated graphs and user description.

Expects graphs already exist on disk (from generate_all_rows).
Produces: ground_truth.md, mapping.md, README.md, test graphs, snippets,
and Jekyll front-matter stubs.
"""

import json
import os
import uuid
from pathlib import Path
from typing import Optional


def scaffold_case(
    output_dir: str,
    case_id: str,
    title: str,
    summary: str,
    artifacts: list[dict],
    contributor: str,
    category: str = "temporal",
    inconsistency_description: str = "",
) -> dict:
    """
    Assemble a complete CASES/AF-NEW/ directory structure.

    Args:
        output_dir: base directory (e.g., path to ioi-framework repo)
        case_id: e.g., "AF-NEW" (placeholder, maintainers finalize)
        title: human title, e.g., "Timestomping Detection via Prefetch Analysis"
        summary: plain English description of the anti-forensic technique
        artifacts: list of {
            "name": "MFT",
            "graph_path": "/path/to/mft_full_graph.jsonld",
            "turtle_path": "/path/to/mft_ext.ttl" (optional),
            "column_mapping": {"col": "prop", ...},
            "csv_path": "/path/to/mft.csv",
        }
        contributor: GitHub handle
        category: "temporal", "structural", or "semantic"
        inconsistency_description: what the contradiction is

    Returns:
        {"case_dir": path, "files_created": [list of files]}
    """
    case_dir = Path(output_dir) / "CASES" / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "test").mkdir(exist_ok=True)
    (case_dir / "snippets").mkdir(exist_ok=True)

    files_created = []

    # 1. ground_truth.md
    gt_path = case_dir / "ground_truth.md"
    gt_content = _build_ground_truth(case_id, title, summary, artifacts, inconsistency_description)
    gt_path.write_text(gt_content)
    files_created.append(str(gt_path))

    # 2. mapping.md
    map_path = case_dir / "mapping.md"
    map_content = _build_mapping_doc(case_id, artifacts)
    map_path.write_text(map_content)
    files_created.append(str(map_path))

    # 3. README.md
    readme_path = case_dir / "README.md"
    readme_content = _build_readme(case_id, title, artifacts, inconsistency_description)
    readme_path.write_text(readme_content)
    files_created.append(str(readme_path))

    # 4. Snippets (first 3 rows from each graph)
    for artifact in artifacts:
        graph_path = artifact.get("graph_path")
        if graph_path and os.path.exists(graph_path):
            snippet = _extract_snippet(graph_path, max_rows=3)
            snippet_name = f"{artifact['name'].lower()}_snippet.jsonld"
            snippet_path = case_dir / "snippets" / snippet_name
            with open(snippet_path, "w") as f:
                json.dump(snippet, f, indent=2)
            files_created.append(str(snippet_path))

    # 5. Test graph stubs (empty structure for contributor to fill with synthetic values)
    for artifact in artifacts:
        graph_path = artifact.get("graph_path")
        if graph_path and os.path.exists(graph_path):
            test_graph = _extract_snippet(graph_path, max_rows=1)
            test_name = f"{artifact['name'].lower()}_test.jsonld"
            test_path = case_dir / "test" / test_name
            with open(test_path, "w") as f:
                json.dump(test_graph, f, indent=2)
            files_created.append(str(test_path))

    # 6. Jekyll front-matter stubs
    # _cases/af-NEW.md for the docs site
    cases_stub = _build_case_frontmatter(case_id, title, summary, artifacts, contributor, category)
    cases_stub_path = case_dir / f"{case_id.lower()}_frontmatter.md"
    cases_stub_path.write_text(cases_stub)
    files_created.append(str(cases_stub_path))

    # _rules/ioi-NEW.md for the docs site
    rule_stub = _build_rule_frontmatter(case_id, title, category, artifacts, contributor)
    rule_stub_path = case_dir / f"ioi_{case_id.lower()}_rule_frontmatter.md"
    rule_stub_path.write_text(rule_stub)
    files_created.append(str(rule_stub_path))

    return {
        "case_dir": str(case_dir),
        "files_created": files_created,
    }


def _build_ground_truth(case_id, title, summary, artifacts, inconsistency):
    artifact_list = "\n".join(
        f"- **{a['name']}** — {', '.join(a.get('column_mapping', {}).values())[:100]}"
        for a in artifacts
    )
    artifact_table = "\n".join(
        f"| {a['name']} | {a.get('csv_path', 'N/A')} |"
        for a in artifacts
    )

    return f"""# {case_id}: {title}

## Summary

{summary}

## Impacted Artifacts and Attributes

{artifact_list}

## Scenario Steps

### 1. Set Up
- (describe initial system state)

### 2. Tampering Process
- (describe the anti-forensic technique applied)

### 3. Post-Tampering Evidence Collection
- (describe what artifacts were collected and how)

## Ground Truth Criteria

{inconsistency if inconsistency else '- (describe what makes this detectable — the invariant that was violated)'}

## Dataset Reference

| Artifact Type | Source |
|---------------|--------|
{artifact_table}

## Inconsistency Summary

{inconsistency if inconsistency else '(describe the contradiction between artifacts)'}

## Pseudo-queries to Surface the Inconsistency

```sql
-- (contributor fills in the detection logic)
```

## Evidence Summary

- (contributor fills in specific evidence values)
"""


def _build_mapping_doc(case_id, artifacts):
    rows = []
    ext_props = []
    for a in artifacts:
        for csv_col, prop in a.get("column_mapping", {}).items():
            facet = "ioi-ext" if "ioi-ext:" in prop else "uco-observable"
            rows.append(f"| {csv_col} | {prop} | {facet} | {a['name']} |")
            if "ioi-ext:" in prop:
                ext_props.append(prop)

    mapping_table = "\n".join(rows) if rows else "| (fill in) | (fill in) | (fill in) | (fill in) |"
    ext_list = "\n".join(f"- `{p}`" for p in sorted(set(ext_props))) if ext_props else "- (none)"

    return f"""# Mapping Notes ({case_id})

## Purpose
This file documents how raw artifact fields were mapped into CASE/UCO JSON-LD for this case.

## Input Artifacts
{chr(10).join(f'- {a["name"]}: `{a.get("csv_path", "N/A")}`' for a in artifacts)}

## Mapping Method
- Generated via IOI MCP Server (`generate_all_rows`)
- Column mappings provided by contributor via Claude/MCP workflow

## Field-to-Ontology Mapping
| Source Field | JSON-LD Property | Facet/Class | Artifact |
| --- | --- | --- | --- |
{mapping_table}

## ioi-ext Extensions Used
{ext_list}

## Notes / Assumptions
- Time normalization: US locale dates converted to ISO 8601
- All values from CSV row data — no manual edits
"""


def _build_readme(case_id, title, artifacts, inconsistency):
    return f"""# {case_id}

## Extracted Artifacts
{chr(10).join(f'- {a["name"]}' for a in artifacts)}

## Mapped Artifacts
{chr(10).join(f'- {a["name"]} → CASE/UCO + ioi-ext JSON-LD' for a in artifacts)}

## IoI Signature
{inconsistency if inconsistency else '- (short description of the contradiction)'}

## Notes
- Generated via IOI MCP Server
- status: Community
"""


def _build_case_frontmatter(case_id, title, summary, artifacts, contributor, category):
    artifact_names = ", ".join(a["name"] for a in artifacts)
    return f"""---
layout: case
case_id: {case_id}
title: "{title}"
status: Community
contributor: {contributor}
category: {category}
artifacts: [{artifact_names}]
rule: IOI-{case_id.replace('AF-', '')}
summary: >
  {summary[:200]}
---

{summary}
"""


def _build_rule_frontmatter(case_id, title, category, artifacts, contributor):
    rule_id = f"IOI-{case_id.replace('AF-', '')}"
    artifact_names = ", ".join(a["name"] for a in artifacts)
    return f"""---
layout: rule
rule_id: {rule_id}
title: "{title}"
status: Community
contributor: {contributor}
category: {category}
case: {case_id}
artifacts: [{artifact_names}]
---

## Description

(describe what the SPARQL rule checks)

## Graph IRIs

(list the named graphs the rule queries)

## Detection Logic

(describe the FILTER conditions)
"""


def _extract_snippet(graph_path: str, max_rows: int = 3) -> dict:
    """Extract first N rows from a JSON-LD graph file."""
    with open(graph_path, "r") as f:
        data = json.load(f)

    if "@graph" in data:
        snippet = dict(data)
        snippet["@graph"] = data["@graph"][:max_rows]
        return snippet
    return data
