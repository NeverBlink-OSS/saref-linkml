#!/usr/bin/env python3
"""Translate an OWL/Turtle ontology into a LinkML schema.

Every OWL-to-LinkML decision lives in the mapping table (mapping/mapping_table_v2.yaml);
this script only executes it. Nothing is invented: anything the table does not cover is
reported on stderr rather than guessed at, and no placeholder classes are emitted.

    bin/owl2linkml.py source/SAREFCore/saref.ttl -o schema/saref-core.yaml
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import yaml
from rdflib import BNode, Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS

SCHEMA_ORDER = """id name title description license version created_on last_updated_on
    source contributors comments see_also prefixes default_prefix default_range imports
    types slots classes""".split()
CLASS_ORDER = """class_uri title description comments notes examples see_also is_a mixins
    slots slot_usage""".split()
SLOT_ORDER = """slot_uri title description comments notes examples see_also is_a mixins
    range any_of required multivalued inverse""".split()

# Metaslots whose values are objects rather than plain strings.
STRUCTURED = {"examples": "value"}

# Predicates read structurally rather than through a mapping table row.
PLUMBING = {RDF.type, RDF.first, RDF.rest, OWL.unionOf, OWL.onProperty, RDFS.range}


def camel(name: str) -> str:
    return name[:1].upper() + name[1:]


def snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def ordered(d: dict, order: list[str]) -> dict:
    """Reorder a mapping for readable output, keeping unknown keys at the end."""
    return {k: d[k] for k in order if k in d} | {k: v for k, v in d.items() if k not in order}


class Converter:
    """Turns one RDF graph into one LinkML schema dict, driven by the mapping table."""

    def __init__(self, graph: Graph, mapping: dict, name: str):
        self.g = graph
        self.m = mapping
        self.name = name

        self.issues: list[str] = []
        self.used_prefixes: set[str] = set()
        self.classes: dict[URIRef, str] = {}  # emitted classes: IRI -> LinkML name
        self.slots: dict[URIRef, str] = {}  # emitted slots:   IRI -> LinkML name
        self.slot_kind: dict[URIRef, str] = {}  # slot IRI -> declaring owl: type CURIE
        self.class_slots: dict[URIRef, set[str]] = defaultdict(set)

    # -- small helpers ------------------------------------------------------

    def uri(self, curie: str) -> URIRef:
        prefix, _, local = curie.partition(":")
        return URIRef(self.m["prefixes"][prefix] + local)

    def curie(self, iri: URIRef) -> str:
        prefix, _, local = self.g.compute_qname(iri)
        self.used_prefixes.add(prefix)
        return f"{prefix}:{local}"

    def note(self, message: str) -> None:
        self.issues.append(message)

    def union(self, node) -> list[URIRef]:
        """Members of an owl:unionOf class expression, or [] if it is not one."""
        members = self.g.value(node, OWL.unionOf)
        return list(self.g.items(members)) if members else []

    # -- vocabulary ---------------------------------------------------------

    def read_ontology(self) -> dict:
        """The owl:Ontology node becomes the schema header."""
        ontology = next(self.g.subjects(RDF.type, OWL.Ontology))

        header = {"name": self.name}
        for curie, metaslot in self.m["ontology"]["one"].items():
            if curie == "owl:Ontology":  # the ontology IRI itself
                header[metaslot] = str(ontology)
                continue
            value = self.g.value(ontology, self.uri(curie))
            if value is not None:
                header[metaslot] = str(value)
        for curie, metaslot in self.m["ontology"]["many"].items():
            values = sorted(str(v) for v in self.g.objects(ontology, self.uri(curie)))
            if values:
                header[metaslot] = values
        return header

    def collect_terms(self) -> None:
        """Split the named subjects into classes and slots, per the `terms` table.

        A term counts as local if the source declares it, whatever its namespace: emitting it
        is translation, not invention. Terms merely referenced are dropped and reported.
        """
        for curie, kind in self.m["terms"].items():
            for term in self.g.subjects(RDF.type, self.uri(curie)):
                if isinstance(term, BNode):
                    continue  # anonymous class expressions are not emitted
                if kind == "class":
                    self.classes[term] = camel(re.split(r"[#/]", str(term))[-1])
                elif kind == "slot":
                    self.slots[term] = snake(re.split(r"[#/]", str(term))[-1])
                    self.slot_kind[term] = curie

    # -- ranges -------------------------------------------------------------

    def target(self, iri: URIRef, context: str) -> str | None:
        """LinkML name for a class or datatype used as a range, or None if not emitted."""
        datatype = self.m["datatypes"].get(self.curie(iri))
        if datatype:
            return datatype
        if iri in self.classes:
            return self.classes[iri]
        self.note(f"{context} points at {self.curie(iri)}, which is not emitted")
        return None

    def range_of(self, node, prop: URIRef, context: str) -> dict:
        """A `range:` or `any_of:` fragment, falling back to `defaults.range`."""
        members = self.union(node) if isinstance(node, BNode) else [node]
        names = [n for n in (self.target(m, context) for m in members) if n]
        if len(names) > 1:
            return {self.m["properties"]["owl:unionOf"]: [{"range": n} for n in names]}
        if names:
            return {"range": names[0]}
        return {"range": self.m["defaults"]["range"][self.slot_kind[prop]]}

    # -- documentation ------------------------------------------------------

    def wrap(self, metaslot: str, value) -> object:
        key = STRUCTURED.get(metaslot)
        return {key: str(value)} if key else str(value)

    def documentation(self, subject: URIRef) -> dict:
        docs = {}
        for curie, metaslot in self.m["documentation"]["one"].items():
            value = self.g.value(subject, self.uri(curie))
            if value is not None:
                docs[metaslot] = self.wrap(metaslot, value)
        for curie, metaslot in self.m["documentation"]["many"].items():
            values = sorted(str(v) for v in self.g.objects(subject, self.uri(curie)))
            if values:
                docs[metaslot] = [self.wrap(metaslot, v) for v in values]
        return docs

    def inheritance(self, parents: list[str]) -> dict:
        """All are mixins"""
        return {"mixins": sorted(parents)} if parents else {}

    # -- slots --------------------------------------------------------------

    def domains(self, prop: URIRef) -> list[URIRef]:
        """Classes a property attaches to; a property without a domain inherits its parents'."""
        declared = list(self.g.objects(prop, RDFS.domain))
        if not declared:
            return [c for q in self.g.objects(prop, RDFS.subPropertyOf) for c in self.domains(q)]
        classes = []
        for domain in declared:
            for member in self.union(domain) or [domain]:
                if member in self.classes:
                    classes.append(member)
                elif not isinstance(member, BNode):
                    self.note(f"domain of {self.curie(prop)} drops {self.curie(member)}, not emitted")
        return classes

    def inherited_union(self, prop: URIRef, heir: URIRef) -> dict:
        """The nearest ancestor's range, but only when it is a union.

        """
        union = self.m["properties"]["owl:unionOf"]
        for parent in self.g.objects(prop, RDFS.subPropertyOf):
            declared = self.g.value(parent, RDFS.range)
            expression = (
                self.range_of(declared, heir, f"inherited range of {self.curie(heir)}")
                if declared is not None
                else self.inherited_union(parent, heir)
            )
            if union in expression:
                self.note(f"{self.curie(heir)}: {union} copied down from {self.curie(parent)}, "
                          f"mixins does not propagate it")
                return expression
        return {}

    def build_slot(self, prop: URIRef) -> dict:
        slot = {"slot_uri": self.curie(prop)} | self.documentation(prop)
        parents = [self.slots[q] for q in self.g.objects(prop, RDFS.subPropertyOf) if q in self.slots]
        slot |= self.inheritance(parents)

        declared_range = self.g.value(prop, RDFS.range)
        if declared_range is not None:
            slot |= self.range_of(declared_range, prop, f"range of {self.curie(prop)}")
        elif parents:  # a plain range arrives through is_a; a union does not
            slot |= self.inherited_union(prop, prop)
        else:
            slot["range"] = self.m["defaults"]["range"][self.slot_kind[prop]]

        slot["multivalued"] = self.m["defaults"]["multivalued"]
        if (prop, RDF.type, OWL.FunctionalProperty) in self.g:
            slot |= self.m["properties"]["owl:FunctionalProperty"]
        inverse = self.g.value(prop, OWL.inverseOf)
        if inverse in self.slots:
            slot[self.m["properties"]["owl:inverseOf"]] = self.slots[inverse]
        return ordered(slot, SLOT_ORDER)

    def annotation_slots(self) -> dict:
        """rdfs:label / rdfs:comment on individuals become slots every class carries."""
        slots = {}
        for curie, spec in self.m["instance_annotations"].items():
            spec = dict(spec)
            name, ranges = spec.pop("slot"), spec.pop("range", None)
            slot = {"slot_uri": curie, "description": f"{curie} on an instance, not on its class."}
            slot |= spec
            if isinstance(ranges, list):
                slot[self.m["properties"]["owl:unionOf"]] = [{"range": r} for r in ranges]
            elif ranges:
                slot["range"] = ranges
            slots[name] = ordered(slot, SLOT_ORDER)
            self.used_prefixes.add(curie.partition(":")[0])
        return slots

    # -- restrictions -------------------------------------------------------

    def dimension(self, metaslot: str) -> str | None:
        """What a slot_usage metaslot constrains, or None if nothing can contest it.

        Two restrictions clash only when they write the same dimension. An upper bound and a type
        constraint are both true at once, so both survive.
        """
        if metaslot in ("range", self.m["properties"]["owl:unionOf"]):
            return "type"
        return {"required": "lower", "multivalued": "upper"}.get(metaslot)

    def width(self, dimension: str, body: dict) -> int:
        """How much `body` admits in `dimension`; the wider value wins a clash."""
        if dimension == "type":
            union = body.get(self.m["properties"]["owl:unionOf"])
            return len(union) if union else 1
        if dimension == "lower":
            return 0 if body.get("required") else 1
        return 1 if body.get("multivalued", True) else 0

    def resolve(self, bodies: list[dict], cls: URIRef, slot: str) -> dict:
        """One slot_usage entry from every restriction on this class and property.

        OWL reads several restrictions as a conjunction, which one slot_usage entry cannot always
        hold, so the widest constraint wins. 
        """
        widest: dict[str, int] = {}
        for body in bodies:
            for metaslot in body:
                if d := self.dimension(metaslot):
                    widest[d] = max(widest.get(d, 0), self.width(d, body))

        usage: dict = {}
        for body in bodies:
            narrower = sorted({
                d for metaslot in body
                if (d := self.dimension(metaslot)) and self.width(d, body) < widest[d]
            })
            if narrower:
                self.note(f"{self.curie(cls)}.{slot}: dropped {body}, narrower on "
                          f"{' and '.join(narrower)} than another restriction on the same slot")
                continue
            # equally wide restrictions still disagree; OWL reads them as a conjunction that one
            # slot_usage entry cannot hold, so the later one wins and the choice is reported
            clash = {k: [usage[k], v] for k, v in body.items() if k in usage and usage[k] != v}
            if clash:
                self.note(f"{self.curie(cls)}.{slot}: {clash} disagree between restrictions of "
                          f"equal width, keeping the later value of each")
            usage |= body
        return usage

    def restriction(self, node: BNode) -> tuple[str | None, dict]:
        """An owl:Restriction becomes slot_usage for the slot it is on."""
        prop = self.g.value(node, OWL.onProperty)
        if prop not in self.slots:
            self.note(f"restriction on {prop} skipped, the property is not emitted")
            return None, {}
        usage: dict = {}
        for curie, rules in self.m["restrictions"].items():
            value = self.g.value(node, self.uri(curie))
            if value is None:
                continue
            rules = dict(rules)
            expected = rules.pop("when", None)
            if expected is not None and value.toPython() != expected:
                self.note(f"{self.curie(prop)}: {curie} {value.toPython()} has no mapping table row")
                continue
            for metaslot, target in rules.items():
                source = str(target)
                if not source.startswith("from "):
                    usage[metaslot] = target
                    continue
                referenced = self.g.value(node, self.uri(source[len("from "):]))
                context = f"{source[len('from '):]} on {self.curie(prop)}"
                if metaslot == "range":
                    usage |= self.range_of(referenced, prop, context)
                else:
                    usage[metaslot] = self.target(referenced, context)
        return self.slots[prop], usage

    # -- classes ------------------------------------------------------------

    def build_class(self, iri: URIRef) -> dict:
        cls = {"class_uri": self.curie(iri)} | self.documentation(iri)
        parents: list[str] = []
        contributions: dict[str, list[dict]] = defaultdict(list)
        for parent in self.g.objects(iri, RDFS.subClassOf):
            if isinstance(parent, BNode):
                slot, restriction = self.restriction(parent)
                if slot:
                    self.class_slots[iri].add(slot)
                    if restriction:
                        contributions[slot].append(restriction)
            elif parent in self.classes:
                parents.append(self.classes[parent])
            else:
                self.note(f"{self.curie(iri)} drops parent {self.curie(parent)}, not emitted")
        cls |= self.inheritance(parents)
        if self.class_slots[iri]:
            cls["slots"] = sorted(self.class_slots[iri])
        # every restriction on a slot is resolved together, so the widest can be found
        usage = {s: b for s in sorted(contributions)
                 if (b := self.resolve(contributions[s], iri, s))}
        if usage:
            cls["slot_usage"] = usage
        return ordered(cls, CLASS_ORDER)

    # -- reporting ----------------------------------------------------------

    def report_predicates(self) -> None:
        """Anything the mapping table never mentions is surfaced, not silently dropped."""
        prefixes = "|".join(self.m["prefixes"])
        known = set(re.findall(rf"\b(?:{prefixes}):[A-Za-z]+", yaml.safe_dump(self.m)))
        for predicate in sorted(set(self.g.predicates())):
            if predicate in PLUMBING:
                continue
            curie = self.curie(predicate)
            if curie in self.m["report_ignored"]:
                self.note(f"{curie}: {self.m['report_ignored'][curie]}")
            elif curie not in known:
                self.note(f"{curie} is used in the source but absent from the mapping table")

    # -- assembly -----------------------------------------------------------

    def run(self) -> dict:
        schema = self.read_ontology()
        self.collect_terms()

        slots = {self.slots[p]: self.build_slot(p) for p in self.slots}
        for prop in self.slots:
            for owner in self.domains(prop):
                self.class_slots[owner].add(self.slots[prop])

        annotations = self.annotation_slots()
        for owner in self.classes:
            self.class_slots[owner].update(annotations)
        classes = {self.classes[c]: self.build_class(c) for c in self.classes}
        carried = {name for names in self.class_slots.values() for name in names}
        for prop, name in self.slots.items():
            if name not in carried:
                self.note(f"{self.curie(prop)} is emitted unattached to any class, "
                          f"neither a domain nor a restriction places it")
        self.report_predicates()

        schema |= {k: v for k, v in self.m["schema"].items() if k != "prefixes"}
        prefixes = {p: str(n) for p, n in self.g.namespaces() if p in self.used_prefixes}
        schema["prefixes"] = dict(sorted((prefixes | self.m["schema"]["prefixes"]).items()))
        schema["slots"] = {k: (slots | annotations)[k] for k in sorted(slots | annotations)}
        schema["classes"] = {k: classes[k] for k in sorted(classes)}
        return ordered(schema, SCHEMA_ORDER)


def main() -> int:
    here = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="ontology in Turtle")
    parser.add_argument("-o", "--out", type=Path, required=True, help="LinkML schema to write")
    parser.add_argument("-m", "--mapping", type=Path, default=here / "mapping/mapping_table.yaml")
    args = parser.parse_args()

    mapping = yaml.safe_load(args.mapping.read_text())
    graph = Graph().parse(args.source, format="turtle")
    converter = Converter(graph, mapping, args.out.stem)
    schema = converter.run()

    args.out.write_text(yaml.safe_dump(schema, sort_keys=False, allow_unicode=True, width=100))
    print(f"{args.out}: {len(schema['classes'])} classes, {len(schema['slots'])} slots")

    for issue in sorted(set(converter.issues)):
        print(f"  note: {issue}", file=sys.stderr)

    if shutil.which("linkml-scala"):
        return subprocess.run(["linkml-scala", "validate", str(args.out)]).returncode
    print("  note: linkml-scala not on PATH, schema not validated", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
