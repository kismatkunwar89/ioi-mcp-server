"""
Extension generator — creates ioi-ext: Facet classes + properties + Turtle patches.
Case-agnostic: generates from CSV column metadata, never from hardcoded artifact knowledge.
"""

from typing import Optional


IOI_EXT_NS = "https://ontology.ioi-framework.org/ext/"
IOI_EXT_PREFIX = "ioi-ext"


def _to_facet_name(artifact_name: str) -> str:
    """Convert artifact name to Facet class name.
    'SRUM' -> 'SRUMFacet'
    'ShellBags' -> 'ShellBagsFacet'
    'USN Journal' -> 'USNJournalFacet'
    """
    # Remove spaces, hyphens, underscores -> PascalCase
    clean = artifact_name.replace(" ", "").replace("-", "").replace("_", "")
    if not clean.endswith("Facet"):
        clean += "Facet"
    return clean


def _to_property_name(artifact_name: str, column_clean_name: str) -> str:
    """Convert to ioi-ext property name.
    ('SRUM', 'bytesSent') -> 'srumBytesSent'
    """
    # Artifact prefix in lowercase
    prefix = artifact_name.lower().replace(" ", "").replace("-", "").replace("_", "")
    # Column name with first letter capitalized
    prop_part = column_clean_name[0].upper() + column_clean_name[1:] if column_clean_name else ""
    return prefix + prop_part


def generate_turtle_patch(
    artifact_name: str,
    columns: list[dict],
    description: Optional[str] = None,
) -> str:
    """
    Generate a Turtle (.ttl) patch defining a new ioi-ext Facet.

    Args:
        artifact_name: e.g., 'SRUM'
        columns: list of {clean_name, inferred_type, column_name} from type_inferencer
        description: optional human description of the artifact

    Returns:
        Turtle string defining the Facet class and all properties.
    """
    facet_name = _to_facet_name(artifact_name)
    desc = description or f"Properties extracted from {artifact_name} forensic artifact."

    lines = [
        f"@prefix {IOI_EXT_PREFIX}: <{IOI_EXT_NS}> .",
        "@prefix uco-core: <https://ontology.unifiedcyberontology.org/uco/core/> .",
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .",
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
        "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .",
        "",
        f"{IOI_EXT_PREFIX}:{facet_name} a owl:Class ;",
        f'    rdfs:subClassOf uco-core:Facet ;',
        f'    rdfs:label "{facet_name}"@en ;',
        f'    rdfs:comment "{desc}"@en .',
        "",
    ]

    for col in columns:
        prop_name = _to_property_name(artifact_name, col["clean_name"])
        xsd_type = col["inferred_type"]
        col_desc = col.get("column_name", col["clean_name"])

        # Map xsd prefix to range
        if xsd_type.startswith("xsd:"):
            range_str = xsd_type
        else:
            range_str = f"xsd:{xsd_type}"

        lines.extend([
            f"{IOI_EXT_PREFIX}:{prop_name} a owl:DatatypeProperty ;",
            f"    rdfs:domain {IOI_EXT_PREFIX}:{facet_name} ;",
            f"    rdfs:range {range_str} ;",
            f'    rdfs:label "{col_desc}"@en ;',
            f'    rdfs:comment "Property from {artifact_name}: {col_desc}"@en .',
            "",
        ])

    return "\n".join(lines)


def generate_facet_jsonld(
    artifact_name: str,
    columns: list[dict],
    sample_row: Optional[dict] = None,
) -> dict:
    """
    Generate a JSON-LD Facet fragment (for embedding in a graph).

    Args:
        artifact_name: e.g., 'SRUM'
        columns: list of {clean_name, inferred_type, column_name} from type_inferencer
        sample_row: optional dict of {column_name: value} to populate

    Returns:
        JSON-LD dict for the Facet instance.
    """
    import uuid

    facet_name = _to_facet_name(artifact_name)
    facet = {
        "@id": f"kb:{facet_name.lower()}-{uuid.uuid4()}",
        "@type": f"{IOI_EXT_PREFIX}:{facet_name}",
    }

    for col in columns:
        prop_name = _to_property_name(artifact_name, col["clean_name"])
        prefixed_prop = f"{IOI_EXT_PREFIX}:{prop_name}"
        xsd_type = col["inferred_type"]

        # Get value from sample row or use empty placeholder
        value = None
        if sample_row and col["column_name"] in sample_row:
            value = sample_row[col["column_name"]]

        if xsd_type in ("xsd:integer", "xsd:decimal"):
            if value is not None:
                try:
                    value = int(value) if xsd_type == "xsd:integer" else float(value)
                except (ValueError, TypeError):
                    value = 0
            facet[prefixed_prop] = {
                "@type": xsd_type,
                "@value": value if value is not None else 0,
            }
        elif xsd_type == "xsd:dateTime":
            facet[prefixed_prop] = {
                "@type": xsd_type,
                "@value": str(value) if value else "",
            }
        elif xsd_type == "xsd:boolean":
            facet[prefixed_prop] = {
                "@type": xsd_type,
                "@value": str(value).lower() in ("true", "1") if value else False,
            }
        elif xsd_type == "xsd:hexBinary":
            facet[prefixed_prop] = {
                "@type": xsd_type,
                "@value": str(value) if value else "",
            }
        else:
            # xsd:string and anything else
            facet[prefixed_prop] = str(value) if value else ""

    return facet


def get_extension_property_list(artifact_name: str, columns: list[dict]) -> list[dict]:
    """
    Get the list of extension properties (for tool output).
    """
    props = []
    for col in columns:
        prop_name = _to_property_name(artifact_name, col["clean_name"])
        props.append({
            "name": f"{IOI_EXT_PREFIX}:{prop_name}",
            "local_name": prop_name,
            "range": col["inferred_type"],
            "range_type": "datatype",
            "max_count": 1,
            "is_array": False,
            "source_column": col["column_name"],
        })
    return props
