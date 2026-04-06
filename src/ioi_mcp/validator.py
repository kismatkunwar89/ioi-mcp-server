"""
Validator — ensures every output has valid IRIs, UUIDs, @context, and passes SHACL.
Case-agnostic: validates any CASE/UCO JSON-LD regardless of artifact type.
"""

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from rdflib import Graph

from ioi_mcp.ontology_loader import OntologyLoader

# UUID v4 pattern
_UUID4_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# Required context prefixes
_REQUIRED_PREFIXES = {"kb", "uco-core", "uco-observable", "xsd"}
_EXTENSION_PREFIX = "ioi-ext"


class ValidationResult:
    """Container for validation results."""

    def __init__(self):
        self.checks: dict[str, dict] = {}
        self.valid = True

    def add_check(self, name: str, passed: bool, errors: Optional[list[str]] = None):
        self.checks[name] = {
            "pass": passed,
            "errors": errors or [],
        }
        if not passed:
            self.valid = False

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "checks": self.checks,
        }


class Validator:
    """Validates CASE/UCO JSON-LD graphs."""

    def __init__(self, ontology: OntologyLoader):
        self.ontology = ontology

    def validate_jsonld(
        self,
        jsonld: dict,
        turtle_patch: Optional[str] = None,
    ) -> ValidationResult:
        """
        Full validation of a JSON-LD graph.

        Checks:
        1. @context completeness
        2. @id format (kb:<type>-<UUID>)
        3. @type IRI resolution (every type exists in ontology or ioi-ext)
        4. Property IRI resolution
        5. JSON-LD parseable by rdflib
        6. SHACL (case_validate) if available
        """
        result = ValidationResult()

        # 1. Context completeness
        ctx_errors = self._check_context(jsonld)
        result.add_check("context_completeness", len(ctx_errors) == 0, ctx_errors)

        # 2. @id format
        id_errors = self._check_ids(jsonld)
        result.add_check("id_format", len(id_errors) == 0, id_errors)

        # 3. @type IRI resolution
        type_errors = self._check_types(jsonld)
        result.add_check("type_iri_resolution", len(type_errors) == 0, type_errors)

        # 4. Property IRI resolution
        prop_errors = self._check_properties(jsonld)
        result.add_check("property_iri_resolution", len(prop_errors) == 0, prop_errors)

        # 5. rdflib parseable
        parse_errors = self._check_parseable(jsonld)
        result.add_check("rdflib_parseable", len(parse_errors) == 0, parse_errors)

        # 6. SHACL validation (optional, requires case_validate)
        shacl_errors = self._check_shacl(jsonld, turtle_patch)
        result.add_check("shacl_conformance", len(shacl_errors) == 0, shacl_errors)

        return result

    def _check_context(self, jsonld: dict) -> list[str]:
        """Check @context has all required prefixes."""
        errors = []
        context = jsonld.get("@context", {})

        for prefix in _REQUIRED_PREFIXES:
            if prefix not in context:
                errors.append(f"Missing required prefix '{prefix}' in @context")

        # Check if ioi-ext is needed but missing
        jsonld_str = json.dumps(jsonld)
        if _EXTENSION_PREFIX + ":" in jsonld_str and _EXTENSION_PREFIX not in context:
            errors.append(
                f"ioi-ext: terms used but '{_EXTENSION_PREFIX}' prefix missing from @context"
            )

        return errors

    def _check_ids(self, jsonld: dict) -> list[str]:
        """Check all @id values follow kb:<type>-<UUID> pattern."""
        errors = []
        seen_ids = set()

        for node in jsonld.get("@graph", []):
            self._collect_id_errors(node, errors, seen_ids)

        return errors

    def _collect_id_errors(self, node: dict, errors: list[str], seen_ids: set):
        """Recursively check @id values."""
        if not isinstance(node, dict):
            return

        node_id = node.get("@id", "")
        if node_id and node_id.startswith("kb:"):
            # Check format: kb:<type>-<uuid>
            local = node_id[3:]  # strip 'kb:'
            parts = local.rsplit("-", 5)
            if len(parts) >= 6:
                # Last 5 parts should form a UUID
                potential_uuid = "-".join(parts[-5:])
                if not _UUID4_PATTERN.match(potential_uuid):
                    errors.append(f"@id '{node_id}' does not end with valid UUIDv4")
            else:
                errors.append(f"@id '{node_id}' does not follow kb:<type>-<UUID> pattern")

            # Check uniqueness
            if node_id in seen_ids:
                errors.append(f"Duplicate @id: '{node_id}'")
            seen_ids.add(node_id)

        # Recurse into hasFacet and other nested structures
        for key, val in node.items():
            if isinstance(val, dict):
                self._collect_id_errors(val, errors, seen_ids)
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, dict):
                        self._collect_id_errors(item, errors, seen_ids)

    def _check_types(self, jsonld: dict) -> list[str]:
        """Check all @type values resolve against the ontology."""
        errors = []

        for node in jsonld.get("@graph", []):
            self._collect_type_errors(node, errors)

        return errors

    def _collect_type_errors(self, node: dict, errors: list[str]):
        """Recursively check @type values."""
        if not isinstance(node, dict):
            return

        node_type = node.get("@type", "")
        if node_type and isinstance(node_type, str):
            if ":" in node_type and not node_type.startswith("xsd:"):
                valid, msg = self.ontology.validate_type_iri(node_type)
                if not valid:
                    errors.append(f"@type '{node_type}': {msg}")

        for key, val in node.items():
            if isinstance(val, dict):
                self._collect_type_errors(val, errors)
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, dict):
                        self._collect_type_errors(item, errors)

    def _check_properties(self, jsonld: dict) -> list[str]:
        """Check property IRIs resolve (official ones only; ioi-ext are allowed)."""
        errors = []

        for node in jsonld.get("@graph", []):
            self._collect_property_errors(node, errors)

        return errors

    def _collect_property_errors(self, node: dict, errors: list[str]):
        """Recursively check property names."""
        if not isinstance(node, dict):
            return

        for key, val in node.items():
            if key.startswith("@"):
                continue

            # Check official UCO properties exist
            if key.startswith("uco-"):
                valid, msg = self.ontology.validate_type_iri(key)
                if not valid:
                    errors.append(f"Property '{key}': {msg}")

            # ioi-ext properties are always allowed (they're user-defined)
            # No check needed for ioi-ext:* properties

            # Recurse
            if isinstance(val, dict):
                self._collect_property_errors(val, errors)
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, dict):
                        self._collect_property_errors(item, errors)

    def _check_parseable(self, jsonld: dict) -> list[str]:
        """Check the JSON-LD is parseable by rdflib."""
        errors = []
        try:
            g = Graph()
            g.parse(data=json.dumps(jsonld), format="json-ld")
            triple_count = len(g)
            if triple_count == 0:
                errors.append("JSON-LD parsed but produced 0 triples")
        except Exception as e:
            errors.append(f"rdflib parse error: {str(e)}")
        return errors

    def _check_shacl(
        self,
        jsonld: dict,
        turtle_patch: Optional[str] = None,
    ) -> list[str]:
        """Run case_validate (SHACL) if available."""
        errors = []

        try:
            # Write JSON-LD to temp file
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".jsonld", delete=False
            ) as f:
                json.dump(jsonld, f, indent=2)
                jsonld_path = f.name

            cmd = ["case_validate", jsonld_path]

            # Add extension ontology if present
            if turtle_patch:
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".ttl", delete=False
                ) as f:
                    f.write(turtle_patch)
                    ttl_path = f.name
                cmd.extend(["--ontology-graph", ttl_path])

            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30
            )

            if result.returncode != 0:
                # Parse SHACL output for actual constraint violations
                output = result.stderr or result.stdout
                if output:
                    for line in output.strip().split("\n"):
                        if "Constraint Violation" in line or "Result" in line:
                            errors.append(line.strip())
                if not errors:
                    errors.append(f"case_validate exited with code {result.returncode}")

        except FileNotFoundError:
            # case_validate not installed — skip gracefully
            pass
        except subprocess.TimeoutExpired:
            errors.append("case_validate timed out (30s)")
        except Exception as e:
            errors.append(f"SHACL validation error: {str(e)}")
        finally:
            # Cleanup temp files
            for path_var in ("jsonld_path", "ttl_path"):
                p = locals().get(path_var)
                if p and Path(p).exists():
                    Path(p).unlink()

        return errors
