# Artifact Workflow
**Load when:** User says "work on [artifact]" or drops a CSV.

## Step 1 — resolve_artifact(name)
- `source=registry` → canonical_field_types returned → skip to Step 3
- `source=extension` → load @skills/new-artifact-workflow/SKILL.md

## Step 2 — analyze_csv(csv_path)
Check: do CSV headers match `canonical_field_types` columns?
✓ ≥80% match → use canonical mapping
✗ mismatch   → flag unknown columns, map them to `ioi-ext:` automatically

## Step 3 — generate_all_rows(name, csv_path, column_mapping={})
- Registry artifacts: `column_mapping={}` uses canonical field_types automatically
- Use `file_facet_columns` from resolve_artifact for FileFacet assignment

## Step 4 — validate_graph(jsonld_path) ← MANDATORY
✓ valid  → proceed to Step 5
✗ invalid → read errors, fix column_mapping, re-run Step 3

## Step 5 — Next step decision
- Writing a detection rule? → load @skills/sparql-workflow/SKILL.md
- Packaging a PR?          → load @skills/contribution-workflow/SKILL.md
