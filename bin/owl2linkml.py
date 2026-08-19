#!/usr/bin/env python3
"""Translate an OWL/Turtle ontology into a LinkML schema.

Every OWL-to-LinkML decision lives in the mapping table (mapping/mapping_table.yaml);
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
    types enums slots classes""".split()
ENUM_ORDER = """enum_uri title description comments notes examples see_also mixins
    permissible_values""".split()
CLASS_ORDER = """class_uri title description comments notes examples see_also is_a mixins
    slots slot_usage""".split()
SLOT_ORDER = """slot_uri title description comments notes examples see_also is_a mixins
    range any_of required multivalued inverse transitive symmetric asymmetric reflexive
    irreflexive""".split()

# `has_value` keys that parameterise the row rather than name a destination metaslot.
HAS_VALUE_PARAMS = {"enum_name", "strip_prefix"}

# What a `terms` row may say a declared term becomes.
TERM_KINDS = {"class", "slot", "ignored"}


def camel(name: str) -> str:
    return name[:1].upper() + name[1:]


def snake(name: str) -> str:
    """CamelCase to snake_case, keeping runs of capitals together.

    A run is one word: hasMACAddress is has_mac_address, not has_m_a_c_address, and
    DCPowerSource is dc_power_source.
    """
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    name = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", name)
    return name.lower()


def upper_snake(name: str) -> str:
    return snake(name).upper()


