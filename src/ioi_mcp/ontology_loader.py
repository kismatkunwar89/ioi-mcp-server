"""
Ontology loader — loads CASE/UCO .ttl from case-utils into rdflib Graph.
Provides SPARQL-based property extraction for any Facet.
Case-agnostic: no artifact types hardcoded. All knowledge comes from the ontology.
"""

import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

from rdflib import Graph, Namespace, RDF, RDFS, OWL, URIRef
from rdflib.namespace import XSD, SH

# UCO namespaces
UCO_CORE = Namespace("https://ontology.unifiedcyberontology.org/uco/core/")
UCO_OBS = Namespace("https://ontology.unifiedcyberontology.org/uco/observable/")
UCO_TYPES = Namespace("https://ontology.unifiedcyberontology.org/uco/types/")
UCO_VOCAB = Namespace("https://ontology.unifiedcyberontology.org/uco/vocabulary/")
UCO_ACTION = Namespace("https://ontology.unifiedcyberontology.org/uco/action/")
UCO_TOOL = Namespace("https://ontology.unifiedcyberontology.org/uco/tool/")
UCO_IDENTITY = Namespace("https://ontology.unifiedcyberontology.org/uco/identity/")
UCO_LOCATION = Namespace("https://ontology.unifiedcyberontology.org/uco/location/")
CASE_INV = Namespace("https://ontology.caseontology.org/case/investigation/")
IOI_EXT = Namespace("https://ioi-framework.github.io/ns/ioi-ext/")
SH_NS = Namespace("http://www.w3.org/ns/shacl#")


def _find_ontology_ttl() -> str:
    """Find the latest aggregated CASE .ttl shipped with case-utils."""
    try:
        import case_utils
        ontology_dir = Path(case_utils.__file__).parent / "ontology"
        ttl_files = sorted(ontology_dir.glob("case-*.ttl"))
        # Pick the latest non-subclasses file
        main_files = [f for f in ttl_files if "subclasses" not in f.name]
        if main_files:
            return str(main_files[-1])
    except ImportError:
        pass
    raise FileNotFoundError(
        "case-utils not installed or ontology .ttl not found. "
        "Install with: pip install case-utils"
    )


