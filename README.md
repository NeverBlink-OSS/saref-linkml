<div align="center">

[![CI](https://img.shields.io/github/actions/workflow/status/NeverBlink-OSS/saref-linkml/ci.yml?branch=main&style=flat-square&logo=githubactions&logoColor=white&label=CI)](https://github.com/NeverBlink-OSS/saref-linkml/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-BSD_3--Clause-blue?style=flat-square&logo=opensourceinitiative&logoColor=white)](LICENSE)
[![Discord](https://img.shields.io/badge/Discord-join_chat-5865F2?style=flat-square&logo=discord&logoColor=white)](https://discord.gg/HGksVAJ6ss)
[![Playground](https://img.shields.io/badge/playground-try_it_live-8A2BE2?style=flat-square&logo=scala&logoColor=white)](https://linkml.neverblink.eu/playground/?url=https%3A%2F%2Fgithub.com%2FNeverBlink-OSS%2Fsaref-linkml%2Fblob%2Fmain%2Fschema%2Fsaref-core.yaml)
[![built with LinkML-Scala](https://img.shields.io/badge/built_with-LinkML--Scala-2D6A9F?style=flat-square)](https://github.com/NeverBlink-OSS/linkml-scala)

</div>

# SAREF-LinkML

**[SAREF](https://saref.etsi.org/) is ETSI's reference ontology for the Internet of Things.** This repository has SAREF core and three of its extensions written as [LinkML](https://linkml.io/) schemas. **We also publish SAREF SHACL shapes, RDFS, and JSON Schemas** generated from LinkML.

SAREF is published as OWL, which works well for reasoning and Semantic Web tools, but does not help with validation (constraints) or with data formats other than RDF. LinkML solves that: it is a modelling language that works across formats. You write a schema in LinkML, and it generates SHACL, RDFS, JSON Schema, Frictionless table schemas, GraphQL, ER diagrams, and code for several languages.

**We use SAREF-LinkML in our work to:**

- Guide agents when aligning data to SAREF.
- Validate SAREF data in RDF with SHACL.
- Express SAREF data in JSON and validate it with JSON Schema.
- Draw ER diagrams of SAREF classes and their relationships.
- Reason about SAREF with "light" semantics in RDFS.

> [!IMPORTANT]
> This is an unofficial, community conversion. It is not endorsed by, affiliated with, or maintained by ETSI, and it is not a normative representation of SAREF. Please report problems with these schemas here, not to ETSI.

## Releases

**[Every release](https://github.com/NeverBlink-OSS/saref-linkml/releases)** contains the LinkML schemas plus SHACL shapes, RDFS, and JSON Schema already generated from them. If you only want the artifacts, you can download them from the releases page without needing any LinkML tooling.

Tags are `v<SAREF core version>-<date>`, for example `v4.1.1-2026-08-25`. The first half says which
upstream ontology the conversion targets, the second says which conversion it is, so a fix to the
conversion alone still produces a higher version. Dated releases go out automatically on Mondays,
and only when something has actually changed. There is also a `dev` prerelease, rebuilt on every
push to `main`.

## Usage

### LinkML schemas

They are in [schema/](schema/):

- [saref-core.yaml](schema/saref-core.yaml) - SAREF core, v4.1.1
- [saref4bldg.yaml](schema/saref4bldg.yaml) - SAREF4BLDG (buildings)
- [saref4ener.yaml](schema/saref4ener.yaml) - SAREF4ENER (energy flexibility)
- [saref4grid.yaml](schema/saref4grid.yaml) - SAREF4GRID (smart grid)

If an extension that you need is not here, please [open an issue](https://github.com/saref-linkml/saref-linkml/issues/new).

Classes in LinkML are written in a YAML format. For example, the `Sensor` class looks like this:

```yaml
Sensor:
  class_uri: saref:Sensor
  title: Sensor
  description: A device designed to observe and measure one or more properties
    or states of one or more features of interest.
  mixins:
    - Device
  slots:
    - observes
  slot_usage:
    observes:
      required: true
```

### Generating SHACL, RDFS, JSON Schema, and other formats

Grab the four files from [schema/](schema/). Then, we recommend using [LinkML-Scala](https://github.com/NeverBlink-OSS/linkml-scala), which is a single binary with no runtime dependencies (see the [installation instructions](https://github.com/NeverBlink-OSS/linkml-scala/blob/main/cli/README.md)).

Generate a JSON Schema for SAREF4ENER:

```shell
linkml-scala generate json-schema --to saref4ener.schema.json schema/saref4ener.yaml
```

Generate SHACL shapes, so you can validate RDF data with any SHACL engine:

```shell
linkml-scala generate shacl --format ttl --open --to shapes.ttl schema/saref-core.yaml
```

Run `linkml-scala --help` for more generators and options.

You can also play with the schemas in the [in-browser playground](https://linkml.neverblink.eu/playground/), or use use them in [Python](https://pypi.org/project/neverblink-linkml/), [Java](https://central.sonatype.com/namespace/eu.neverblink.linkml), or [JavaScript](https://www.npmjs.com/package/@neverblink/linkml).

## Using SAREF for JSON

To make a SAREF JSON document, you need to pick a class that will be the root of the document (`tree_root`). SAREF itself does not say what that is, so you have to add one. [examples/](examples/) has two small hand-written schemas that do this, alongside JSON data that matches them:

- [device-catalog.yaml](examples/device-catalog.yaml) – a catalogue of sensors in an
  installation, the properties they observe, and the units those come in.
- [observation-log.yaml](examples/observation-log.yaml) – a batch of readings that refer to a
  sensor catalogue by IRIs.

A reading then looks like this:

```json
{
  "has_timestamp": ["2026-08-25T09:00:00Z"],
  "made_by": ["ex:thermostat-hallway"],
  "observes": ["ex:indoor-air-temperature"],
  "has_result": [{ "has_value": [21.4], "is_measured_in": ["qudt-unit:DEG_C"] }]
}
```

Those single-element arrays should be plain values. The example schemas say `multivalued: false`
on each of these slots, but `slot_usage` overrides of `multivalued` are currently dropped, so the
generated JSON Schema still asks for arrays. Same cause as the cardinality limitation below.

To check that the JSON really does match:

```shell
pip install jsonschema neverblink-linkml
python checks/check-json.py --schema examples/observation-log.yaml --data examples/observation-log.json
```

## How it works and limitations

The conversion from SAREF's original in OWL to LinkML is fully automated using the [bin/owl2linkml.py](bin/owl2linkml.py) script. It uses the translation rules defined in [mapping/mapping_table.yaml](mapping/mapping_table.yaml).

### Tests

We use ETSI's SAREF examples to test the generated SHACL shapes.

1. **The generated RDFS must match the source ontology.** [checks/check-rdfs.py](checks/check-rdfs.py) compares the class hierarchy and property coverage of the generated RDFS against the original Turtle, and reports anything lost that the mapping table does not account for.
2. **The generated SHACL must be able to validate ETSI's examples.** [checks/check-examples.py](checks/check-examples.py) validates ETSI's own examples against the generated SHACL shapes, with some exceptions (some examples are not fully compliant or incomplete).
3. **The LinkML schema must be valid** under `linkml-scala validate --strict`.
4. **The hand-written JSON examples must validate against the generated JSON Schema.** [checks/check-json.py](checks/check-json.py) generates a JSON Schema from each schema in `examples/` and validates the JSON file next to it.


To run the checks locally:

```shell
pip install rdflib pyyaml pyshacl jsonschema
linkml-scala generate shacl --open --to build/shapes.nt schema/saref-core.yaml
linkml-scala generate rdfs --to build/rdfs.nt schema/saref-core.yaml
python checks/check-rdfs.py --rdfs build/rdfs.nt --schema schema/saref-core.yaml \
  --source source/SAREFCore/saref.ttl
python checks/check-examples.py --shapes build/shapes.nt --schema schema/saref-core.yaml \
  --examples source/SAREFCore/examples --mode open
python checks/check-json.py --schema examples/device-catalog.yaml --data examples/device-catalog.json
```

Most of ETSI's examples needed a small edit before they would validate, usually because they use a term without ever saying what type it is, or because they predate a cardinality the ontology now requires. Every edit is noted down: [source/README.md](source/README.md) lists what was changed.

### Limitations

- `owl:minCardinality 1` becomes `required: true`. Temporarily, only an upper bound of exactly 1 is expressible (max 1, cardinality 1 -> `multivalued: false`), larger upper bounds are dropped and reported. `owl:minCardinality 0` is skipped.
- Two restrictions are read as closed, stricter than the axiom: `owl:someValuesFrom X` becomes `required: true` and `range: X`, and `owl:hasValue` becomes a closed enum. `owl:allValuesFrom` also becomes a range.
- By default all slots are `multivalued: true`, constraints restrict that per class basis. Exception: `owl:FunctionalProperty` sets `multivalued: false` on the slot itself, schema-wide.
- When there are multiple restrictions on the same slot, the WIDEST is taken (e.g. `saref:represents`). The loser is dropped whole, not just the key that lost.

## Contributing and support

Bug reports, questions, and pull requests are welcome. If a class or slot came out wrong, an issue with the offending term and what you expected is the most useful thing you can send.

The files in [schema/](schema/) are generated, and CI overwrites them. To fix the translation, edit the mapping table or the converter. [CONTRIBUTING.md](CONTRIBUTING.md) explains how to set up the environment, how to regenerate the schemas, how to run the checks, and how to add an extension or move to a new SAREF version.

This project follows a [Code of Conduct](CODE_OF_CONDUCT.md).

You can also **[join our Discord](https://discord.gg/HGksVAJ6ss)** to ask questions.

## License and provenance

The LinkML schemas here are a conversion of ETSI's SAREF ontologies from OWL/Turtle into LinkML. The source ontologies are Copyright 2019 ETSI and licensed under the BSD 3-Clause License. The same license applies to this repository, and its terms carry over to the converted schemas. Class and slot definitions, labels, and descriptions are derived from the ETSI sources. The LinkML modelling decisions, the converter, and the tooling are the work of this project's contributors. See [LICENSE](LICENSE).

"SAREF" is used here descriptively, to identify the source ontology.

## Maintainers

This project is developed and maintained by [NeverBlink](https://neverblink.eu). For inquiries, write to us at [contact@neverblink.eu](mailto:contact@neverblink.eu).

----

*This work has been supported by the HEDGE-IoT project grant number 101136216 funded by the European Commission as part of the Horizon Europe Framework Programme. However, views and opinions expressed are those of the authors only and do not necessarily reflect those of the European Union or the European Climate, Infrastructure and Environment Executive Agency. Neither the European Union nor the granting authority can be held responsible for them.*
