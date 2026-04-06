"""
Comprehensive test: run all 60 forensics_wiki_index artifacts through resolve_artifact,
verify tier classification (official/extension), and test the full pipeline with real CSVs.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from ioi_mcp.ontology_loader import OntologyLoader
from ioi_mcp.batch_generator import generate_all_rows
from ioi_mcp.validator import Validator
from ioi_mcp.type_inferencer import analyze_csv


def test_resolve_all_artifacts():
    """Test resolve_artifact logic for all 60 index entries."""
    print("=" * 70)
    print("TEST 1: Resolve all 60 forensics_wiki_index artifacts")
    print("=" * 70)

    ontology = OntologyLoader()

    # Load the index
    data_dir = Path(__file__).parent.parent / "src" / "ioi_mcp" / "data"
    with open(data_dir / "forensics_wiki_index.json") as f:
        index = json.load(f)

    print(f"\nIndex size: {len(index)} artifacts\n")

    official = []
    extension = []
    partial = []  # Has observable but no facet, or has ext facet

    for artifact_name, info in sorted(index.items()):
        # Replicate resolve_artifact logic
        candidates = [
            artifact_name,
            f"Windows{artifact_name}",
            artifact_name.replace(" ", ""),
        ]

        obs_class = None
        obs_class_name = None
        for c in candidates:
            if ontology.observable_exists(c):
                uri = ontology.get_observable_uri(c)
                obs_class_name = uri.split("/")[-1] if uri else c
                obs_class = f"uco-observable:{obs_class_name}"
                break

        facet_name = None
        facet_candidates = [
            f"{obs_class_name}Facet" if obs_class_name else None,
            f"{artifact_name}Facet",
            f"Windows{artifact_name}Facet",
        ]
        for fc in facet_candidates:
            if fc and ontology.facet_exists(fc):
                uri = ontology.get_facet_uri(fc)
                facet_name = uri.split("/")[-1] if uri else fc
                break

        # Check ioi-ext facet
        ext_facet = None
        for ext_name in [f"{artifact_name}Facet", f"Windows{artifact_name}Facet",
                         f"{obs_class_name}Facet" if obs_class_name else None]:
            if ext_name and ontology.get_ext_facet_properties(ext_name):
                ext_facet = ext_name
                break

        # Classify
        wiki_desc = ontology.get_artifact_description(artifact_name)
        has_desc = "YES" if wiki_desc else "no"

        if obs_class and facet_name:
            tier = "OFFICIAL"
            official.append(artifact_name)
            facet_props = ontology.get_facet_properties(facet_name)
            print(f"  ✓ {artifact_name:30s} → {tier:10s} | class={obs_class:45s} | facet={facet_name:35s} | props={len(facet_props):3d} | desc={has_desc}")
        elif obs_class and not facet_name:
            tier = "PARTIAL"
            partial.append(artifact_name)
            print(f"  ~ {artifact_name:30s} → {tier:10s} | class={obs_class:45s} | facet=NONE{' ':26s} | ext={ext_facet or 'none':20s} | desc={has_desc}")
        else:
            tier = "EXTENSION"
            extension.append(artifact_name)
            print(f"  - {artifact_name:30s} → {tier:10s} | class=uco-observable:ObservableObject{' ':8s} | ext={ext_facet or 'none':20s} | desc={has_desc}")

    print(f"\n{'=' * 70}")
    print(f"RESULTS: {len(official)} official, {len(partial)} partial, {len(extension)} extension = {len(official) + len(partial) + len(extension)} total")
    print(f"{'=' * 70}")

    # Verify all have descriptions
    no_desc = [name for name in index if not ontology.get_artifact_description(name)]
    if no_desc:
        print(f"\n⚠ Missing descriptions: {no_desc}")
    else:
        print(f"\n✓ All {len(index)} artifacts have forensics.wiki descriptions")

    return official, partial, extension


def test_real_csvs():
    """Test generate_all_rows with all 12 real CSVs."""
    print(f"\n{'=' * 70}")
    print("TEST 2: Generate JSON-LD from all 12 real forensic CSVs")
    print(f"{'=' * 70}")

    ontology = OntologyLoader()
    validator = Validator(ontology)

    csv_dir = Path("/home/user/workspace")
    csv_files = {
        "Amcache": csv_dir / "amcache_unassociated_sample-4.csv",
        "EVTX": csv_dir / "evtx_sample-5.csv",
        "JumpList": csv_dir / "jumplist_auto_sample-6.csv",
        "LNK": csv_dir / "lnk_sample-7.csv",
        "MFT": csv_dir / "mft_sample-8.csv",
        "RecycleBin": csv_dir / "rbcmd_sample-9.csv",
        "ShimCache": csv_dir / "shimcache_sample-10.csv",
        "SRUM": csv_dir / "srum_appresource_sample-11.csv",
        "UserAssist": csv_dir / "userassist_sample-12.csv",
        "USNJournal": csv_dir / "usn_journal_sample-13.csv",
        "PECmd": csv_dir / "20260218171621_PECmd_Output-2.csv",
        "AmcacheFull": csv_dir / "20260218132315_Amcache_UnassociatedFileEntries.csv",
    }

    results = []
    for name, path in csv_files.items():
        if not path.exists():
            print(f"  SKIP {name:20s} — CSV not found: {path}")
            continue

        start = time.time()
        try:
            result = generate_all_rows(
                ontology=ontology,
                artifact_name=name,
                csv_path=str(path),
                column_mapping={},  # All extension (auto-map)
                description=f"Test: {name} artifact from forensic image",
            )

            elapsed = time.time() - start
            row_count = result["row_count"]

            # Validate
            validation = validator.validate_jsonld(
                result["jsonld"],
                turtle_patch=result.get("turtle_patch"),
            )

            status = "PASS" if validation.valid else "FAIL"
            checks_total = len(validation.checks)
            checks_passed = sum(1 for c in validation.checks.values() if c["pass"])
            failed_checks = [
                (name_c, c["errors"])
                for name_c, c in validation.checks.items()
                if not c["pass"]
            ]

            results.append({
                "name": name,
                "rows": row_count,
                "elapsed": elapsed,
                "valid": validation.valid,
                "checks_passed": checks_passed,
                "checks_total": checks_total,
            })

            print(f"  {'✓' if validation.valid else '✗'} {name:20s} | {row_count:6d} rows | {elapsed:6.2f}s | {status} ({checks_passed}/{checks_total} checks)")
            if failed_checks:
                for check_name, errs in failed_checks:
                    print(f"    ⚠ {check_name}: {errs[0][:100] if errs else 'unknown'}")
                    for e in errs[1:3]:
                        print(f"      {e[:100]}")

        except Exception as e:
            elapsed = time.time() - start
            print(f"  ✗ {name:20s} | ERROR: {str(e)[:80]} | {elapsed:.2f}s")
            results.append({"name": name, "rows": 0, "elapsed": elapsed, "valid": False, "error": str(e)})

    # Summary
    passed = sum(1 for r in results if r.get("valid"))
    total = len(results)
    total_rows = sum(r.get("rows", 0) for r in results)
    total_time = sum(r["elapsed"] for r in results)

    print(f"\n{'=' * 70}")
    print(f"CSV RESULTS: {passed}/{total} passed | {total_rows} total rows | {total_time:.2f}s total")
    print(f"{'=' * 70}")
    return results


def test_contribution_tools():
    """Quick smoke test for the 4 contribution workflow tools."""
    print(f"\n{'=' * 70}")
    print("TEST 3: Contribution workflow tools (smoke test)")
    print(f"{'=' * 70}")

    from ioi_mcp.test_rule import generate_test_graph

    # Test generate_test_graph
    graph = generate_test_graph(
        artifact_name="MFT",
        graph_iri="http://example.org/mft_test",
        synthetic_values={
            "facet_type": "ioi-ext:MftFacet",
            "entity_type": "observable:File",
            "properties": {
                "ioi-ext:entryNumber": {"@type": "xsd:integer", "@value": "12345"},
                "ioi-ext:created0x10": {"@type": "xsd:dateTime", "@value": "2025-02-16T10:15:00"},
                "ioi-ext:created0x30": {"@type": "xsd:dateTime", "@value": "2025-02-16T10:15:00"},
            },
        },
    )

    assert "@context" in graph, "Missing @context"
    assert "@graph" in graph, "Missing @graph"
    assert len(graph["@graph"]) == 1, f"Expected 1 entity, got {len(graph['@graph'])}"
    print("  ✓ generate_test_graph — produces valid JSON-LD structure")

    # Test sparql_context
    from ioi_mcp.sparql_context import extract_sparql_context

    # Save test graph first
    test_path = "/tmp/mft_test.jsonld"
    with open(test_path, "w") as f:
        json.dump(graph, f, indent=2)

    ctx = extract_sparql_context(
        graphs=[{"name": "MFT", "graph_path": test_path, "graph_iri": "http://example.org/mft_test"}],
        contradiction_description="Test: $SI timestamps differ from $FN timestamps",
        category="temporal",
    )

    assert "prefixes" in ctx, "Missing prefixes"
    assert "graphs" in ctx, "Missing graphs"
    assert "sparql_template" in ctx, "Missing sparql_template"
    assert len(ctx["graphs"]) == 1, "Expected 1 graph"
    print(f"  ✓ draft_sparql_context — extracted {ctx['graphs'][0]['property_count']} properties, template generated")

    # Test scaffold_case (dry run)
    from ioi_mcp.scaffold_case import scaffold_case
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        result = scaffold_case(
            output_dir=tmpdir,
            case_id="AF-TEST",
            title="Test Case: MFT Timestomping",
            summary="Synthetic test case for MFT timestamp analysis",
            artifacts=[{
                "name": "MFT",
                "graph_path": test_path,
                "column_mapping": {"Created0x10": "ioi-ext:created0x10"},
                "csv_path": "/tmp/dummy.csv",
            }],
            contributor="test",
            category="temporal",
            inconsistency_description="$SI and $FN timestamps should match for legitimate files",
        )

        assert len(result["files_created"]) >= 4, f"Expected ≥4 files, got {len(result['files_created'])}"
        print(f"  ✓ scaffold_case — created {len(result['files_created'])} files in {result['case_dir']}")

    print(f"\n  All contribution tools working correctly.")


if __name__ == "__main__":
    t0 = time.time()
    official, partial, extension = test_resolve_all_artifacts()
    csv_results = test_real_csvs()
    test_contribution_tools()

    total = time.time() - t0
    print(f"\n{'=' * 70}")
    print(f"ALL TESTS COMPLETE in {total:.1f}s")
    print(f"{'=' * 70}")
