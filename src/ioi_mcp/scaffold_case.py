"""
scaffold_case — assembles a complete CASES/AF-NEW/ directory structure
plus a SPARQL rule skeleton, ready for IoI-Framework PR.

M-8 additions:
- Uses registry IRI pattern for all graph IRIs
- Emits RULES/{category}/IOI-NEW_{name}.rq skeleton with version header + correct IRIs
- Emits _instantiators/inst-NEW.md front-matter when artifact is new (not in registry)
"""

import json
import os
import uuid
from pathlib import Path
from typing import Optional

KB_NAMESPACE      = "https://ioi-framework.github.io/kb/"
GRAPH_IRI_PATTERN = "https://ioi-framework.github.io/cases/{case_id}/graphs/{artifact_lower}"


def make_graph_iri(case_id: str, artifact_name: str) -> str:
    return GRAPH_IRI_PATTERN.format(
        case_id=case_id,
        artifact_lower=artifact_name.lower().replace(" ", "_"),
    )


def scaffold_case(
    output_dir: str,
    case_id: str,
    title: str,
    summary: str,
    artifacts: list[dict],
    contributor: str = "contributor",
    category: str = "temporal",
    inconsistency_description: str = "",
    registry_entry: Optional[dict] = None,
) -> dict:
    """
    Assemble a complete CASES/AF-NEW/ directory + SPARQL rule skeleton.

    artifacts items:
        name, graph_path, column_mapping, csv_path,
        turtle_path (optional), is_new_artifact (optional bool)
    """
    case_dir = Path(output_dir) / "CASES" / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "test").mkdir(exist_ok=True)
    (case_dir / "snippets").mkdir(exist_ok=True)

    files_created = []

    # 1. ground_truth.md
    gt = case_dir / "ground_truth.md"
    gt.write_text(_build_ground_truth(case_id, title, summary, artifacts, inconsistency_description))
    files_created.append(str(gt))

    # 2. mapping.md (filled from column_mapping)
    mp = case_dir / "mapping.md"
    mp.write_text(_build_mapping_doc(case_id, artifacts))
    files_created.append(str(mp))

    # 3. README.md
    rm = case_dir / "README.md"
    rm.write_text(_build_readme(case_id, title, artifacts, inconsistency_description))
    files_created.append(str(rm))

    # 4. Snippets — first 3 rows + correct graph IRI in @context
    for artifact in artifacts:
        gp = artifact.get("graph_path")
        if gp and os.path.exists(gp):
            snippet = _extract_snippet(gp, max_rows=3)
            # Ensure @context has correct kb namespace
            if "@context" in snippet:
                snippet["@context"]["kb"] = KB_NAMESPACE
            sn = case_dir / "snippets" / f"{artifact['name'].lower()}_snippet.jsonld"
            with open(sn, "w") as f: json.dump(snippet, f, indent=2)
            files_created.append(str(sn))

    # 5. Test graphs — 1 row, correct graph IRI
    for artifact in artifacts:
        gp = artifact.get("graph_path")
        if gp and os.path.exists(gp):
            test_g = _extract_snippet(gp, max_rows=1)
            if "@context" in test_g:
                test_g["@context"]["kb"] = KB_NAMESPACE
            tn = case_dir / "test" / f"{artifact['name'].lower()}_test.jsonld"
            with open(tn, "w") as f: json.dump(test_g, f, indent=2)
            files_created.append(str(tn))

    # 6. Jekyll front-matter stubs
    cf = case_dir / f"{case_id.lower()}_frontmatter.md"
    cf.write_text(_build_case_frontmatter(case_id, title, summary, artifacts, contributor, category))
    files_created.append(str(cf))

    rf = case_dir / f"ioi_{case_id.lower()}_rule_frontmatter.md"
    rf.write_text(_build_rule_frontmatter(case_id, title, category, artifacts, contributor))
    files_created.append(str(rf))

    # 7. SPARQL rule skeleton with version header + correct graph IRIs (M-8 addition)
    rules_dir = Path(output_dir) / "RULES" / category
    rules_dir.mkdir(parents=True, exist_ok=True)
    rule_name = title.lower().replace(" ", "_").replace("/", "_")[:40]
    rule_id   = f"IOI-{case_id.replace('AF-', '')}"
    rq = rules_dir / f"{rule_id}_{rule_name}.rq"
    rq.write_text(_build_sparql_skeleton(case_id, rule_id, title, category, artifacts, contributor))
    files_created.append(str(rq))

    # 8. _instantiators front-matter for new artifacts (M-8 addition)
    for artifact in artifacts:
        if artifact.get("is_new_artifact"):
            inst_dir = Path(output_dir) / "_instantiators"
            inst_dir.mkdir(exist_ok=True)
            inst_id = f"INST-{case_id.replace('AF-', '')}"
            inst_f  = inst_dir / f"inst-{case_id.lower()}.md"
            inst_f.write_text(_build_instantiator_frontmatter(
                inst_id, artifact["name"], contributor))
            files_created.append(str(inst_f))

    return {"case_dir": str(case_dir), "files_created": files_created}


# ── Content builders ──────────────────────────────────────────────────────────

def _build_sparql_skeleton(case_id, rule_id, title, category, artifacts, contributor):
    """Build a SPARQL rule skeleton with version header and correct graph IRIs."""
    artifact_names = ", ".join(a["name"] for a in artifacts)
    graph_clauses  = "
".join(
        f"  # GRAPH <{make_graph_iri(case_id, a['name'])}> {{
"
        f"  #   ?entry a observable:File ;
"
        f"  #          core:hasFacet ?facet .
"
        f"  #   # add detection pattern for {a['name']}
