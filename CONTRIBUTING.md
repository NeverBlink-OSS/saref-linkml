# Contributing to SAREF-LinkML

Thanks for your interest. Issues and pull requests are both welcome, and reports about a term that
converted wrong are the most useful thing you can send.

Want to ask something before opening a PR? **[Join our Discord](https://discord.gg/HGksVAJ6ss)**.

## How to edit the schemas

**Don't edit the files in [schema/](schema/).** They are generated, and CI regenerates them on
every push and fails if they differ. A change made there will be reverted the next time anyone
runs the converter.

A fix belongs in one of two places:

- [mapping/mapping_table.yaml](mapping/mapping_table.yaml) if it is a decision about how OWL turns
  into LinkML. Most fixes are this.
- [bin/owl2linkml.py](bin/owl2linkml.py) if the table cannot express what you need. Adding a row
  type to the table is usually better than special-casing something in the converter.

Then regenerate and commit the result along with your change.

## Setting up

You need Python 3.12 or newer and the LinkML-Scala CLI. Everything the converter and the checks
need is in [requirements.txt](requirements.txt).

```shell
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
. <(curl -sSfL https://raw.githubusercontent.com/NeverBlink-OSS/linkml-scala/refs/heads/main/cli/install.sh)
```

## Regenerating the schemas

One command per ontology:

```shell
python bin/owl2linkml.py source/SAREFCore/saref.ttl        -o schema/saref-core.yaml
python bin/owl2linkml.py source/saref4bldg/saref4bldg.ttl  -o schema/saref4bldg.yaml
python bin/owl2linkml.py source/saref4ener/saref4ener.ttl  -o schema/saref4ener.yaml
python bin/owl2linkml.py source/saref4grid/saref4grid.ttl  -o schema/saref4grid.yaml
python bin/owl2linkml.py source/saref4watr/saref4watr.ttl  -o schema/saref4watr.yaml
```

The converter prints a `note:` line on stderr for anything in the source it did not know what to
do with. Read those. A new note means either the mapping table needs a row, or the term genuinely
has no LinkML counterpart and belongs under `report_ignored` with a reason.

Extensions import `saref-core`, so regenerate that one first if you changed anything it affects.

## Running the checks

Same three things CI runs. For each schema:

```shell
# 1. the schema itself is valid
linkml-scala validate --strict schema/saref-core.yaml

# 2. the generated RDFS still describes the source ontology
linkml-scala generate rdfs --to build/rdfs.nt schema/saref-core.yaml
python checks/check-rdfs.py --rdfs build/rdfs.nt --schema schema/saref-core.yaml \
  --source source/SAREFCore/saref.ttl

# 3. ETSI's own examples still validate against the generated SHACL
linkml-scala generate shacl --open --to build/shapes.nt schema/saref-core.yaml
python checks/check-examples.py --shapes build/shapes.nt --schema schema/saref-core.yaml \
  --examples source/SAREFCore/examples --mode open
```

And for the hand-written examples:

```shell
python checks/check-json.py --schema examples/device-catalog.yaml  --data examples/device-catalog.json
python checks/check-json.py --schema examples/observation-log.yaml --data examples/observation-log.json
```

`check-examples.py --mode closed` is stricter: it also rejects any property the schema does not
declare, which catches under-coverage rather than over-constraint. CI runs `open` only.

## Adding an extension

SAREF has fifteen extensions and this repository converts four. To add another:

1. Download the ontology from the ETSI portal, pinning the version IRI in the URL. Put it under
   `source/<name>/` and add its example data too if ETSI publishes any.
2. Add its checksum and download URL to [source/README.md](source/README.md).
3. Run the converter. Read every note it prints.
4. Add it to the `convert` and `check` matrices in
   [.github/workflows/ci.yml](.github/workflows/ci.yml). The `validate` job and the release build
   glob `schema/*.yaml`, so those two pick it up on their own.
5. Add a row to the schema table in the README, and update the extension status table.

Expect the mapping table to need work. Each extension so far has used OWL constructs the previous
one did not.

## Updating to a new SAREF version

Same shape, but replace the source file rather than adding one, and look hard at the schema diff.
A version bump that changes hundreds of lines is normal; a version bump that drops classes is
worth understanding before merging.

## Releases

You do not need to do anything to cut a release. [.github/workflows/release.yml](.github/workflows/release.yml)
runs on Mondays, works out whether anything has changed since the last one, and if so runs the full
check suite and publishes a tagged release with the schemas and the SHACL, RDFS and JSON Schema
generated from them. The `dev` prerelease is rebuilt on every push to `main`.

Two consequences for a pull request:

- A change that lands on `main` is downloadable as `dev` immediately, and goes out in the next
  dated release. There is no separate release PR to remember.
- The release only builds from `schema/*.yaml`. Anything you add elsewhere - a new check, a new
  example - is not packaged, so add it to [ci.yml](.github/workflows/ci.yml) if it should gate a
  release.

To publish out of cycle, run the Release workflow by hand. It takes a date and a `force` flag for
the case where nothing has changed but you want a release anyway.

## Sources and examples

[source/](source/) holds unmodified copies of the ETSI ontologies. Don't edit them to make a
conversion work. The example data under `source/*/examples/` has been edited in places, and
[source/README.md](source/README.md) explains what was changed and why.
