#!/usr/bin/env python3
"""Validate a JSON document against the JSON Schema generated from a LinkML schema.

Used for the hand-written examples under examples/, to show that a schema with a tree_root really
does describe a whole JSON file:

    checks/check-json.py --schema examples/device-catalog.yaml --data examples/device-catalog.json

The schema is validated strictly first, so this covers both halves of the check: the schema has to
be sound, and the document has to match what it generates. Either the neverblink-linkml Python
bindings or the linkml-scala CLI will do, whichever is available.
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build_with_bindings(schema):
    """Validate `schema` strictly and return its JSON Schema, via the Python bindings.

    The example schemas import ../schema/saref-core, and the bindings resolve that against the
    filesystem. The linkml-scala GitHub Action cannot: it builds an import map from the files it
    is handed, and a path with a .. in it is not a key in that map.
    """
    import linkml_scala

    try:
        loaded = linkml_scala.load_file(schema)
    except linkml_scala.LinkMlError as error:
        # A fatal problem stops the schema loading at all.
        sys.exit(f"{schema} is not valid:\n{error}")

    with loaded:
        issues = loaded.lint().get("issues") or []
        if issues:
            report = "\n".join(f"  {i.get('severity', '?')}: {i.get('message', '')}"
                               for i in issues)
            sys.exit(f"{schema} has validation issues (strict):\n{report}")
        return loaded.json_schema()


def build_with_cli(schema, build):
    """The same, shelling out to the linkml-scala binary."""
    validated = subprocess.run(["linkml-scala", "validate", "--strict", str(schema)],
                               capture_output=True, text=True)
    if validated.returncode != 0:
        sys.exit(f"{schema} is not valid:\n{validated.stdout}{validated.stderr}")

    out = build / f"{schema.stem}.schema.json"
    generated = subprocess.run(
        ["linkml-scala", "generate", "json-schema", "--to", str(out), str(schema)],
        capture_output=True, text=True)
    if generated.returncode != 0:
        sys.exit(f"generating JSON Schema from {schema} failed:\n"
                 f"{generated.stdout}{generated.stderr}")
    return out.read_text()


def build(schema, build_dir):
    try:
        import linkml_scala  # noqa: F401
    except ImportError:
        if not shutil.which("linkml-scala"):
            sys.exit("neither neverblink-linkml nor the linkml-scala CLI is available - "
                     "pip install neverblink-linkml")
        return build_with_cli(schema, build_dir)
    return build_with_bindings(schema)


def describe(error):
    """One line for a jsonschema ValidationError, saying where in the document it is."""
    where = "/".join(str(part) for part in error.absolute_path) or "(document root)"
    return f"{where}: {error.message}"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--schema", type=Path, required=True, help="LinkML schema with a tree_root")
    ap.add_argument("--data", type=Path, required=True, help="JSON document to validate")
    ap.add_argument("--json-schema", type=Path, default=None,
                    help="pre-generated JSON Schema; skips validating and generating from --schema")
    ap.add_argument("--build", type=Path, default=ROOT / "build")
    args = ap.parse_args()

    try:
        import jsonschema
    except ImportError:
        sys.exit("jsonschema is not installed - pip install jsonschema")

    args.build.mkdir(parents=True, exist_ok=True)
    if args.json_schema:
        source, text = args.json_schema, args.json_schema.read_text()
    else:
        source, text = args.schema, build(args.schema, args.build)
        (args.build / f"{args.schema.stem}.schema.json").write_text(text)

    validator = jsonschema.Draft202012Validator(json.loads(text))
    document = json.loads(args.data.read_text())
    errors = sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))

    print(f"schema: {source}")
    print(f"data:   {args.data}")

    if not errors:
        print("\nok, the schema is valid and the document matches it")
        return 0

    print(f"\n{len(errors)} problem(s):")
    for error in errors:
        print(f"  {describe(error)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
