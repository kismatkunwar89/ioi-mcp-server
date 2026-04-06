"""
Type inferencer — infers xsd types from CSV column values.
Case-agnostic: works on any CSV regardless of artifact type.
Uses pattern matching on actual values, not column names.
"""

import csv
import re
from pathlib import Path
from typing import Optional

# Patterns ordered by specificity (most specific first)
_PATTERNS = [
    # ISO 8601 datetime variants
    (re.compile(
        r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
    ), "xsd:dateTime"),

    # Date only (YYYY-MM-DD)
    (re.compile(r"^\d{4}-\d{2}-\d{2}$"), "xsd:date"),

    # Boolean
    (re.compile(r"^(true|false|True|False|TRUE|FALSE|0|1)$"), "xsd:boolean"),

    # Hex binary (common in forensics: hashes, SIDs encoded)
    (re.compile(r"^(0x)?[0-9a-fA-F]{32,}$"), "xsd:hexBinary"),

    # Integer (including negative)
    (re.compile(r"^-?\d+$"), "xsd:integer"),

    # Decimal / float
    (re.compile(r"^-?\d+\.\d+$"), "xsd:decimal"),

    # Everything else is string
]


def infer_xsd_type(values: list[str]) -> str:
    """
    Infer the xsd type from a list of sample values.
    Skips empty/null values. Uses majority vote.
    """
    type_votes: dict[str, int] = {}

    for val in values:
        val = val.strip()
        if not val or val.lower() in ("", "null", "none", "n/a", "-"):
            continue

        matched_type = "xsd:string"  # default
        for pattern, xsd_type in _PATTERNS:
            if pattern.match(val):
                matched_type = xsd_type
                break

        type_votes[matched_type] = type_votes.get(matched_type, 0) + 1

    if not type_votes:
        return "xsd:string"

    # Return the most common type
    return max(type_votes, key=type_votes.get)


def _to_camel_case(name: str) -> str:
    """Convert column name to camelCase property name.
    'BytesSent' -> 'bytesSent'
    'bytes_sent' -> 'bytesSent'
    'BYTES_SENT' -> 'bytesSent'
    'Bytes Sent' -> 'bytesSent'
    """
    # Handle snake_case and space-separated
    parts = re.split(r'[_\s]+', name)
    if len(parts) > 1:
        return parts[0].lower() + "".join(p.capitalize() for p in parts[1:])

    # Handle PascalCase -> camelCase
    if name[0].isupper() and not name.isupper():
        return name[0].lower() + name[1:]

    # Handle ALL_CAPS
    if name.isupper():
        return name.lower()

    return name


def _sanitize_property_name(name: str) -> str:
    """Remove non-alphanumeric characters, ensure valid IRI local name."""
    clean = re.sub(r'[^a-zA-Z0-9]', '', name)
    if not clean:
        clean = "unknownProperty"
    return clean


def analyze_csv(csv_path: str, max_sample_rows: int = 50) -> list[dict]:
    """
    Analyze a CSV file and return column metadata.

    Returns list of:
    {
        "column_name": original header,
        "clean_name": camelCase version,
        "inferred_type": xsd type,
        "sample_values": first N non-empty values,
        "non_null_count": how many rows have values,
        "total_rows": total data rows read,
    }
    """
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError(f"CSV has no headers: {csv_path}")

        headers = list(reader.fieldnames)

        # Collect sample values per column
        column_values: dict[str, list[str]] = {h: [] for h in headers}
        row_count = 0

        for row in reader:
            row_count += 1
            if row_count > max_sample_rows:
                break
            for h in headers:
                val = row.get(h, "")
                if val and val.strip():
                    column_values[h].append(val.strip())

    results = []
    for header in headers:
        values = column_values[header]
        clean = _to_camel_case(_sanitize_property_name(header))
        xsd_type = infer_xsd_type(values)

        results.append({
            "column_name": header,
            "clean_name": clean,
            "inferred_type": xsd_type,
            "sample_values": values[:5],
            "non_null_count": len(values),
            "total_rows": min(row_count, max_sample_rows),
        })

    return results
