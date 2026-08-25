#!/usr/bin/env python3
"""Validate a JSON document against the JSON Schema generated from a LinkML schema.

Used for the hand-written examples under examples/, to show that a schema with a tree_root really
does describe a whole JSON file:

    checks/check-json.py --schema examples/device-catalog.yaml --data examples/device-catalog.json

The JSON Schema is generated here, either through the neverblink-linkml Python bindings or the
linkml-scala CLI, whichever is available. Pass --json-schema to use one that was generated
earlier.
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def generate_json_schema(schema, build):
    """The JSON Schema for `schema`, as text.

    Prefers the Python bindings, since that is what the other checks use, and falls back to the
    CLI so that a local checkout with only the binary installed still works.
    """
    try:
        import linkml_scala
    except ImportError:
        return generate_with_cli(schema, build)

    try:
        with linkml_scala.load_file(schema) as loaded:
            return loaded.json_schema()
    except linkml_scala.LinkMlError as error:
        sys.exit(f"generating JSON Schema from {schema} failed:\n{error}")


def generate_with_cli(schema, build):
    if not shutil.which("linkml-scala"):
        sys.exit("neither neverblink-linkml nor the linkml-scala CLI is available - "
                 "pip install neverblink-linkml")

    out = build / f"{schema.stem}.schema.json"
    result = subprocess.run(
        ["linkml-scala", "generate", "json-schema", "--to", str(out), str(schema)],
        capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"generating JSON Schema from {schema} failed:\n{result.stdout}{result.stderr}")
    return out.read_text()


def describe(error):
    """One line for a jsonschema ValidationError, saying where in the document it is."""
    where = "/".join(str(part) for part in error.absolute_path) or "(document root)"
    return f"{where}: {error.message}"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--schema", type=Path, required=True, help="LinkML schema with a tree_root")
    ap.add_argument("--data", type=Path, required=True, help="JSON document to validate")
    ap.add_argument("--json-schema", type=Path, default=None,
                    help="pre-generated JSON Schema; skips generating it from --schema")
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
        source, text = args.schema, generate_json_schema(args.schema, args.build)
        (args.build / f"{args.schema.stem}.schema.json").write_text(text)

    validator = jsonschema.Draft202012Validator(json.loads(text))
    document = json.loads(args.data.read_text())
    errors = sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))

    print(f"schema: {source}")
    print(f"data:   {args.data}")

    if not errors:
        print("\nok, the document matches the schema")
        return 0

    print(f"\n{len(errors)} problem(s):")
    for error in errors:
        print(f"  {describe(error)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
