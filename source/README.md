# Sources

The ETSI ontologies these schemas are converted from, and the example data ETSI ships with them.

Nothing in this directory is generated, and nothing in it should be edited to make a conversion
work. If a conversion comes out wrong, the fix belongs in
[../mapping/mapping_table.yaml](../mapping/mapping_table.yaml) or
[../bin/owl2linkml.py](../bin/owl2linkml.py).

## Ontologies

The four Turtle files are byte-for-byte copies of what the ETSI portal serves.

To check that a file still matches what ETSI publishes:

```shell
curl -sSL https://saref.etsi.org/core/v4.1.1/saref.ttl | sha256sum
sha256sum source/SAREFCore/saref.ttl
```

Version IRIs are pinned in the URLs above, so ETSI publishing a newer SAREF will not change these
files. Moving to a new version is a deliberate step: download it, regenerate, and look at what the
diff does to the schemas.

## Examples

The `examples/` directory under each ontology holds the example data ETSI publishes for it: 13
files for SAREF core, 1 for SAREF4BLDG, 8 for SAREF4ENER, 12 for SAREF4GRID.
[../checks/check-examples.py](../checks/check-examples.py) validates all of them against SHACL
shapes generated from the corresponding schema. That is how we catch a conversion that came out
stricter than the ontology intended: if ETSI's own example fails, the constraint is wrong.

Most of these files were edited. The reason is always the same: SAREF core is deliberately
permissive, but the extensions add cardinality restrictions, and ETSI's own examples do not always
satisfy them. Where an example was missing a statement that the ontology requires, it was added.

| Ontology | Examples | Edited |
| --- | --- | --- |
| SAREF core | 13 | 4 |
| SAREF4BLDG | 1 | 1 |
| SAREF4ENER | 8 | 8 |
| SAREF4GRID | 12 | 12 |

Every edit is marked in place. Most sit in a block at the end of the file:

```turtle
#################################################################
#    Added for SHACL validation, not in the ETSI original
#################################################################

# Nodes added only so that the required values below have something to point at
s4grid:ActiveEnergy a s4grid:EnergyAndPowerProperty, saref:Property .

# Values required by sh:minCount 1.
ex:Meter1234 saref:observes s4grid:ActiveEnergy .
```

A few are single inline additions instead, marked `# added, not in the ETSI original` or with a
note saying which constraint made them necessary. Grep for `ETSI original` to find all of them.

The additions fall into two kinds:

- **A term is used but never typed.** SHACL cannot check a node it does not know the class of, so
  the missing `a saref:Something` was added. This is a gap in the example, not in the ontology.
- **A restriction in the ontology is not satisfied.** The example is missing a property the
  ontology says is required, so the property was added along with anything it needs to point at.

The second kind is a judgement call and worth being upfront about. Adding a statement to make a
constraint pass hides the possibility that the constraint is the thing that is wrong. We took the
view that a cardinality ETSI wrote down deliberately should be respected, and that an example
predating it is the weaker evidence. If you think a specific case got this backwards, that is a
good issue to open - the alternative is to loosen the conversion, and we would rather discuss it
than guess.

No example was edited to work around a bug in the converter. Where the conversion was wrong, the
fix went into the mapping table.

## Licensing

These files are Copyright 2019 ETSI, licensed under the BSD 3-Clause License, which is the same
licence this repository uses. See [../LICENSE](../LICENSE) and
[the provenance section of the README](../README.md#license-and-provenance).
