#!/usr/bin/env python3
"""Compare the RDFS generated from schema/saref-core.yaml against source/SAREFCore/saref.ttl.

Checks inheritance, rdfs:domain and rdfs:range. Range comparison is skipped for slots
whose LinkML definition (or slot_usage) uses any_of. Cardinality, inverses, property chains, functional properties,
allValuesFrom and annotations are out of scope and are never reported as differences.

RDFS comes from `--rdfs` when given, else from `linkml-scala generate rdfs` - so CI can pass an
already-generated file and needs no linkml-scala binary.
"""

import argparse
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

import yaml
from rdflib import BNode, Graph, RDF, RDFS, URIRef
from rdflib.namespace import OWL

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "schema" / "saref-core.yaml"
SAREF = ROOT / "source" / "SAREFCore" / "saref.ttl"

SKOS = "http://www.w3.org/2004/02/skos/core#"
# saref.ttl declares no global domain/range for these; the taxonomies are per-class
# allValuesFrom restrictions instead. Reported separately, never as a mismatch.
EXCEPTIONS = {URIRef(SKOS + "broader"), URIRef(SKOS + "narrower")}
# saref.ttl never declares these as properties; they only appear as annotations.
IGNORED = {RDFS.label, RDFS.comment}

PREFIXES = {
    "https://saref.etsi.org/core/": "saref:",
    "https://saref.etsi.org/saref4syst/": "s4syst:",
    "http://www.w3.org/2006/time#": "time:",
    "http://www.w3.org/2001/XMLSchema#": "xsd:",
    "http://www.w3.org/2000/01/rdf-schema#": "rdfs:",
    SKOS: "skos:",
}


def qname(term):
    if isinstance(term, str) and not isinstance(term, URIRef):
        return term
    for base, pfx in PREFIXES.items():
        if str(term).startswith(base):
            return pfx + str(term)[len(base):]
    return f"<{term}>"


def fmt(terms):
    return ", ".join(sorted(qname(t) for t in terms)) if terms else "(empty)"


# --- step 1: generate RDFS from the LinkML schema ------------------------------------

def generate_rdfs():
    out = Path(tempfile.mkdtemp(prefix="rdfs-check-")) / "generated.rdfs.ttl"
    subprocess.run(
        ["linkml-scala", "generate", "rdfs", "--format", "ttl", "--to", str(out), str(SCHEMA)],
        check=True,
    )
    return out


# --- steps 3-7: extract the reference model from saref.ttl ---------------------------

def resolve(graph, subj, pred):
    """None if the triple is absent; otherwise the set of IRIs, unfolding owl:unionOf."""
    objs = list(graph.objects(subj, pred))
    if not objs:
        return None
    out = set()
    for o in objs:
        if isinstance(o, URIRef):
            out.add(o)
        else:
            heads = list(graph.objects(o, OWL.unionOf))
            if not heads:
                out.add("<unresolved blank node>")
            for head in heads:
                out.update(graph.items(head))
    return out


def read_reference(graph):
    classes = {s for s in graph.subjects(RDF.type, OWL.Class) if isinstance(s, URIRef)}
    subclass_of = set()
    local_axioms = defaultdict(set)  # property -> classes carrying a local restriction
    for c, d in graph.subject_objects(RDFS.subClassOf):
        if isinstance(d, URIRef):
            subclass_of.add((c, d))
        elif isinstance(d, BNode):
            for prop in graph.objects(d, OWL.onProperty):
                local_axioms[prop].add(c)
    props = {
        s
        for t in (OWL.ObjectProperty, OWL.DatatypeProperty)
        for s in graph.subjects(RDF.type, t)
        if isinstance(s, URIRef)
    }
    domains = {p: resolve(graph, p, RDFS.domain) for p in props}
    ranges = {p: resolve(graph, p, RDFS.range) for p in props}
    return classes, subclass_of, props, domains, ranges, local_axioms


# --- steps 8-9: extract the generated model ------------------------------------------

def read_generated(graph):
    classes = set(graph.subjects(RDF.type, RDFS.Class))
    subclass_of = set(graph.subject_objects(RDFS.subClassOf))
    props = set(graph.subjects(RDF.type, RDF.Property))
    domains = {p: resolve(graph, p, RDFS.domain) for p in props}
    ranges = {p: resolve(graph, p, RDFS.range) for p in props}
    return classes, subclass_of, props, domains, ranges


# --- steps 10-11: normalisation over saref.ttl's own hierarchy -----------------------

def make_closure(subclass_of):
    children = defaultdict(set)
    for sub, sup in subclass_of:
        children[sup].add(sub)

    def close(terms):
        if terms is None:
            return None
        seen, queue = set(terms), list(terms)
        while queue:
            for child in children[queue.pop()]:
                if child not in seen:
                    seen.add(child)
                    queue.append(child)
        return seen

    return close


# --- step 17: which slot URIs use any_of --------------------------------------------

def any_of_slot_uris():
    schema = yaml.safe_load(SCHEMA.read_text())
    prefixes = schema.get("prefixes", {})
    default_prefix = schema.get("default_prefix", "")

    def expand(curie):
        pfx, _, local = curie.partition(":")
        return URIRef(prefixes.get(pfx, pfx) + local) if local else URIRef(curie)

    slots = schema.get("slots", {})

    def uri_of(name):
        definition = slots.get(name) or {}
        curie = definition.get("slot_uri")
        return expand(curie) if curie else expand(f"{default_prefix}:{name}")

    skip = set()
    for name, definition in slots.items():
        if "any_of" in (definition or {}):
            skip.add(uri_of(name))
    for cls in (schema.get("classes") or {}).values():
        for name, usage in ((cls or {}).get("slot_usage") or {}).items():
            if "any_of" in (usage or {}):
                skip.add(uri_of(name))
    return skip


