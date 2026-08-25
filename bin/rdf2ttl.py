#!/usr/bin/env python3
"""Rewrite generated RDF as prefixed Turtle.

    bin/rdf2ttl.py --schema-dir schema --in build/shacl --out dist/shacl --suffix shacl
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml
from rdflib import Graph

# Bound on every graph.
COMMON = {
    "sh": "http://www.w3.org/ns/shacl#",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "owl": "http://www.w3.org/2002/07/owl#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "linkml": "https://w3id.org/linkml/",
    "skos": "http://www.w3.org/2004/02/skos/core#",
    "dcterms": "http://purl.org/dc/terms/",
}


def prefixes_for(schema: Path) -> dict[str, str]:
    """Prefixes declared by the schema, plus the ones its imports bring in."""
    declared = dict(COMMON)
    for path in sorted(schema.parent.glob("*.yaml")):
        loaded = yaml.safe_load(path.read_text()) or {}
        for name, uri in (loaded.get("prefixes") or {}).items():
            declared.setdefault(name, uri)
    return declared


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--schema-dir", type=Path, required=True)
    parser.add_argument("--in", dest="source", type=Path, required=True,
                        help="directory of generated RDF, one file per schema")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--suffix", required=True,
                        help="goes into the file name, e.g. 'shacl' -> saref-core.shacl.ttl")
    args = parser.parse_args()

    inputs = sorted(p for p in args.source.iterdir() if p.is_file())
    if not inputs:
        sys.exit(f"nothing to convert in {args.source}")

    args.out.mkdir(parents=True, exist_ok=True)
    for path in inputs:
        # The generator names its output after the schema, but the extension it picks is
        # its own business - take the leading name and drop everything after the first dot.
        name = path.name.split(".")[0]
        schema = args.schema_dir / f"{name}.yaml"
        if not schema.exists():
            sys.exit(f"{path}: no schema {schema} to take prefixes from")

        graph = Graph()
        graph.parse(path, format="nt")
        for prefix, uri in prefixes_for(schema).items():
            graph.bind(prefix, uri, replace=True)

        target = args.out / f"{name}.{args.suffix}.ttl"
        graph.serialize(destination=target, format="turtle")
        print(f"{target}: {len(graph)} triples")
    return 0


if __name__ == "__main__":
    sys.exit(main())
