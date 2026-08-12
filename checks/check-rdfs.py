#!/usr/bin/env python3
"""Compare the RDFS generated from schema/saref-core.yaml against source/SAREFCore/saref.ttl.

Only checks what RDFS itself expresses about hierarchy: rdfs:subClassOf between named
classes and rdfs:subPropertyOf between named properties. Domain and range are deliberately
out of scope - saref.ttl states them in OWL terms (owl:unionOf, per-class owl:Restriction),
which has no faithful RDFS counterpart. Cardinality, inverses, property chains, functional
properties and annotations are likewise never reported as differences.

A property saref.ttl declares but the generated RDFS omits is only a real loss if the LinkML
schema does not declare it either: the RDFS generator prunes slots no class carries, so it
drops terms the translation kept. Those are checked against schema/saref-core.yaml and
reported as mismatches only when the schema has no slot for them.

The rdfs:subPropertyOf comparison is currently disabled and reported as skipped; pass
--check-slots to run it. See CHECK_SLOT_INHERITANCE below.

RDFS comes from `--rdfs` when given, else from `linkml-scala generate rdfs` - so CI can pass an
already-generated file and needs no linkml-scala binary.
"""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml
from rdflib import Graph, RDF, RDFS, URIRef
from rdflib.namespace import OWL

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "schema" / "saref-core.yaml"
SAREF = ROOT / "source" / "SAREFCore" / "saref.ttl"

SKOS = "http://www.w3.org/2004/02/skos/core#"
# saref.ttl never declares these as properties; they only appear as annotations.
IGNORED = {RDFS.label, RDFS.comment}

# Slot (property) inheritance is checked but disabled by default: saref-core.yaml declares no
# is_a on any slot, so the generated RDFS carries no rdfs:subPropertyOf and every pair in
# saref.ttl reports as MISSING. Flip to True - or pass --check-slots - once the schema models
# the slot hierarchy.
CHECK_SLOT_INHERITANCE = False

PREFIXES = {
    "https://saref.etsi.org/core/": "saref:",
    "https://saref.etsi.org/saref4syst/": "s4syst:",
    "http://www.w3.org/2006/time#": "time:",
    "http://www.w3.org/2001/XMLSchema#": "xsd:",
    "http://www.w3.org/2000/01/rdf-schema#": "rdfs:",
    SKOS: "skos:",
}


def qname(term):
    for base, pfx in PREFIXES.items():
        if str(term).startswith(base):
            return pfx + str(term)[len(base):]
    return f"<{term}>"

def generate_rdfs():
    out = Path(tempfile.mkdtemp(prefix="rdfs-check-")) / "generated.rdfs.ttl"
    subprocess.run(
        ["linkml-scala", "generate", "rdfs", "--format", "ttl", "--to", str(out), str(SCHEMA)],
        check=True,
    )
    return out

def named_hierarchy(graph, predicate):
    """subject/object pairs of `predicate` where both ends are named terms.

    Blank-node objects are skipped: in saref.ttl they are owl:Restriction axioms, which
    say nothing about the class hierarchy.
    """
    return {
        (s, o)
        for s, o in graph.subject_objects(predicate)
        if isinstance(s, URIRef) and isinstance(o, URIRef)
    }


def read_reference(graph):
    classes = {s for s in graph.subjects(RDF.type, OWL.Class) if isinstance(s, URIRef)}
    props = {
        s
        for t in (OWL.ObjectProperty, OWL.DatatypeProperty)
        for s in graph.subjects(RDF.type, t)
        if isinstance(s, URIRef)
    }
    return (
        classes,
        props,
        named_hierarchy(graph, RDFS.subClassOf),
        named_hierarchy(graph, RDFS.subPropertyOf),
    )

def read_schema_slots(path):
    """Every slot_uri the LinkML schema declares, resolved to a full IRI.

    This is the fallback the RDFS output cannot provide: a slot no class carries is pruned
    from the generated RDFS, so only the schema itself can say whether a property survived
    the translation.
    """
    schema = yaml.safe_load(path.read_text())
    prefixes = schema.get("prefixes") or {}
    definitions = list((schema.get("slots") or {}).values())
    for cls in (schema.get("classes") or {}).values():
        definitions += list((cls.get("attributes") or {}).values())

    uris = set()
    for definition in definitions:
        curie = definition.get("slot_uri")
        if not curie:
            continue
        prefix, _, local = curie.partition(":")
        uris.add(URIRef(prefixes[prefix] + local) if prefix in prefixes else URIRef(curie))
    return uris