# --- steps 14-18: compare one dimension ---------------------------------------------

def compare(dimension, ref_map, gen_map, shared, close, skip, local_axioms):
    buckets = defaultdict(list)
    for prop in sorted(shared, key=str):
        ref, gen = ref_map.get(prop), gen_map.get(prop)
        if prop in EXCEPTIONS:
            buckets["exception"].append((prop, ref, gen))
        elif dimension == "range" and prop in skip:
            buckets["skipped"].append((prop, ref, gen))
        elif ref is None and gen is None:
            buckets["equal"].append((prop, ref, gen))
        elif ref is None:
            buckets["unilateral"].append((prop, ref, gen))
        elif gen is None:
            buckets["missing"].append((prop, ref, gen))
        else:
            cref, cgen = close(ref), close(gen)
            if cref == cgen:
                buckets["equal"].append((prop, ref, gen))
            elif cgen < cref:
                buckets["narrower"].append((prop, cref - cgen, local_axioms.get(prop, set())))
            elif cgen > cref:
                buckets["wider"].append((prop, cgen - cref, local_axioms.get(prop, set())))
            else:
                buckets["disjoint"].append((prop, cgen ^ cref, local_axioms.get(prop, set())))
    return buckets


def report_dimension(dimension, buckets):
    failures = 0
    print(f"\n=== rdfs:{dimension} ===")
    print(f"  equal (after subclass closure): {len(buckets['equal'])}")
    if dimension == "range" and buckets["skipped"]:
        print(f"  skipped, slot uses any_of:     {len(buckets['skipped'])}")
    for kind, label in (
        ("narrower", "generated is NARROWER than saref.ttl, missing"),
        ("wider", "generated is WIDER than saref.ttl, extra"),
        ("disjoint", "generated and saref.ttl differ both ways, symmetric difference"),
    ):
        for prop, diff, axioms in buckets[kind]:
            failures += 1
            note = ""
            overlap = diff & axioms if isinstance(diff, set) else set()
            if overlap:
                note = f"  [saref has a local owl:Restriction on this property for: {fmt(overlap)}]"
            print(f"  MISMATCH {qname(prop)}: {label}: {fmt(diff)}{note}")
    for prop, ref, _ in buckets["missing"]:
        failures += 1
        print(f"  MISMATCH {qname(prop)}: saref.ttl declares {fmt(ref)}, generated declares none")
    for prop, _, gen in buckets["unilateral"]:
        print(f"  narrowing {qname(prop)}: saref.ttl declares none, generated declares {fmt(gen)}")
    for prop, ref, gen in buckets["exception"]:
        print(
            f"  by design {qname(prop)}: saref.ttl "
            f"{'declares none' if ref is None else fmt(ref)}, generated {fmt(gen)}"
        )
    return failures


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rdfs", default=None,
                    help="pre-generated RDFS; skips calling linkml-scala")
    args = ap.parse_args()

    generated = Path(args.rdfs) if args.rdfs else generate_rdfs()
    ref, gen = Graph(), Graph()
    ref.parse(SAREF, format="turtle")
    gen.parse(generated)          # format inferred from the extension, so .ttl or .nt both work

    ref_classes, ref_sub, ref_props, ref_dom, ref_rng, local_axioms = read_reference(ref)
    gen_classes, gen_sub, gen_props, gen_dom, gen_rng = read_generated(gen)
    close = make_closure(ref_sub)
    skip = any_of_slot_uris()

    print(f"reference: {SAREF.name} - {len(ref_classes)} classes, {len(ref_props)} properties")
    print(f"generated: {generated.name} - {len(gen_classes)} classes, {len(gen_props)} properties")
    print("out of scope: cardinality, inverses, property chains, functional properties,")
    print("             allValuesFrom, annotations, ontology metadata")
    print("note: repeated rdfs:domain/rdfs:range values are read as unions (generator intent),")
    print("      not as the conjunction that strict RDFS semantics would give them")

    failures = 0

    print("\n=== inheritance (named rdfs:subClassOf only) ===")
    print(f"  shared pairs: {len(ref_sub & gen_sub)}")
    for sub, sup in sorted(ref_sub - gen_sub, key=str):
        failures += 1
        print(f"  MISSING  {qname(sub)} rdfs:subClassOf {qname(sup)}")
    for sub, sup in sorted(gen_sub - ref_sub, key=str):
        failures += 1
        print(f"  EXTRA    {qname(sub)} rdfs:subClassOf {qname(sup)}")
    print(f"  blank-node subClassOf axioms in saref.ttl (not inheritance): "
          f"{sum(len(v) for v in local_axioms.values())}")

    shared = (ref_props & gen_props) - IGNORED
    failures += report_dimension("domain", compare("domain", ref_dom, gen_dom, shared, close, skip, local_axioms))
    failures += report_dimension("range", compare("range", ref_rng, gen_rng, shared, close, skip, local_axioms))

    print("\n=== coverage ===")
    for c in sorted(ref_classes - gen_classes, key=str):
        print(f"  class only in saref.ttl:  {qname(c)}")
    for c in sorted(gen_classes - ref_classes, key=str):
        print(f"  class only in generated:  {qname(c)}")
    for p in sorted(ref_props - gen_props, key=str):
        print(f"  property only in saref.ttl: {qname(p)}")
    for p in sorted((gen_props - ref_props) - IGNORED, key=str):
        print(f"  property only in generated: {qname(p)}")
    for p in sorted(gen_props & IGNORED, key=str):
        print(f"  property ignored, saref.ttl uses it only as an annotation: {qname(p)}")

    print(f"\n{failures} mismatch(es) in inheritance, domain and range.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