class OntologyLoader:
    """Loads and queries the CASE/UCO ontology."""

    def __init__(self, extra_ttl: Optional[str] = None):
        """
        Args:
            extra_ttl: Optional path to additional .ttl files for extension terms.
        """
        self.graph = Graph()
        ttl_path = _find_ontology_ttl()
        self.graph.parse(ttl_path, format="turtle")
        self._ontology_version = Path(ttl_path).stem  # e.g., "case-1.4.0"

        # Auto-load ioi-ext.ttl from data directory (the framework's existing extensions)
        data_dir = Path(__file__).parent / "data"
        for ext_file in ["ioi-ext.ttl", "dfc-ext.ttl"]:
            ext_path = data_dir / ext_file
            if ext_path.exists():
                self.graph.parse(str(ext_path), format="turtle")

        # Load any additional .ttl passed explicitly
        if extra_ttl and os.path.exists(extra_ttl):
            self.graph.parse(extra_ttl, format="turtle")

        # Build class and property indexes (lowercased for lookup)
        self._class_index: dict[str, URIRef] = {}
        self._facet_index: dict[str, URIRef] = {}
        self._observable_index: dict[str, URIRef] = {}
        self._ext_property_index: dict[str, list[dict]] = {}  # facet_name -> [properties]

        self._build_indexes()

    def _build_indexes(self):
        """Build lookup indexes from the ontology graph."""
        # All OWL classes
        for s in self.graph.subjects(RDF.type, OWL.Class):
            local = str(s).split("/")[-1]
            self._class_index[local.lower()] = s

        # Facet subclasses (transitive)
        for facet in self._get_all_subclasses(UCO_CORE["Facet"]):
            local = str(facet).split("/")[-1]
            self._facet_index[local.lower()] = facet

        # ObservableObject subclasses (transitive)
        for obs in self._get_all_subclasses(UCO_OBS["ObservableObject"]):
            local = str(obs).split("/")[-1]
            self._observable_index[local.lower()] = obs

        # Index ioi-ext / dfc-ext properties by their rdfs:domain (facet name)
        self._build_ext_property_index()

        # Load forensics.wiki artifact descriptions
        self._artifact_descriptions = {}
        _data_dir = Path(__file__).parent / "data"
        wiki_index = _data_dir / "forensics_wiki_index.json"
        if wiki_index.exists():
            import json as _json
            with open(wiki_index) as _f:
                self._artifact_descriptions = _json.load(_f)

    def _build_ext_property_index(self):
        """Index ioi-ext properties by their rdfs:domain facet."""
        IOI_EXT_NS_STR = str(IOI_EXT)  # https://ioi-framework.github.io/ns/ioi-ext/

        for prop_type in [OWL.DatatypeProperty, OWL.ObjectProperty, URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#Property")]:
            for prop_uri in self.graph.subjects(RDF.type, prop_type):
                prop_str = str(prop_uri)
                # Only index ioi-ext / dfc-ext properties
                if not (IOI_EXT_NS_STR in prop_str or "dfc-ext" in prop_str):
                    continue

                # Get domain (the facet this belongs to)
                domains = list(self.graph.objects(prop_uri, RDFS.domain))
                if not domains:
                    continue

                domain_local = str(domains[0]).split("/")[-1]
                local_name = prop_str.split("/")[-1]

                # Get range
                ranges = list(self.graph.objects(prop_uri, RDFS.range))
                range_str = str(ranges[0]).split("#")[-1] if ranges else "string"

                # Get label and comment
                labels = list(self.graph.objects(prop_uri, RDFS.label))
                comments = list(self.graph.objects(prop_uri, RDFS.comment))

                # Determine prefix
                if IOI_EXT_NS_STR in prop_str:
                    prefixed = f"ioi-ext:{local_name}"
                else:
                    prefixed = f"dfc-ext:{local_name}"

                prop_entry = {
                    "name": prefixed,
                    "local_name": local_name,
                    "label": str(labels[0]) if labels else local_name,
                    "description": str(comments[0]) if comments else "",
                    "iri": prop_str,
                    "range": range_str,
                    "range_iri": str(ranges[0]) if ranges else "",
                    "range_type": "datatype",
                    "max_count": 1,
                    "is_array": False,
                }

                # Determine shape hint from range
                if range_str in ("integer", "int"):
                    prop_entry["shape_hint"] = "integer_literal"
                elif range_str in ("dateTime",):
                    prop_entry["shape_hint"] = "datetime_literal"
                elif range_str in ("boolean",):
                    prop_entry["shape_hint"] = "boolean_literal"
                elif range_str in ("decimal", "float", "double"):
                    prop_entry["shape_hint"] = "number_literal"
                else:
                    prop_entry["shape_hint"] = "string"

                if domain_local not in self._ext_property_index:
                    self._ext_property_index[domain_local] = []
                self._ext_property_index[domain_local].append(prop_entry)

    def get_ext_facet_properties(self, facet_name: str) -> list[dict]:
        """Get ioi-ext properties defined for a given extension facet."""
        return self._ext_property_index.get(facet_name, [])

    def get_all_ext_facets(self) -> dict[str, list[dict]]:
        """Get all extension facets and their properties."""
        return dict(self._ext_property_index)

    def get_artifact_description(self, artifact_name: str) -> dict | None:
        """Get forensics.wiki description for an artifact (case-insensitive)."""
        for key, val in self._artifact_descriptions.items():
            if key.lower() == artifact_name.lower():
                return val
            if val.get("full_name", "").lower() == artifact_name.lower():
                return val
        return None

    def _get_all_subclasses(self, cls: URIRef) -> set[URIRef]:
        """Get all transitive subclasses of a class."""
        subs = set()
        for s in self.graph.subjects(RDFS.subClassOf, cls):
            if s not in subs:
                subs.add(s)
                subs.update(self._get_all_subclasses(s))
        return subs

    @property
    def version(self) -> str:
        return self._ontology_version

    @property
    def class_count(self) -> int:
        return len(self._class_index)

    @property
    def facet_count(self) -> int:
        return len(self._facet_index)

    @property
    def observable_count(self) -> int:
        return len(self._observable_index)

    def class_exists(self, class_name: str) -> bool:
        """Check if a class exists in the ontology (case-insensitive)."""
        return class_name.lower() in self._class_index

    def facet_exists(self, facet_name: str) -> bool:
        """Check if a Facet subclass exists (case-insensitive)."""
        return facet_name.lower() in self._facet_index

    def observable_exists(self, obs_name: str) -> bool:
        """Check if an ObservableObject subclass exists (case-insensitive)."""
        return obs_name.lower() in self._observable_index

    def get_facet_properties(self, facet_name: str) -> list[dict]:
        """
        Get all SHACL-defined properties for a Facet.
        Returns list of {name, local_name, range, range_type, max_count}.
        Queries the ontology live — never hardcoded.
        """
        facet_key = facet_name.lower()
        if facet_key not in self._facet_index:
            return []

        facet_uri = self._facet_index[facet_key]
        properties = []

        for prop_shape in self.graph.objects(facet_uri, SH_NS.property):
            path = list(self.graph.objects(prop_shape, SH_NS.path))
            if not path:
                continue

            prop_uri = path[0]
            local_name = str(prop_uri).split("/")[-1]

            # Determine range type
            datatype = list(self.graph.objects(prop_shape, SH_NS.datatype))
            cls = list(self.graph.objects(prop_shape, SH_NS["class"]))
            max_count = list(self.graph.objects(prop_shape, SH_NS.maxCount))

            if datatype:
                range_uri = str(datatype[0])
                range_type = "datatype"
            elif cls:
                range_uri = str(cls[0])
                range_type = "object"
            else:
                range_uri = str(XSD.string)
                range_type = "datatype"

            # Extract just the local part for display
            range_local = range_uri.split("#")[-1] if "#" in range_uri else range_uri.split("/")[-1]

            # Determine namespace prefix
            if "observable/" in str(prop_uri):
                prefixed = f"uco-observable:{local_name}"
            elif "core/" in str(prop_uri):
                prefixed = f"uco-core:{local_name}"
            elif "types/" in str(prop_uri):
                prefixed = f"uco-types:{local_name}"
            elif "action/" in str(prop_uri):
                prefixed = f"uco-action:{local_name}"
            else:
                prefixed = local_name

            # Get rdfs:comment for this property (the human description)
            comments = list(self.graph.objects(prop_uri, RDFS.comment))
            description = str(comments[0]) if comments else ""
            # Also get rdfs:label
            labels = list(self.graph.objects(prop_uri, RDFS.label))
            label = str(labels[0]) if labels else local_name

            properties.append({
                "name": prefixed,
                "local_name": local_name,
                "label": label,
                "description": description,
                "iri": str(prop_uri),
                "range": range_local,
                "range_iri": range_uri,
                "range_type": range_type,
                "max_count": int(str(max_count[0])) if max_count else None,
                "is_array": max_count == [] or (max_count and int(str(max_count[0])) > 1),
            })

        return properties

    def get_facet_uri(self, facet_name: str) -> Optional[str]:
        """Get the full IRI for a Facet name."""
        key = facet_name.lower()
        if key in self._facet_index:
            return str(self._facet_index[key])
        return None

    def get_observable_uri(self, obs_name: str) -> Optional[str]:
        """Get the full IRI for an ObservableObject name."""
        key = obs_name.lower()
        if key in self._observable_index:
            return str(self._observable_index[key])
        return None

    def iri_exists(self, iri: str) -> bool:
        """Check if any IRI exists in the ontology (as subject)."""
        uri = URIRef(iri)
        return bool(list(self.graph.triples((uri, None, None))))

    def validate_type_iri(self, type_str: str) -> tuple[bool, str]:
        """
        Validate a prefixed type string (e.g., 'uco-observable:File').
        Returns (valid, full_iri_or_error).
        """
        prefix_map = {
            "uco-observable": str(UCO_OBS),
            "uco-core": str(UCO_CORE),
            "uco-types": str(UCO_TYPES),
            "uco-action": str(UCO_ACTION),
            "uco-tool": str(UCO_TOOL),
            "uco-identity": str(UCO_IDENTITY),
            "uco-location": str(UCO_LOCATION),
            "uco-vocabulary": str(UCO_VOCAB),
            "case-investigation": str(CASE_INV),
            "ioi-ext": str(IOI_EXT),
        }

        if ":" not in type_str:
            return False, f"No prefix in '{type_str}'"

        prefix, local = type_str.split(":", 1)
        if prefix not in prefix_map:
            return False, f"Unknown prefix '{prefix}'"

        full_iri = prefix_map[prefix] + local

        # ioi-ext terms are valid if they follow naming convention
        if prefix == "ioi-ext":
            return True, full_iri

        # Official terms must exist in ontology
        if self.iri_exists(full_iri):
            return True, full_iri
        else:
            return False, f"IRI not found in ontology: {full_iri}"