# Styles a `naming.style` row may ask for, applied to an IRI's local name.
STYLES = {
    "UpperCamelCase": camel,
    "snake_case": snake,
    "UPPER_SNAKE_CASE": upper_snake,
}


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
        self.imports: list[str] = []  # LinkML imports resolved from owl:imports
        self.classes: dict[URIRef, str] = {}  # emitted classes: IRI -> LinkML name
        self.slots: dict[URIRef, str] = {}  # emitted slots:   IRI -> LinkML name
        self.slot_kind: dict[URIRef, str] = {}  # slot IRI -> declaring owl: type CURIE
        self.class_slots: dict[URIRef, set[str]] = defaultdict(set)
        self.imported: dict[URIRef, str] = {}  # elements an import already provides: IRI -> name
        self.imported_kind: dict[URIRef, str] = {}  # the same IRI -> "classes" | "slots"
        self.default_prefix = ""  # vann:preferredNamespacePrefix, read from the ontology node
        self.enums: dict[URIRef, str] = {}  # a class rendered as an enum: type IRI -> enum name
        self.members: dict[URIRef, set[URIRef]] = defaultdict(set)  # type IRI -> individuals
        self.enumerated: dict[URIRef, set[URIRef]] = {}  # the same, from enumeration axioms
        self.read_one_of: set = set()  # nodes an enumeration was actually read off
        self.slot_owners: set[URIRef] = set()  # classes some property declares a domain of
        self.ranged: set[URIRef] | None = None  # classes in range position, computed once
        self.value_enums: dict[str, dict] = {}  # every emitted enum, by LinkML name

    # -- small helpers ------------------------------------------------------

    def uri(self, curie: str) -> URIRef:
        prefix, _, local = curie.partition(":")
        return URIRef(self.m["prefixes"][prefix] + local)

    def curie(self, iri: URIRef) -> str:
        try:
            prefix, _, local = self.g.compute_qname(iri)
        except ValueError:
            return str(iri)  # no namespace/name split, e.g. https://www.upm.es/; a URI is valid
        self.used_prefixes.add(prefix)
        return f"{prefix}:{local}"

    def note(self, message: str) -> None:
        self.issues.append(message)

    @staticmethod
    def local(iri) -> str:
        """An IRI's local name: whatever follows the last # or /."""
        return re.split(r"[#/]", str(iri))[-1]

    def styled(self, kind: str, iri) -> str:
        """`iri`'s local name, recased to the style `naming.style` names for `kind`."""
        style = self.m["naming"]["style"][kind]
        if style not in STYLES:
            raise ValueError(f"naming.style.{kind} is {style!r}; "
                             f"the styles on offer are {sorted(STYLES)}")
        return STYLES[style](self.local(iri))

    def union(self, node) -> list[URIRef]:
        """Members of an owl:unionOf class expression, or [] if it is not one."""
        members = self.g.value(node, OWL.unionOf)
        return list(self.g.items(members)) if members else []

    @staticmethod
    def axiom_names(source: str) -> list[str]:
        """The axioms a `from <axiom>` or `from [<a>, <b>]` table value names.

        The list form exists because a qualified cardinality carries its filler under
        owl:onClass or owl:onDataRange depending on whether it is a class or a datatype.
        """
        body = source[len("from "):].strip()
        return ([a.strip() for a in body[1:-1].split(",")]
                if body.startswith("[") else [body])

    def filler(self, node: BNode, source: str) -> tuple[object, str]:
        """Resolve a `from <axiom>` table value against a restriction."""
        axioms = self.axiom_names(source)
        for axiom in axioms:
            value = self.g.value(node, self.uri(axiom))
            if value is not None:
                return value, axiom
        return None, " or ".join(axioms)

    # -- imports ------------------------------------------------------------

    @staticmethod
    def trim(iri) -> str:
        """Compare import IRIs under `imports.compare: ignore_trailing_slash`."""
        return str(iri).rstrip("/")

    def resolve_imports(self, root: Path) -> None:
        """Match each owl:imports IRI against the schemas already in the schema directory.

        No IRI is hardcoded: a candidate matches when the import equals its `version` or its
        `id`, which is how saref4bldg and saref4ener reach saref-core v4.1.1.
        """
        spec = self.m["imports"]
        index: dict[str, tuple[str, str, dict]] = {}
        for path in sorted((root / spec["resolve_from"]).glob("*.yaml")):
            doc = yaml.safe_load(path.read_text()) or {}
            if doc.get("name") in (None, self.name):  # never resolve a schema against itself
                continue
            for key in spec["match"]:
                if doc.get(key):
                    index.setdefault(self.trim(doc[key]), (doc["name"], key, doc))
        # look at what the source ontology imports and see if we found it in source
        for target in sorted(self.g.objects(None, OWL.imports)):
            found = index.get(self.trim(target))
            if not found:
                self.note(f"owl:imports {target} matches no schema in {spec['resolve_from']}")
                continue
            name, key, doc = found
            self.imports.append(spec["emit"].replace("<name>", name))
            self.adopt(doc)
            self.note(f"owl:imports {target} resolved to {name} by its {key}")

    def adopt(self, doc: dict) -> None:
        """Register an imported schema's elements by URI, so references to them resolve."""
        prefixes = doc.get("prefixes", {})

        def expand(curie: str) -> URIRef:
            prefix, _, local = str(curie).partition(":")
            return URIRef(prefixes.get(prefix, f"{prefix}:") + local)

        for kind, key in (("classes", "class_uri"), ("slots", "slot_uri")):
            for name, body in (doc.get(kind) or {}).items():
                if body and body.get(key):
                    self.imported[expand(body[key])] = name
                    self.imported_kind[expand(body[key])] = kind

    def known_class(self, iri: URIRef) -> str | None:
        """LinkML name usable as a range: a class, an enum, or an import.

        An enum name is a valid range, so a class replaced by an enum keeps resolving.
        """
        if iri in self.classes:
            return self.classes[iri]
        if iri in self.enums:
            return self.enums[iri]
        if self.imported_kind.get(iri) == "classes":
            return self.imported[iri]
        return None

    def claim(self, name: str, iri: URIRef | None, what: str) -> str:
        """Reserve a name for an enum, renaming it if something else already holds it.

        """
        taken = (set(self.imported.values()) | set(self.classes.values())
                 | set(self.slots.values()) | set(self.value_enums)
                 | set(self.m["schema"]["root_only"]["types"]))
        if name not in taken:
            return name
        if name in self.value_enums:
            raise ValueError(f"{what} would be named {name}, which another enum in this schema "
                             f"already uses; two enums cannot share one name")
        chosen = self.qualify(self.m["naming"]["on_conflict_with_import"]["class"], name,
                              self.term_prefix(iri) if iri is not None else "")
        self.note(f"{what} named {chosen}: {name} is already taken in this schema or an import")
        return chosen

    def known_slot(self, iri: URIRef) -> str | None:
        """LinkML name for a slot, whether this schema emits it or an import provides it."""
        if iri in self.slots:
            return self.slots[iri]
        if self.imported_kind.get(iri) == "slots":
            return self.imported[iri]
        return None

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
        self.default_prefix = header.get("default_prefix", "")
        for curie, metaslot in self.m["ontology"]["many"].items():
            values = {str(v) for v in self.g.objects(ontology, self.uri(curie))}
            if values:  # several predicates may feed one metaslot: creator and contributor both
                header[metaslot] = sorted(set(header.get(metaslot, [])) | values)
        return header

    def collect_terms(self) -> None:
        """Split the named subjects into classes and slots, per the `terms` table.

        A term counts as local if the source declares it, whatever its namespace: emitting it
        is translation, not invention. E.g., Core declares skos:broader.
        Terms merely referenced are dropped and reported.
        A term an import already declares under the same URI is reused rather than redeclared
        """
        unknown = {c: k for c, k in self.m["terms"].items() if k not in TERM_KINDS}
        if unknown:
            raise ValueError(f"`terms` values must be one of {sorted(TERM_KINDS)}; "
                             f"{unknown} name none of them")
        for curie, kind in self.m["terms"].items():
            if kind == "ignored":
                continue  # no element comes of these terms
            for term in self.g.subjects(RDF.type, self.uri(curie)):
                if isinstance(term, BNode):
                    continue  # anonymous classes are not emitted
                if self.curie(term) in self.m["report_ignored"]:
                    continue  # declared by the source, but deliberately ignored
                if term in self.imported:
                    self.note(f"{self.curie(term)} is declared but already imported "
                              f"as {self.imported[term]}, reusing it")
                    continue
                if kind == "class":
                    self.classes[term] = self.styled(kind, term)
                else:
                    self.slots[term] = self.styled(kind, term)
                    self.slot_kind[term] = curie

    def qualify(self, template: str, name: str, prefix: str) -> str:
        """Fill a naming template with a namespace prefix."""
        prefix = prefix or self.default_prefix
        return (template.replace("<Prefix>", camel(prefix)).replace("<prefix>", prefix)
                .replace("<Name>", name).replace("<name>", name))

    def term_prefix(self, iri: URIRef) -> str:
        """The prefix the source binds for a term's own namespace.

        Used for collisions rather than the schema's own prefix: saref4grid declares terms in
        the third-party oneM2M namespace, and OneM2MDevice is right where S4gridDevice is not.
        """
        return self.curie(iri).partition(":")[0]

    def apply_naming(self) -> None:
        """Rename any local element whose name an import already uses.

        Only the identity matters for interoperability, and that lives in class_uri/slot_uri,
        which are untouched by this.
        So we create a new name for the local element based on whether it is a slo or a class (styling)
        and prepend an ontology prefix to it
        """
        spec = self.m["naming"]["on_conflict_with_import"]
        override = self.m["naming"].get("rename") or {}
        taken = set(self.imported.values())
        if not override and not taken:
            return
        for registry, kind in ((self.classes, "class"), (self.slots, "slot")):
            for iri, name in list(registry.items()):
                chosen = override.get(self.curie(iri))
                if chosen is None:
                    if name not in taken:
                        continue
                    chosen = self.qualify(spec[kind], name, self.term_prefix(iri))
                registry[iri] = chosen
                if spec.get("report"):
                    self.note(f"{self.curie(iri)} named {chosen}: an import already uses {name}")

    # -- individuals and enums ----------------------------------------------

    def collect_individuals(self) -> None:
        """Named subjects typed by a class become the members of an enum standing for it.

        owl:NamedIndividual cannot be the signal, individual is recognised
        structurally: a named subject whose rdf:type is a class, and which is not itself a class
        or a slot.
        """
        for subject, _, kind in self.g.triples((None, RDF.type, None)):
            if isinstance(subject, BNode) or isinstance(kind, BNode):
                continue
            if subject in self.classes or subject in self.slots or subject in self.imported:
                continue
            if self.known_class(kind) is not None:
                self.members[kind].add(subject)

    def value_name(self, individual: URIRef) -> str:
        return self.styled("permissible_value", individual)

    def enumeration(self, node, kind: URIRef, seen: set) -> set[URIRef] | None:
        """The individuals a class expression enumerates, or None when it enumerates nothing.

        Recursive, because an enumeration is not always written on the class itself. `kind` is the named class the walk started
        from, so a note can say where an anonymous axiom was found.
        """
        if node in seen:
            return None
        seen.add(node)
        found: set[URIRef] = set()
        enumerates = False
        for listed in self.g.objects(node, OWL.oneOf):
            enumerates = True
            self.read_one_of.add(node)
            for item in self.g.items(listed):
                if isinstance(item, BNode):
                    self.note(f"{self.curie(kind)}: owl:oneOf lists an anonymous individual, "
                              f"dropped; only a named one can carry a meaning")
                else:
                    found.add(item)
        for expression in self.g.objects(node, OWL.equivalentClass):
            nested = self.enumeration(expression, kind, seen)  # the class *is* that expression
            if nested is not None:
                enumerates = True
                found |= nested
        for member in self.union(node):
            nested = self.enumeration(member, kind, seen)
            typed = set(self.members.get(member, ()))  # a named class in the union brings its own
            if nested or typed:
                enumerates = True
                found |= (nested or set()) | typed
            elif not isinstance(member, BNode):
                self.note(f"{self.curie(kind)}: the union enumerating it includes "
                          f"{self.curie(member)}, which enumerates no individuals")
        return found if enumerates else None

    def collect_enumerations(self) -> None:
        """Every class an enumeration axiom names, with the individuals it names.

        """
        for kind in self.classes:
            found = self.enumeration(kind, kind, set())
            if found is not None:
                self.enumerated[kind] = found

    def enum_members(self) -> dict[URIRef, set[URIRef]]:
        """Every class an enum could stand for, mapped to the individuals it would enumerate.

        """
        members: dict[URIRef, set[URIRef]] = {}
        for kind in set(self.members) | set(self.enumerated):
            typed = set(self.members.get(kind, ()))
            listed = self.enumerated.get(kind)
            if listed is None:
                members[kind] = typed
                continue
            members[kind] = typed | listed
        return members

    def build_enum(self, head: dict, members: set[URIRef]) -> dict:
        """One enum: `head` carries whatever identifies it, `meaning` keeps each individual's IRI."""
        spec = self.m["individuals"]
        enum = dict(head)
        values = {}
        for member in sorted(members, key=str):
            body = {"meaning": self.curie(member)}
            for curie, metaslot in spec["one"].items():
                value = self.g.value(member, self.uri(curie))
                if value is not None:
                    body[metaslot] = str(value)
            values[self.value_name(member)] = body
        enum["permissible_values"] = values
        return ordered(enum, ENUM_ORDER)

    def filler_axioms(self) -> set[URIRef]:
        """The axioms a restriction carries its range under, read off the `restrictions` table.

        Taken from the rows themselves, so a row added there is picked up here rather than
        restated: every `range: from <axiom>` value names one.
        """
        axioms: set[URIRef] = set()
        for rules in self.m["restrictions"].values():
            source = str(rules.get("range", ""))
            if source.startswith("from "):
                axioms |= {self.uri(name) for name in self.axiom_names(source)}
        return axioms

    def ranged_over(self) -> set[URIRef]:
        """Classes an emitted slot can hold the values of.

        Find all the places where property has a range or where we create a range through restriction
        """
        if self.ranged is None:
            nodes = [declared for prop in self.slots
                     if (declared := self.g.value(prop, RDFS.range)) is not None]
            nodes += [filler for kind in self.classes
                      for parent in self.g.objects(kind, RDFS.subClassOf)
                      if isinstance(parent, BNode) # restriction
                      for axiom in self.filler_axioms() # allValuesFrom, someValuesFrom, onClass, onDataRange
                      for filler in self.g.objects(parent, axiom)] # the range restriction creates
            self.ranged = {member for node in nodes
                           for member in (self.union(node) or [node])
                           if isinstance(member, URIRef)} # guard
        return self.ranged

    def owns_slots(self, kind: URIRef) -> bool:
        """Whether a class carries slots of its own, which an enum has nowhere to put."""
        return kind in self.slot_owners

    def stays_a_class(self, kind: URIRef, members: set[URIRef]) -> str | None:
        """Why `kind` cannot become the enum: an enum holds no slots, has no subclasses and cannot be inherited from.
        
        Check if we can get rid of the class.
        """
        if self.imported_kind.get(kind) == "classes":
            # an extension adding individuals does not get to remodel an import
            return (f"{self.curie(kind)} enumerates {len(members)} individuals declared here, but an "
                    f"import provides it as the class {self.imported[kind]}, so it stays one; the "
                    f"schema that declares a class decides that, not one that extends it")
        if any(not isinstance(c, BNode) for c in self.g.subjects(RDFS.subClassOf, kind)):
            # we cannot make an enum the parent of a class, so it stays a class too
            return (f"{self.curie(kind)} enumerates {len(members)} individuals but has subclasses, "
                    f"so it stays a class; LinkML cannot inherit from an enum")
        if self.owns_slots(kind):
            return (f"{self.curie(kind)} enumerates {len(members)} individuals but carries slots "
                    f"of its own, so it stays a class; an enum holds permissible values only")
        if self.m["individuals"]["needs_a_range"] and kind not in self.ranged_over():
            return (f"{self.curie(kind)} enumerates {len(members)} individuals but nothing ranges "
                    f"over it, so it stays a class and they stay its instances; closing a class "
                    f"OWL leaves open buys nothing when no slot is waiting for the values")
        return None

    def build_individual_enums(self) -> None:
        """Enums get meaning and permissible_values from the individuals they enumerate.

        Note: enum_uri uses default_prefix
        """
        self.ranged_over()
        for kind, members in sorted(self.enum_members().items(), key=lambda kv: str(kv[0])):
            if not members:
                self.note(f"{self.curie(kind)} enumerates no individuals, no enum is emitted")
                continue
            if reason := self.stays_a_class(kind, members):
                self.note(reason)
                continue
            name = self.classes.pop(kind)
            # the enum takes the class's place, so it takes its identity too
            head = {"enum_uri": self.curie(kind)} | self.documentation(kind)
            if kind not in self.enumerated:  # nothing in the source says these are the only members
                head.setdefault("comments", []).append(
                    "OWL leaves this class open.")
            parents = sorted({n for parent in self.g.objects(kind, RDFS.subClassOf)
                              if not isinstance(parent, BNode)
                              and (n := self.known_class(parent))})
            if parents:
                self.note(f"{self.curie(kind)} replaced by an enum; mixins {parents} are "
                          f"recorded")
            name = self.claim(name, kind, f"the enum for {self.curie(kind)}")
            self.enums[kind] = name
            enum = self.build_enum(head, members)
            self.value_enums[name] = ordered(
                enum | self.inheritance(parents, "classes", "rdfs:subClassOf"), ENUM_ORDER)

    def has_value_enum(self, owner: str, prop: URIRef, members: set[URIRef]) -> str:
        """The permitted set an owl:hasValue conjunction becomes, as a per-class enum."""
        spec = self.m["has_value"]
        stem = self.local(prop)
        strip = spec.get("strip_prefix") or ""
        if strip and re.match(rf"{re.escape(strip)}[A-Z]", stem):
            stem = stem[len(strip):]
        name = spec["enum_name"].replace("<Class>", owner).replace("<Property>", camel(stem))
        name = self.claim(name, None, f"the owl:hasValue enum for {owner}")
        kinds = sorted({self.curie(k) for member in members
                        for k in self.g.objects(member, RDF.type)
                        if self.known_class(k) is not None})
        if not kinds:
            self.note(f"{name}: the pinned individuals have no class among their rdf:types, "
                      f"so the enum names none")
        elif len(kinds) > 1:
            self.note(f"{name}: the pinned individuals span {kinds}, all recorded in see_also")
        head = {"description": f"The individuals owl:hasValue pins on {owner}, and the range of "
                               f"the slot they are pinned on."}
        if kinds:
            head["see_also"] = kinds
        self.value_enums[name] = self.build_enum(head, members)
        return name

    # -- ranges -------------------------------------------------------------

    def target(self, iri: URIRef, context: str) -> str | None:
        """LinkML name for a class or datatype used as a range, or None if not emitted."""
        datatype = self.m["datatypes"].get(self.curie(iri))
        if datatype:
            return datatype
        name = self.known_class(iri)
        if name:
            return name
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
        kind = self.slot_kind.get(prop)
        if kind is None:
            self.note(f"{context}: nothing emittable to range over, keeping the range "
                      f"{self.known_slot(prop)} already has")
            return {}
        return {"range": self.m["defaults"]["range"][kind]}

    # -- documentation ------------------------------------------------------

    @staticmethod
    def wrap(target) -> tuple[str, str | None]:
        """A `documentation` row: the metaslot it writes, and the key its values wrap in.

        LinkML's `examples` takes objects rather than plain strings, so the row says which key
        each value goes under. A bare row wraps nothing.
        """
        if isinstance(target, dict):
            return target["slot"], target.get("wrap")
        return target, None

    def documentation(self, subject: URIRef) -> dict:
        docs = {}
        for curie, target in self.m["documentation"]["one"].items():
            metaslot, key = self.wrap(target)
            value = self.g.value(subject, self.uri(curie))
            if value is not None:
                docs[metaslot] = {key: str(value)} if key else str(value)
        for curie, target in self.m["documentation"]["many"].items():
            metaslot, key = self.wrap(target)
            values = sorted(str(v) for v in self.g.objects(subject, self.uri(curie)))
            if values:
                docs[metaslot] = [{key: v} if key else v for v in values]
        return docs

    def inheritance(self, parents: list[str], section: str, curie: str) -> dict:
        """Emit named parents.

        """
        return {self.m[section][curie]: sorted(parents)} if parents else {}

    # -- slots --------------------------------------------------------------

    def domains(self, prop: URIRef) -> list[URIRef]:
        """Classes a property attaches to; a property without a domain inherits its parents'."""
        declared = list(self.g.objects(prop, RDFS.domain))
        if not declared: # check if there is a parent property that has a domain
            return [c for q in self.g.objects(prop, RDFS.subPropertyOf) for c in self.domains(q)]
        classes = []
        for domain in declared:
            for member in self.union(domain) or [domain]: # union is here bc of Core that uses it extensively
                if member in self.classes:
                    classes.append(member)
                elif self.imported_kind.get(member) == "classes":
                    # LinkML cannot reopen an imported class to hang another slot on it
                    self.note(f"domain of {self.curie(prop)} is {self.curie(member)}, which an "
                              f"import owns; the slot is left unattached there")
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
        parents = [n for q in self.g.objects(prop, RDFS.subPropertyOf)
                   if (n := self.known_slot(q))]
        slot |= self.inheritance(parents, "properties", "rdfs:subPropertyOf")

        declared_range = self.g.value(prop, RDFS.range)
        if declared_range is not None:
            slot |= self.range_of(declared_range, prop, f"range of {self.curie(prop)}")
        elif parents:  # a plain range arrives through is_a; a union does not
            slot |= self.inherited_union(prop, prop)
        else:
            slot["range"] = self.m["defaults"]["range"][self.slot_kind[prop]]

        slot["multivalued"] = self.m["defaults"]["multivalued"]
        for curie, body in self.m["properties"].items():
            if isinstance(body, dict) and (prop, RDF.type, self.uri(curie)) in self.g:
                slot |= body
        inverse = self.g.value(prop, OWL.inverseOf)
        if inverse in self.slots:
            slot[self.m["properties"]["owl:inverseOf"]] = self.slots[inverse]
        return ordered(slot, SLOT_ORDER)

    # -- restrictions -------------------------------------------------------

    def usage_metaslot(self) -> str:
        """The metaslot per-class constraints are emitted under, as `has_value` names it.

        """
        names = set(self.m["has_value"]) - HAS_VALUE_PARAMS
        if len(names) != 1:
            raise ValueError(f"`has_value` must name exactly one destination metaslot besides "
                             f"{sorted(HAS_VALUE_PARAMS)}, found {sorted(names)}")
        return names.pop()

    def dimension(self, metaslot: str) -> str | None:
        """What a slot_usage metaslot constrains, or None if nothing can contest it.

        Two restrictions clash only when they write the same dimension. 
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
            clash = {k: [usage[k], v] for k, v in body.items() if k in usage and usage[k] != v}
            if clash:
                self.note(f"{self.curie(cls)}.{slot}: {clash} disagree between restrictions of "
                          f"equal width, keeping the later value of each")
            usage |= body
        return usage

    def axioms(self, node: BNode) -> str:
        """The axioms an anonymous node carries, for reporting it when it has no name."""
        return " and ".join(sorted(self.curie(p) for p in set(self.g.predicates(node))
                                   if p != RDF.type))

    def is_restriction(self, node: BNode) -> bool:
        """Whether an anonymous rdfs:subClassOf object really is an owl:Restriction.

        """
        typed = (node, RDF.type, OWL.Restriction) in self.g
        prop = self.g.value(node, OWL.onProperty)
        if prop is None:
            kind = "owl:Restriction" if typed else "an anonymous parent"
            self.note(f"{kind} carrying {self.axioms(node) or 'no axiom'} is skipped, "
                      f"it names no owl:onProperty to constrain")
            return False
        if not typed:
            self.note(f"the anonymous parent restricting {self.curie(prop)} does not declare "
                      f"rdf:type owl:Restriction; owl:onProperty is taken as enough")
        return True

    def restriction(self, node: BNode) -> tuple[str | None, dict]:
        """An owl:Restriction becomes slot_usage for the slot it is on."""
        prop = self.g.value(node, OWL.onProperty)
        name = self.known_slot(prop)
        if name is None:
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
                if value.toPython() in (self.m["vacuous_restrictions"].get(curie) or []):
                    continue  # a cardinality that asserts nothing, not a gap in the table
                self.note(f"{self.curie(prop)}: {curie} {value.toPython()} has no mapping table row")
                continue
            for metaslot, target in rules.items():
                source = str(target)
                if not source.startswith("from "):
                    usage[metaslot] = target
                    continue
                referenced, axiom = self.filler(node, source)
                if referenced is None:
                    self.note(f"{self.curie(prop)}: {curie} carries none of {axiom}, "
                              f"{metaslot} not set")
                    continue
                context = f"{axiom} on {self.curie(prop)}"
                if metaslot == "range":
                    usage |= self.range_of(referenced, prop, context)
                else:
                    usage[metaslot] = self.target(referenced, context)
        return name, usage

    # -- classes ------------------------------------------------------------

    def build_class(self, iri: URIRef) -> dict:
        cls = {"class_uri": self.curie(iri)} | self.documentation(iri)
        equivalent = sorted(self.curie(o) for o in self.g.objects(iri, OWL.equivalentClass)
                            if not isinstance(o, BNode))
        if equivalent:
            cls[self.m["classes"]["owl:equivalentClass"]] = equivalent
        parents: list[str] = []
        contributions: dict[str, list[dict]] = defaultdict(list)
        pinned: dict[URIRef, set[URIRef]] = defaultdict(set)
        for parent in self.g.objects(iri, RDFS.subClassOf):
            if isinstance(parent, BNode):
                if not self.is_restriction(parent):
                    continue  # some other class expression; not read as a constraint
                slot, restriction = self.restriction(parent)
                if slot:
                    self.class_slots[iri].add(slot)
                    if restriction:
                        contributions[slot].append(restriction)
                    value = self.g.value(parent, OWL.hasValue)
                    if value is not None:
                        pinned[self.g.value(parent, OWL.onProperty)].add(value)
            elif self.curie(parent) in self.m["report_ignored"]:
                pass  # owl:Thing and the like: referenced, never an element
            elif name := self.known_class(parent):
                parents.append(name)
            else:
                self.note(f"{self.curie(iri)} drops parent {self.curie(parent)}, not emitted")
        cls |= self.inheritance(parents, "classes", "rdfs:subClassOf")
        for prop, members in sorted(pinned.items(), key=lambda kv: str(kv[0])):
            enum = self.has_value_enum(self.classes[iri], prop, members)
            usage = {k: (enum if v == "<Enum>" else v)
                     for k, v in self.m["has_value"][self.usage_metaslot()].items()}
            contributions[self.known_slot(prop)].append(usage)
            self.note(f"{self.curie(iri)}.{self.known_slot(prop)}: {len(members)} owl:hasValue "
                      f"axioms became {enum}, read as the closed set of permitted values; "
                      f"the axiom itself permits others, see `has_value` in the mapping table")
        if self.class_slots[iri]:
            cls[self.m["classes"]["rdfs:domain"]] = sorted(self.class_slots[iri])
        # every restriction on a slot is resolved together, so the widest can be found
        usage = {s: b for s in sorted(contributions)
                 if (b := self.resolve(contributions[s], iri, s))}
        if usage:
            cls[self.usage_metaslot()] = usage
        return ordered(cls, CLASS_ORDER)

    # -- reporting ----------------------------------------------------------

    def covered_terms(self) -> set[str]:
        """Every term the mapping table accounts for.

        """
        prefixes = "|".join(self.m["prefixes"])
        pattern = re.compile(rf"(?:{prefixes}):[A-Za-z_][\w.-]*")
        covered: set[str] = set()

        def walk(node) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if isinstance(key, str) and pattern.fullmatch(key):
                        covered.add(key)
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)
            elif isinstance(node, str) and node.startswith("from "):
                covered.update(pattern.findall(node))

        walk(self.m)
        return covered

    def report_predicates(self) -> None:
        """Anything the mapping table never mentions.
        
        """
        known = self.covered_terms()
        structural = {self.uri(curie) for curie in self.m["structural_terms"]}
        vocabulary = set(self.g.predicates())
        for predicate in (RDF.type, RDFS.subClassOf):
            vocabulary |= {o for o in self.g.objects(None, predicate)
                           if isinstance(o, URIRef) and str(o).startswith(str(OWL))}
        for term in sorted(vocabulary - structural):
            curie = self.curie(term)
            if curie in self.m["report_ignored"]:
                self.note(f"{curie}: {self.m['report_ignored'][curie]}")
            elif curie not in known:
                self.note(f"{curie} is used in the source but absent from the mapping table")

    # -- assembly -----------------------------------------------------------

    def run(self, root: Path) -> dict:
        schema = self.read_ontology()
        self.resolve_imports(root)
        self.collect_terms()
        self.apply_naming()
        self.collect_individuals()
        self.collect_enumerations()
        owners = {prop: self.domains(prop) for prop in self.slots} # check who owns slots
        self.slot_owners = {owner for found in owners.values() for owner in found}
        self.build_individual_enums()  # before ranges resolve: it moves classes to enums

        slots = {self.slots[p]: self.build_slot(p) for p in self.slots}
        for prop, found in owners.items():
            for owner in found:
                self.class_slots[owner].add(self.slots[prop])
        classes = {self.classes[c]: self.build_class(c) for c in self.classes}
        carried = {name for names in self.class_slots.values() for name in names}
        for prop, name in self.slots.items():
            if name not in carried:
                self.note(f"{self.curie(prop)} is emitted unattached to any class, "
                          f"neither a domain nor a restriction places it")
        self.report_predicates() # check if there is something generator missed

        # `types` declares named elements, so only a schema that imports nothing may emit them;
        # anything importing inherits them and would otherwise clash on the name.
        header = dict(self.m["schema"]["always"])
        if not self.imports:
            header |= self.m["schema"]["root_only"]
        header["imports"] = list(header.get("imports", [])) + self.imports
        schema |= {k: v for k, v in header.items() if k != "prefixes"}
        prefixes = {p: str(n) for p, n in self.g.namespaces() if p in self.used_prefixes}
        schema["prefixes"] = dict(sorted((prefixes | header["prefixes"]).items()))
        if self.value_enums:
            schema["enums"] = {k: self.value_enums[k] for k in sorted(self.value_enums)}
        schema["slots"] = {k: (slots)[k] for k in sorted(slots)}
        schema["classes"] = {k: classes[k] for k in sorted(classes)}
        return ordered(schema, SCHEMA_ORDER)


def load_mapping(path: Path) -> dict:
    """The mapping table, with the types SAREF adds derived into `datatypes`.

    Each entry under `schema.root_only.types` already records the `uri` it stands for, and a
    datatype mapping is that the other way round, so the rows are computed rather than restated
    alongside it. A row written in `datatypes` wins, so the table can still override one.
    """
    mapping = yaml.safe_load(path.read_text())
    derived = {body["uri"]: name
               for name, body in mapping["schema"]["root_only"]["types"].items()}
    mapping["datatypes"] = derived | (mapping["datatypes"] or {})
    return mapping


def main() -> int:
    here = Path(__file__).resolve().parent.parent
    
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="ontology in Turtle")
    parser.add_argument("-o", "--out", type=Path, required=True, help="LinkML schema to write")
    parser.add_argument("-m", "--mapping", type=Path, default=here / "mapping/mapping_table.yaml")
    args = parser.parse_args()
    # temporary for debugging
    # mapping = load_mapping(here / "mapping/mapping_table.yaml")
    # graph = Graph().parse(str(here / "source/saref4bldg/saref4bldg.ttl"), format="turtle")
    # converter = Converter(graph, mapping, "test")

    mapping = load_mapping(args.mapping)
    graph = Graph().parse(args.source, format="turtle")
    converter = Converter(graph, mapping, args.out.stem)
    
    schema = converter.run(here)

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