def read_generated(graph):
    classes = {s for s in graph.subjects(RDF.type, RDFS.Class) if isinstance(s, URIRef)}
    props = {s for s in graph.subjects(RDF.type, RDF.Property) if isinstance(s, URIRef)}
    return (
        classes,
        props,
        named_hierarchy(graph, RDFS.subClassOf),
        named_hierarchy(graph, RDFS.subPropertyOf),
    )

def report_hierarchy(predicate, ref_pairs, gen_pairs, scope):
    """Report both directions of difference; returns the number of mismatches.

    `scope` limits the comparison to terms both files declare, so that a class or property
    only one side knows about is reported once under coverage rather than again here.
    """
    ref_pairs = {(s, o) for s, o in ref_pairs if s in scope and o in scope}
    gen_pairs = {(s, o) for s, o in gen_pairs if s in scope and o in scope}

    print(f"\n=== inheritance: rdfs:{predicate} (named terms only) ===")
    print(f"  shared pairs: {len(ref_pairs & gen_pairs)}")
    failures = 0
    for sub, sup in sorted(ref_pairs - gen_pairs, key=str):
        failures += 1
        print(f"  MISSING  {qname(sub)} rdfs:{predicate} {qname(sup)}")
    for sub, sup in sorted(gen_pairs - ref_pairs, key=str):
        failures += 1
        print(f"  EXTRA    {qname(sub)} rdfs:{predicate} {qname(sup)}")
    return failures


def report_dropped_properties(dropped, schema_slots):
    """Every property missing from the generated RDFS must still exist in the LinkML schema.

    The generator prunes slots no class carries, so absence from the RDFS is not by itself a
    translation loss. Absence from the schema is: nothing downstream can recover the property.
    """
    print(f"\n=== properties in {SAREF.name} but not in the generated RDFS ===")
    if not dropped:
        print("  none")
        return 0

    failures = 0
    for prop in sorted(dropped, key=str):
        if prop in schema_slots:
            print(f"  pruned   {qname(prop)} - carried by no class, declared in {SCHEMA.name}")
        else:
            failures += 1
            print(f"  MISMATCH {qname(prop)} - absent from the RDFS and from {SCHEMA.name}")
    return failures


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rdfs", default=None,
                    help="pre-generated RDFS; skips calling linkml-scala")
    ap.add_argument("--check-slots", action="store_true", default=CHECK_SLOT_INHERITANCE,
                    help="also compare rdfs:subPropertyOf (disabled by default, see module docstring)")
    args = ap.parse_args()

    generated = Path(args.rdfs) if args.rdfs else generate_rdfs()
    ref, gen = Graph(), Graph()
    ref.parse(SAREF, format="turtle")
    gen.parse(generated)          # format inferred from the extension, so .ttl or .nt both work

    ref_classes, ref_props, ref_subclass, ref_subprop = read_reference(ref)
    gen_classes, gen_props, gen_subclass, gen_subprop = read_generated(gen)

    print(f"reference: {SAREF.name} - {len(ref_classes)} classes, {len(ref_props)} properties")
    print(f"generated: {generated.name} - {len(gen_classes)} classes, {len(gen_props)} properties")

    failures = report_hierarchy(
        "subClassOf", ref_subclass, gen_subclass, ref_classes & gen_classes
    )

    slot_scope = (ref_props & gen_props) - IGNORED
    if args.check_slots:
        failures += report_hierarchy("subPropertyOf", ref_subprop, gen_subprop, slot_scope)
    else:
        pending = {(s, o) for s, o in ref_subprop if s in slot_scope and o in slot_scope}
        print("\n=== inheritance: rdfs:subPropertyOf (named terms only) ===")
        print("  DISABLED - not compared, not counted below. Pass --check-slots to enable.")
        print(f"  saref.ttl declares {len(pending)} pair(s) that would be compared.")

    failures += report_dropped_properties(ref_props - gen_props, read_schema_slots(SCHEMA))

    print("\n=== coverage ===")
    for c in sorted(ref_classes - gen_classes, key=str):
        print(f"  class only in saref.ttl:  {qname(c)}")
    for c in sorted(gen_classes - ref_classes, key=str):
        print(f"  class only in generated:  {qname(c)}")
    for p in sorted((gen_props - ref_props) - IGNORED, key=str):
        print(f"  property only in generated: {qname(p)}")
    for p in sorted(gen_props & IGNORED, key=str):
        print(f"  property ignored, saref.ttl uses it only as an annotation: {qname(p)}")

    scope = "class and property inheritance" if args.check_slots else "class inheritance"
    print(f"\n{failures} mismatch(es) in {scope} and property coverage.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
