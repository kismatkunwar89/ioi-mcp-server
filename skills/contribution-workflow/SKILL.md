# Contribution Workflow
**Load when:** User wants to contribute a new case or rule to IoI-Framework.
**Prerequisites:** `validate_graph` passed + `test_rule` fired=true.

## Step 1 — scaffold_case(output_dir, case_id, title, summary, artifacts, category)
Produces:
- `CASES/AF-NEW/ground_truth.md`     (filled from your inputs)
- `CASES/AF-NEW/mapping.md`          (filled from column_mapping)
- `CASES/AF-NEW/test/*.jsonld`       (correct graph IRIs)
- `CASES/AF-NEW/snippets/`           (3-row previews)
- `RULES/{category}/IOI-NEW_*.rq`    (skeleton with version header + graph IRIs)

## Step 2 — Verify generated files
- `mapping.md`: field table must be filled (not placeholder text)
- `test/*.jsonld`: `@context.kb` = `https://ioi-framework.github.io/kb/`
- `RULES/*.rq`: version header must have rule_id, version, status, category, title, invariant, artifacts, added

## Step 3 — Run local CI (if framework clone available)
```bash
python scripts/validate_registry.py
python scripts/validate_sparql.py
python scripts/validate_jsonld.py
```

## Step 4 — Open PR to IoI-Framework
- Branch: `add/{case-name}`
- Files: `CASES/AF-NNN/` + `RULES/{category}/`
- Use PR template, fill every checkbox
