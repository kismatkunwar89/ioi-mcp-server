# SPARQL Rule Workflow
**Load when:** User wants to write or test a SPARQL IoI detection rule.
**Prerequisite:** At least one validated JSON-LD graph on disk.

## Step 1 — draft_sparql_context(graphs, contradiction_description, category)
- graphs: include `graph_iri` = `https://ioi-framework.github.io/cases/{case_id}/graphs/{artifact_lower}`
- Returns: prefixes, properties_used, join_candidates, SPARQL skeleton

## Step 2 — Write the .rq rule file
- Path: `RULES/{temporal|structural|semantic}/IOI-NNN_{description}.rq`
- Must include version header: rule_id, version, status, category, title, invariant, artifacts, added, changed
- Use `join_candidates` to find properties that span multiple artifact graphs

## Step 3 — generate_test_graph(artifact_name, graph_iri, synthetic_values)
- `synthetic_values`: designed to TRIGGER the rule (positive test)

## Step 4 — test_rule(rule_path, graphs=[{graph_iri, graph_path}])
✓ fired=true, row_count > 0 → rule works
✗ fired=false               → check FILTER logic, fix rule, re-run

## Step 5 — Negative test
Repeat Step 3 with values that should NOT trigger.
test_rule → `fired` must be false.

## Decision tree after Step 4
- fired=true  AND negative=false → rule is correct → load @skills/contribution-workflow/SKILL.md
- fired=true  AND negative=true  → rule is over-broad → tighten FILTER conditions
- fired=false                    → rule is under-specified → check join_candidates, fix FILTER
