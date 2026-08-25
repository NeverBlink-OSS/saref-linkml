#!/usr/bin/env python3
"""Compare the RDFS generated from a LinkML schema against the ontology it was converted from.

Defaults to schema/saref-core.yaml and source/SAREFCore/saref.ttl; pass --schema and --source
to check an extension instead.

Only checks what RDFS itself expresses about hierarchy: rdfs:subClassOf.

A property saref.ttl declares but the generated RDFS omits is only a real loss if the LinkML
schema does not declare it either.

Two comparisons are currently disabled and reported as skipped, see CHECK_SLOT_INHERITANCE
and CHECK_ENUM_INHERITANCE below.

"""

import argparse
import sys
from pathlib import Path

import yaml
from rdflib import Graph, RDF, RDFS, URIRef
from rdflib.namespace import OWL

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "schema" / "saref-core.yaml"
SAREF = ROOT / "source" / "SAREFCore" / "saref.ttl"
MAPPING = ROOT / "mapping" / "mapping_table.yaml"

SKOS = "http://www.w3.org/2004/02/skos/core#"
CHECK_SLOT_INHERITANCE = False
CHECK_ENUM_INHERITANCE = False

PREFIXES = {
    "https://saref.etsi.org/core/": "saref:",
    "https://saref.etsi.org/saref4syst/": "s4syst:",
    "https://saref.etsi.org/saref4bldg/": "s4bldg:",
    "https://saref.etsi.org/saref4ener/": "s4ener:",
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

def generate_rdfs(schema):
    """The RDFS for `schema`, as N-Triples text.

    """
    try:
        import linkml_scala
    except ImportError:
        sys.exit("neverblink-linkml is not installed - pip install neverblink-linkml")

    try:
        with linkml_scala.load_file(schema) as loaded:
            return loaded.rdfs()
    except linkml_scala.LinkMlError as error:
        sys.exit(f"generating RDFS from {schema} failed:\n{error}")

def named_hierarchy(graph, predicate):
    """subject/object pairs of `predicate` where both ends are named terms.

    Blank-node objects are skipped.
    """
    return {
        (s, o)
        for s, o in graph.subject_objects(predicate)
        if isinstance(s, URIRef) and isinstance(o, URIRef)
    }


def read_reference(graph):
    # rdfs:Class as well as owl:Class.
    classes = {
        s
        for t in (OWL.Class, RDFS.Class)
        for s in graph.subjects(RDF.type, t)
        if isinstance(s, URIRef)
    }
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

def expand(curie, prefixes):
    prefix, _, local = curie.partition(":")
    return URIRef(prefixes[prefix] + local) if prefix in prefixes else URIRef(curie)


def read_schema_slots(path):
    """Every slot_uri the LinkML schema declares, resolved to a full IRI.

    """
    schema = yaml.safe_load(path.read_text())
    prefixes = schema.get("prefixes") or {}
    definitions = list((schema.get("slots") or {}).values())
    for cls in (schema.get("classes") or {}).values():
        definitions += list((cls.get("attributes") or {}).values())

    return {expand(d["slot_uri"], prefixes) for d in definitions if d.get("slot_uri")}


def read_schema_enums(path):
    """Every enum_uri the LinkML schema declares, resolved to a full IRI.

    """
    schema = yaml.safe_load(path.read_text())
    prefixes = schema.get("prefixes") or {}
    enums = (schema.get("enums") or {}).values()
    return {expand(e["enum_uri"], prefixes) for e in enums if e.get("enum_uri")}

def read_ignored(path, terms, graph):
    """The `report_ignored` terms among `terms`, each mapped to the reason the table gives.

    """
    if not path.exists():
        print(f"  note: {path} not found, no term is treated as intentionally ignored")
        return {}
    ignored = (yaml.safe_load(path.read_text()) or {}).get("report_ignored") or {}
    found = {}
    for term in terms:
        try:
            prefix, _, local = graph.compute_qname(term)
        except ValueError:
            continue  # no namespace/name split; the table cannot name it either
        if (reason := ignored.get(f"{prefix}:{local}")) is not None:
            found[term] = reason
    return found


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


def report_skipped_hierarchy(predicate, ref_pairs, scope, source, flag, note):
    """Announce a comparison that is switched off, and how much it would have covered."""
    pending = {(s, o) for s, o in ref_pairs if s in scope and o in scope}
    print(f"\n=== inheritance: rdfs:{predicate} ({note}) ===")
    print(f"  DISABLED - not compared, not counted below. Pass {flag} to enable.")
    print(f"  {source.name} declares {len(pending)} pair(s) that would be compared.")


def report_dropped_properties(dropped, schema_slots, source, schema, ignored):
    """Every property missing from the generated RDFS must still exist in the LinkML schema.

    The generator prunes slots no class carries, so absence from the RDFS is not by itself a
    translation loss. Absence from the schema is.
    """
    print(f"\n=== properties in {source.name} but not in the generated RDFS ===")
    if not dropped:
        print("  none")
        return 0

    failures = 0
    for prop in sorted(dropped, key=str):
        if prop in schema_slots:
            print(f"  pruned   {qname(prop)} - carried by no class, declared in {schema.name}")
        elif prop in ignored:
            print(f"  ignored  {qname(prop)} - {ignored[prop]}")
        else:
            failures += 1
            print(f"  MISMATCH {qname(prop)} - absent from the RDFS and from {schema.name}")
    return failures


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rdfs", default=None,
                    help="pre-generated RDFS; skips generating it from --schema")
    ap.add_argument("--schema", type=Path, default=SCHEMA,
                    help="the LinkML schema the RDFS was generated from")
    ap.add_argument("--source", type=Path, default=SAREF,
                    help="the source ontology to compare against")
    ap.add_argument("--mapping", type=Path, default=MAPPING,
                    help="the mapping table whose report_ignored rows name the terms left out "
                         "on purpose; the converter reads the same file")
    ap.add_argument("--namespace", default=None,
                    help="report coverage only for terms under this IRI; defaults to the "
                         "source's own owl:Ontology IRI, so imported terms are not listed")
    ap.add_argument("--check-slots", action="store_true", default=CHECK_SLOT_INHERITANCE,
                    help="also compare rdfs:subPropertyOf (disabled by default, see module docstring)")
    ap.add_argument("--check-enums", action="store_true", default=CHECK_ENUM_INHERITANCE,
                    help="also compare the rdfs:subClassOf pairs of terms the schema models as "
                         "enums (disabled by default, see module docstring)")
    args = ap.parse_args()

    ref, gen = Graph(), Graph()
    ref.parse(args.source, format="turtle")
    if args.rdfs:
        generated = Path(args.rdfs).name
        gen.parse(args.rdfs)      # format inferred from the extension, so .ttl or .nt both work
    else:
        generated = f"generated from {args.schema.name}"
        gen.parse(data=generate_rdfs(args.schema), format="nt")

    ref_classes, ref_props, ref_subclass, ref_subprop = read_reference(ref)
    gen_classes, gen_props, gen_subclass, gen_subprop = read_generated(gen)

    print(f"reference: {args.source.name} - {len(ref_classes)} classes, {len(ref_props)} properties")
    print(f"generated: {generated} - {len(gen_classes)} classes, {len(gen_props)} properties")

    ignored = read_ignored(args.mapping, ref_classes | ref_props, ref)

    class_scope = ref_classes & gen_classes
    enum_uris = read_schema_enums(args.schema)
    if args.check_enums:
        failures = report_hierarchy("subClassOf", ref_subclass, gen_subclass, class_scope)
    else:
        failures = report_hierarchy(
            "subClassOf", ref_subclass, gen_subclass, class_scope - enum_uris
        )
        report_skipped_hierarchy(
            "subClassOf", {(s, o) for s, o in ref_subclass if s in enum_uris or o in enum_uris},
            class_scope, args.source, "--check-enums", "terms the schema models as enums",
        )

    slot_scope = (ref_props & gen_props)
    if args.check_slots:
        failures += report_hierarchy("subPropertyOf", ref_subprop, gen_subprop, slot_scope)
    else:
        report_skipped_hierarchy("subPropertyOf", ref_subprop, slot_scope, args.source,
                                 "--check-slots", "named terms only")

    failures += report_dropped_properties(ref_props - gen_props, read_schema_slots(args.schema),
                                          args.source, args.schema, ignored)

    # Resolve imports
    own = args.namespace or str(next(ref.subjects(RDF.type, OWL.Ontology), ""))
    mine = (lambda t: str(t).startswith(own)) if own else (lambda t: True)
    inherited = sum(1 for c in gen_classes - ref_classes if not mine(c))

    print("\n=== coverage ===")
    if own:
        print(f"  scoped to {own}"
              + (f" ({inherited} imported class(es) not listed)" if inherited else ""))
    for c in sorted(ref_classes - gen_classes, key=str):
        if c in ignored:
            print(f"  class ignored, the mapping table leaves it out: {qname(c)} - {ignored[c]}")
        else:
            print(f"  class only in {args.source.name}:  {qname(c)}")
    for c in sorted(filter(mine, gen_classes - ref_classes), key=str):
        print(f"  class only in generated:  {qname(c)}")
    for p in sorted(filter(mine, (gen_props - ref_props)), key=str):
        print(f"  property only in generated: {qname(p)}")

    checked = ["class inheritance"]
    if args.check_enums:
        checked.append("enum inheritance")
    if args.check_slots:
        checked.append("property inheritance")
    print(f"\n{failures} mismatch(es) in {', '.join(checked)} and property coverage.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
