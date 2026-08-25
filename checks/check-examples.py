#!/usr/bin/env python3
"""Validate the official SAREF examples against SHACL shapes generated from the LinkML schema.

Note: examples may be patched to align with SHACL restrictions. See source .ttl for added statements.

Both modes gate; a failure in either exits nonzero:
  open    only declared constraints are checked. A failure is an over-constraint.
  closed  also rejects any property the schema does not declare. A failure is under-coverage.
"""

import argparse
import subprocess
import sys
from collections import Counter
from pathlib import Path

from rdflib import Graph, Literal, Namespace
from rdflib.namespace import RDF

SH = Namespace("http://www.w3.org/ns/shacl#")
ROOT = Path(__file__).resolve().parents[1]


def generate_shapes(schema, mode, build):
    """Generate shapes for `mode` straight from `schema`. Only used when --shapes is not supplied.
    
    """
    try:
        import linkml_scala
    except ImportError:
        sys.exit("neverblink-linkml is not installed - pip install neverblink-linkml")

    try:
        with linkml_scala.load_file(schema) as loaded:
            triples = loaded.shacl(open=(mode == "open"))
    except linkml_scala.LinkMlError as error:
        sys.exit(f"generating SHACL from {schema} failed:\n{error}")

    graph = Graph()
    graph.parse(data=triples, format="nt")
    out = build / f"shapes-{mode}.ttl"
    graph.serialize(destination=out, format="turtle")
    return out


def shapes_for(source, mode, build):
    """Re-serialize a supplied shapes file as Turtle, checking it was generated for `mode`.

    """
    graph = Graph()
    graph.parse(source)          # format inferred from the extension, so .ttl or .nt both work
    found = {o for _, _, o in graph.triples((None, SH.closed, None))}
    wanted = Literal(mode == "closed")
    if found and found != {wanted}:
        carries = ", ".join(sorted(str(o) for o in found))
        sys.exit(f"{source} carries sh:closed {carries}, but --mode {mode} needs sh:closed "
                 f"{str(wanted.value).lower()}; regenerate the shapes with open="
                 f"{str(mode == 'open').lower()}")

    out = build / f"shapes-{mode}.ttl"
    graph.serialize(destination=out, format="turtle")
    return out


def validate(shapes, data):
    """Run SHACL validation, returning [(focus, path, component, message)] for violations."""
    report = Graph()
    try:
        from pyshacl import validate as pyshacl_validate
        _, report_graph, _ = pyshacl_validate(
            str(data), shacl_graph=str(shapes), data_graph_format="turtle",
            shacl_graph_format="turtle", advanced=False, inplace=False)
        report = report_graph
    except ImportError:
        result = subprocess.run(["shacl", "validate", "--shapes", str(shapes), "--data", str(data)],
                                capture_output=True, text=True)
        if not result.stdout:
            sys.exit(f"shacl validate failed for {data}:\n{result.stderr}")
        report.parse(data=result.stdout, format="turtle")

    findings = []
    for result in report.subjects(RDF.type, SH.ValidationResult):
        severity = report.value(result, SH.resultSeverity)
        if severity and severity != SH.Violation:
            continue
        findings.append((
            short(report.value(result, SH.focusNode)),
            short(report.value(result, SH.resultPath)),
            short(report.value(result, SH.sourceConstraintComponent)).replace("ConstraintComponent", ""),
            str(report.value(result, SH.resultMessage) or "").strip(),
        ))
    return findings


def short(node):
    if node is None:
        return "-"
    text = str(node)
    for prefix, ns in (("saref:", "https://saref.etsi.org/core/"),
                       ("skos:", "http://www.w3.org/2004/02/skos/core#"),
                       ("sh:", "http://www.w3.org/ns/shacl#"),
                       ("rdfs:", "http://www.w3.org/2000/01/rdf-schema#"),
                       ("dcterms:", "http://purl.org/dc/terms/"),
                       ("time:", "http://www.w3.org/2006/time#")):
        if text.startswith(ns):
            return prefix + text[len(ns):]
    if "/example/" in text:
        return "ex:" + text.rsplit("/", 1)[-1].lstrip("#")
    return text


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--schema", default=ROOT / "schema/saref-core.yaml")
    ap.add_argument("--shapes", default=None,
                    help="pre-generated SHACL, which must have been generated for --mode; "
                         "skips generating it from --schema")
    ap.add_argument("--examples", default=ROOT / "source/SAREFCore/examples")
    ap.add_argument("--build", default=ROOT / "build")
    ap.add_argument("--mode", choices=("open", "closed"), default="open")
    ap.add_argument("-v", "--verbose", action="store_true", help="list every violation")
    args = ap.parse_args()

    examples = sorted(Path(args.examples).glob("*.ttl"))
    if not examples:
        sys.exit(f"no .ttl examples found in {args.examples}")

    build = Path(args.build)
    build.mkdir(parents=True, exist_ok=True)
    if args.shapes:
        shapes = shapes_for(Path(args.shapes), args.mode, build)
    else:
        shapes = generate_shapes(Path(args.schema), args.mode, build)

    print(f"shapes:   {shapes} ({args.mode})")
    print(f"examples: {len(examples)} from {args.examples}\n")

    by_component, failures = Counter(), []
    for path in examples:
        found = validate(shapes, path)
        for _, _, component, _ in found:
            by_component[component] += 1
        failures += [(path.name, *f) for f in found]

        print(f"  {'FAIL' if found else 'ok  '} {path.name}"
              + (f"  {len(found)} violation(s)" if found else ""))
        if args.verbose:
            for focus, slot, component, message in found:
                print(f"         {focus} {slot} [{component}] {message}")

    files_failed = len({f[0] for f in failures})
    print(f"\n{len(examples) - files_failed}/{len(examples)} examples pass"
          f"  ({len(failures)} violation(s))")

    if failures:
        print("\nviolations by constraint:")
        for component, count in by_component.most_common():
            print(f"  {count:4}  {component}")
        if args.mode == "closed":
            print("\nThe schema does not declare something the official examples use. Add the "
                  "missing slot, or patch the example and record why in its source .ttl.")
        else:
            print("\nThe schema over-constrains SAREF: these shapes forbid something the official "
                  "examples do. Fix the schema, or patch the example and record why in its "
                  "source .ttl.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())