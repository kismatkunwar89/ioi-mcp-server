# New Artifact Workflow
**Load when:** `resolve_artifact` returns `source != registry`.

## Step 1 — generate_instantiator(artifact_name, csv_path)
Produces:
- `instantiators/{name}_instantiator.py`
- `instantiators/templates/{name}/` (4 JSON files)
- Turtle patch string for `ioi-ext.ttl`
- Registry entry appended to `data/ioi_registry.json`

After this, `resolve_artifact(name)` returns `source=registry`.

## Step 2 — Review generated files
- Open `instantiators/{name}_instantiator.py`
- Set `file_facet_columns` (columns that belong to FileFacet, not the artifact facet)
- Append `turtle_patch` to `IoI-Framework/ontologies/ioi-ext.ttl`

## Step 3 — Follow the known-artifact flow
Continue from @skills/artifact-workflow/SKILL.md Step 3.
(Registry is now populated — canonical path active.)

## Step 4 — PR to IoI-Framework
Files:
- `instantiators/{name}_instantiator.py`
- `instantiators/templates/{name}/`
- `ontologies/ioi-ext.ttl` (with Turtle patch appended)
- `registry.json` (new entry)
