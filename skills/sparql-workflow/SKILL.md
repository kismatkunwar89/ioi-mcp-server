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
- **Write the default-graph form first** (no `GRAPH <...>` clauses) — works in playground + rdflib
- Note in the rule header: "On Virtuoso, wrap patterns in GRAPH clauses"

## Step 3 — generate_test_graph(artifact_name, graph_iri, synthetic_values)
- `synthetic_values`: designed to TRIGGER the rule (positive test)

## Step 4 — test_rule(rule_path, graphs=[{graph_iri, graph_path}])
✓ fired=true, row_count > 0 → rule works
✗ fired=false               → check FILTER logic, fix rule, re-run

**If rule uses FILTER NOT EXISTS or MINUS across named graphs:**
rdflib has a known limitation with cross-graph variable correlation.
Fix: rewrite as default-graph form (remove GRAPH clauses). The playground
merges all loaded files into the default graph, so the rule still detects
the right contradiction. Virtuoso handles the named-graph version correctly.

## Step 5 — Negative test
Repeat Step 3 with values that should NOT trigger.
test_rule → `fired` must be false.

## Decision tree after Step 4
- fired=true  AND negative=false → rule is correct → load @skills/contribution-workflow/SKILL.md
- fired=true  AND negative=true  → rule is over-broad → tighten FILTER conditions
- fired=false + no cross-graph   → rule is under-specified → check join_candidates, fix FILTER
- fired=false + cross-graph      → likely rdflib limitation → rewrite as default-graph form

## Two forms of every rule

| Form | GRAPH clauses | Works in | Use for |
|------|--------------|----------|---------|
| Default-graph | None | Playground + rdflib test_rule | Writing, testing, contribution |
| Named-graph | `GRAPH <IRI> { }` | Virtuoso (production) | Production Virtuoso deployment |

Both detect the same contradiction. Write default-graph first, note Virtuoso form in rule header.
