#!/usr/bin/env python3
"""Validate the official SAREF examples against SHACL shapes generated from the LinkML schema.

    ./checks/check_examples.py --mode open      # shapes must not forbid what SAREF permits
    ./checks/check_examples.py --mode closed    # shapes must declare everything SAREF uses
    ./checks/check_examples.py --shapes build/saref-core.shacl.ttl --mode open   # reuse in CI

The examples are the only external check on the conversion: they were written against SAREF itself,
by people who did not know this schema exists. A violation means our shapes forbid something SAREF
permits, so the schema is wrong - not the example.

Both modes gate; a failure in either exits nonzero:
  open    only declared constraints are checked. A failure is an over-constraint.
  closed  also rejects any property the schema does not declare. A failure is under-coverage.
"""

import argparse
import subprocess
import sys
from collections import Counter
from pathlib import Path

import yaml
from rdflib import Graph, Literal, Namespace
from rdflib.namespace import RDF

SH = Namespace("http://www.w3.org/ns/shacl#")
ROOT = Path(__file__).resolve().parents[1]


def generate_shapes(schema, out):
    """Generate closed shapes with the CLI. Only used when --shapes is not supplied."""
    # linkml-scala's --to does not truncate: writing shorter output over a longer existing file
    # leaves the old tail behind and yields corrupt Turtle. Remove it first.
    out.unlink(missing_ok=True)
    result = subprocess.run(
        ["linkml-scala", "generate", "shacl", "--format", "ttl", "--to", str(out), str(schema)],
        capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"linkml-scala generate shacl failed:\n{result.stderr or result.stdout}")
    return out


def shapes_for(source, mode, build):
    """Write a shapes file for `mode`, derived from a default (closed) generation.

    `--open` sets sh:closed false on every node shape, and that is its only effect - verified by
    diffing both generator outputs on 0.12.1. So open mode is derived by flipping, and closed mode
    is the generated file untouched.

    Untouched matters: the default generation already emits sh:closed false for the 5 mixin classes,
    because a mixin's instances carry the mixing class's properties too. Forcing those to true would
    make closed mode stricter than linkml-scala ever is, and invent failures.
    """
    graph = Graph()
    graph.parse(source)          # format inferred from the extension, so .ttl or .nt both work
    closed = list(graph.triples((None, SH.closed, None)))
    if not any(o == Literal(True) for _, _, o in closed):
        print(f"warning: {source} has no sh:closed true - it looks like an --open generation, "
              f"so closed mode cannot be derived from it", file=sys.stderr)

    flipped = 0
    if mode == "open":
        for shape, _, current in closed:
            if current != Literal(False):
                graph.set((shape, SH.closed, Literal(False)))
                flipped += 1

    out = build / f"shapes-{mode}.ttl"
    graph.serialize(destination=out, format="turtle")
    return out, flipped


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


def load_expected(path):
    """(example, path, constraint) -> reason, from the expected-violations file."""
    if not path or not Path(path).exists():
        return {}
    out = {}
    for entry in yaml.safe_load(Path(path).read_text()) or []:
        for example in entry["examples"]:
            out[(example, entry["path"], entry["constraint"])] = " ".join(entry["reason"].split())
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--schema", default=ROOT / "schema/saref-core.yaml")
    ap.add_argument("--shapes", default=None,
                    help="pre-generated SHACL (either mode); skips calling linkml-scala")
    ap.add_argument("--examples", default=ROOT / "source/SAREFCore/examples")
    ap.add_argument("--build", default=ROOT / "build")
    ap.add_argument("--mode", choices=("open", "closed"), default="open")
    ap.add_argument("--expected", default=ROOT / "tests/expected-violations.yaml",
                    help="known-acceptable violations; pass '' to treat every violation as a failure")
    ap.add_argument("-v", "--verbose", action="store_true", help="list every violation")
    args = ap.parse_args()

    examples = sorted(Path(args.examples).glob("*.ttl"))
    if not examples:
        sys.exit(f"no .ttl examples found in {args.examples}")

    build = Path(args.build)
    build.mkdir(parents=True, exist_ok=True)
    source = Path(args.shapes) if args.shapes else generate_shapes(args.schema, build / "shapes.ttl")
    shapes, flipped = shapes_for(source, args.mode, build)
    expected = load_expected(args.expected)

    print(f"shapes:   {shapes} ({args.mode}"
          + (f", sh:closed flipped on {flipped} shapes)" if flipped else ")"))
    print(f"examples: {len(examples)} from {args.examples}\n")

    by_component, known, unexpected, fired = Counter(), 0, [], set()
    for path in examples:
        new, accepted = [], 0
        for focus, slot, component, message in validate(shapes, path):
            key = (path.name, slot, component)
            if key in expected:
                known += 1
                accepted += 1
                fired.add(key)
            else:
                new.append((focus, slot, component, message))
                by_component[component] += 1
        unexpected += [(path.name, *f) for f in new]

        if new:
            mark, detail = "FAIL", f"  {len(new)} unexpected"
        else:
            mark, detail = "ok  ", f"  ({accepted} known)" if accepted else ""
        print(f"  {mark} {path.name}{detail}")
        if args.verbose:
            for focus, slot, component, message in new:
                print(f"         {focus} {slot} [{component}] {message}")

    files_failed = len({u[0] for u in unexpected})
    print(f"\n{len(examples) - files_failed}/{len(examples)} examples pass"
          f"  ({known} known violation(s) accepted, {len(unexpected)} unexpected)")

    stale = set(expected) - fired
    if stale:
        print(f"\n{len(stale)} expectation(s) no longer fire - remove them from {args.expected}:")
        for example, slot, component in sorted(stale):
            print(f"  {example}  {slot}  [{component}]")

    if unexpected:
        print("\nunexpected violations by constraint:")
        for component, count in by_component.most_common():
            print(f"  {count:4}  {component}")
        if args.mode == "closed":
            print(f"\nThe schema does not declare something the official examples use. Add the "
                  f"missing slot, or record the mismatch with a reason in {args.expected}.")
        else:
            print(f"\nThe schema over-constrains SAREF: these shapes forbid something the official "
                  f"examples do. Fix the schema, or record the mismatch in {args.expected}.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())