"
        f"  # }}"
        for a in artifacts
    )
    filter_hint = {
        "temporal":   "  # FILTER(?timestamp1 != ?timestamp2)",
        "structural": "  # FILTER NOT EXISTS { GRAPH <...> { ?entry ... } }",
        "semantic":   "  # FILTER(?value1 != ?value2)",
    }.get(category, "  # FILTER(...)")

    return f"""# rule_id:   {rule_id}
# version:   1.0
# status:    Community
# category:  {category}
# title:     {title}
# invariant: (describe the expected invariant φ that is violated)
# artifacts: {artifact_names}
# contributor: {contributor}
# added:     (date)
# changed:   (none)
#
PREFIX core:       <https://ontology.unifiedcyberontology.org/uco/core/>
PREFIX observable: <https://ontology.unifiedcyberontology.org/uco/observable/>
PREFIX ioi-ext:    <https://ioi-framework.github.io/ns/ioi-ext/>
PREFIX xsd:        <http://www.w3.org/2001/XMLSchema#>

SELECT DISTINCT ?entry ?evidence
WHERE {{
{graph_clauses}

{filter_hint}
}}
ORDER BY ?entry
"""


def _build_instantiator_frontmatter(inst_id, artifact_name, contributor):
    return f"""---
inst_id:      {inst_id}
title:        "{artifact_name} Instantiator"
artifact:     {artifact_name}
parser_tool:  (tool used to extract CSV)
input_format: CSV
output:       JSON-LD
template:     {artifact_name.lower()}
script:       {artifact_name.lower()}_instantiator.py
contributor:  "@{contributor}"
date_added:   (date)
status:       Community
---

## Notes

(describe what parser produces the CSV and any field normalisation applied)
"""


def _build_ground_truth(case_id, title, summary, artifacts, inconsistency):
    al = "
".join(
        f"- **{a['name']}** — {', '.join(list(a.get('column_mapping', {}).values())[:5])}"
        for a in artifacts
    )
    at = "
".join(f"| {a['name']} | {a.get('csv_path', 'N/A')} |" for a in artifacts)
    return f"""# {case_id}: {title}

## Summary
{summary}

## Impacted Artifacts
{al}

## Scenario Steps
### 1. Set Up
- (describe initial system state)
### 2. Tampering Process
- (describe the anti-forensic technique)
### 3. Post-Tampering Evidence Collection
- (describe artifacts collected)

## Ground Truth Criteria
{inconsistency or "- (describe the invariant that was violated)"}

## Dataset Reference
| Artifact | Source |
|----------|--------|
{at}

## Inconsistency Summary
{inconsistency or "(describe the contradiction between artifacts)"}
"""


def _build_mapping_doc(case_id, artifacts):
    rows = []
    ext_props = []
    for a in artifacts:
        for col, prop in a.get("column_mapping", {}).items():
            facet = "ioi-ext" if "ioi-ext:" in prop else "uco-observable"
            rows.append(f"| {col} | {prop} | {facet} | {a['name']} |")
            if "ioi-ext:" in prop:
                ext_props.append(prop)
    table = "
".join(rows) or "| (fill in) | (fill in) | (fill in) | (fill in) |"
    exts  = "
".join(f"- `{p}`" for p in sorted(set(ext_props))) or "- (none)"
    return f"""# Mapping Notes ({case_id})

## Input Artifacts
{chr(10).join(f"- {a['name']}: `{a.get('csv_path', 'N/A')}`" for a in artifacts)}

## Mapping Method
- Generated via IOI MCP Server (`generate_all_rows`)

## Field-to-Ontology Mapping
| Source Field | JSON-LD Property | Facet | Artifact |
|---|---|---|---|
{table}

## ioi-ext Extensions Used
{exts}

## Graph IRIs Used
{chr(10).join(f"- {a['name']}: `{make_graph_iri('AF-NEW', a['name'])}`" for a in artifacts)}
"""


def _build_readme(case_id, title, artifacts, inconsistency):
    return f"""# {case_id}

## Extracted Artifacts
{chr(10).join(f"- {a['name']}" for a in artifacts)}

## IoI Signature
{inconsistency or "(short description of the contradiction)"}

## Graph IRIs
{chr(10).join(f"- {a['name']}: `{make_graph_iri(case_id, a['name'])}`" for a in artifacts)}

## Notes
- Generated via IOI MCP Server
- status: Community
"""


def _build_case_frontmatter(case_id, title, summary, artifacts, contributor, category):
    names = ", ".join(a["name"] for a in artifacts)
    return f"""---
layout: case
case_id: {case_id}
title: "{title}"
status: Community
contributor: {contributor}
category: {category}
artifacts: [{names}]
rule: IOI-{case_id.replace("AF-", "")}
summary: >
  {summary[:200]}
---

{summary}
"""


def _build_rule_frontmatter(case_id, title, category, artifacts, contributor):
    rule_id = f"IOI-{case_id.replace('AF-', '')}"
    names   = ", ".join(a["name"] for a in artifacts)
    return f"""---
layout: rule
rule_id: {rule_id}
title: "{title}"
status: Community
contributor: {contributor}
category: {category}
case: {case_id}
artifacts: [{names}]
---

## Description
(describe what the SPARQL rule checks)

## Graph IRIs
{chr(10).join(f"- `{make_graph_iri(case_id, a['name'])}`" for a in artifacts)}

## Detection Logic
(describe the FILTER conditions)
"""


def _extract_snippet(graph_path: str, max_rows: int = 3) -> dict:
    with open(graph_path) as f:
        data = json.load(f)
    if "@graph" in data:
        s = dict(data)
        s["@graph"] = data["@graph"][:max_rows]
        return s
    return data
