# SPARQL Rule Workflow
**Load when:** User wants to write or test a SPARQL IoI detection rule.
**Prerequisite:** At least one validated JSON-LD graph on disk.

## Canonical rule form
Always write rules with explicit `GRAPH <IRI>` clauses — this is the one form
that works in the playground (oxigraph) AND Virtuoso (production).
**Do NOT write default-graph queries** — the playground only loads named graphs.

### Anti-join pattern
Use `FILTER NOT EXISTS` with `GRAPH` clauses for cross-artifact contradictions:
```sparql
GRAPH <.../graphs/prefetch> { ?entry ioi-ext:prefetchExecutableName ?exe . }
FILTER NOT EXISTS {
  GRAPH <.../graphs/mft> {
    ?ff a observable:FileFacet ; observable:fileName ?fn .
    FILTER(UCASE(STR(?fn)) = UCASE(STR(?exe)))
  }
}
```
This works correctly in oxigraph. Do NOT use `MINUS` subquery — unreliable in both rdflib and oxigraph.

## Step 1 — draft_sparql_context(graphs, contradiction_description, category)
- graphs: include `graph_iri` = `https://ioi-framework.github.io/cases/{case_id}/graphs/{artifact_lower}`
- Returns: prefixes, properties_used, join_candidates, SPARQL skeleton with GRAPH clauses

## Step 2 — Write the .rq rule file
- Path: `RULES/{temporal|structural|semantic}/IOI-NNN_{description}.rq`
- Must include version header: rule_id, version, status, category, title, invariant, artifacts, added, changed
- Use `GRAPH <IRI>` clauses — works everywhere
- Use `FILTER NOT EXISTS { GRAPH <IRI> { ... } }` for cross-artifact anti-joins

## Step 3 — generate_test_graph(artifact_name, graph_iri, synthetic_values)
- `synthetic_values`: designed to TRIGGER the rule (positive test)

## Step 4 — test_rule(rule_path, graphs=[{graph_iri, graph_path}])
✓ fired=true → rule works in rdflib
✗ fired=false + rule has cross-graph FILTER NOT EXISTS → rdflib limitation
  → test in playground instead (oxigraph handles it correctly)
  → OR use two-step: query graph A names first, inject via VALUES

## Step 5 — Negative test
Repeat Step 3 with values that should NOT trigger. fired must be false.

## Decision tree after Step 4
- fired=true AND negative=false → correct → load @skills/contribution-workflow/SKILL.md
- fired=true AND negative=true  → over-broad → tighten FILTER
- fired=false + no cross-graph  → check join_candidates, fix FILTER
- fired=false + cross-graph FILTER NOT EXISTS → test in playground (oxigraph handles it)
