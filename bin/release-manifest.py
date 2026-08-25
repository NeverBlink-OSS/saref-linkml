#!/usr/bin/env python3
"""Describe a release: which schemas it contains and where they came from.

Writes MANIFEST.json next to the release artifacts so a consumer can tell, without
guessing from file names, which SAREF version each schema was converted from and which
commit and toolchain produced the files.

    bin/release-manifest.py --version 4.1.1-2026-08-25 --channel release -o dist/MANIFEST.json

With --core-version it prints nothing but the SAREF core version, which is what the
release workflow uses to build the tag name.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

# Every schema records its upstream version as a versioned namespace, e.g.
# https://saref.etsi.org/core/v4.1.1/ - the bare number is the part we want.
VERSION_URL = re.compile(r"/v(?P<version>[0-9]+(?:\.[0-9]+)*)/?$")


def upstream_version(schema: dict, path: Path) -> str:
    version = schema.get("version")
    if not version:
        sys.exit(f"{path}: no 'version', cannot tell which SAREF version this is")
    match = VERSION_URL.search(str(version))
    if not match:
        sys.exit(f"{path}: version '{version}' is not a versioned SAREF namespace")
    return match.group("version")


def describe(path: Path) -> dict:
    schema = yaml.safe_load(path.read_text())
    return {
        "name": schema.get("name", path.stem),
        "file": path.name,
        "title": schema.get("title"),
        "id": schema.get("id"),
        "saref_version": upstream_version(schema, path),
        "converted_from": schema.get("source"),
        "license": schema.get("license"),
    }


def main() -> int:
    here = Path(__file__).resolve().parent.parent

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--schema-dir", type=Path, default=here / "schema")
    parser.add_argument("--core-version", action="store_true",
                        help="print the SAREF core version and exit")
    parser.add_argument("--version", help="release version, e.g. 4.1.1-2026-08-25")
    parser.add_argument("--channel", choices=["release", "dev"], default="release")
    parser.add_argument("--date", help="release date, YYYY-MM-DD")
    parser.add_argument("--commit", default="", help="commit the release was built from")
    parser.add_argument("--repository", default="", help="owner/name of the source repository")
    parser.add_argument("--engine", default="", help="linkml-scala-action ref used to generate")
    parser.add_argument("-o", "--out", type=Path, help="MANIFEST.json to write (default: stdout)")
    args = parser.parse_args()

    schemas = sorted(args.schema_dir.glob("*.yaml"))
    if not schemas:
        sys.exit(f"no schemas in {args.schema_dir}")
    described = [describe(path) for path in schemas]

    if args.core_version:
        core = next((s for s in described if s["name"] == "saref-core"), None)
        if not core:
            sys.exit(f"no saref-core.yaml in {args.schema_dir}")
        print(core["saref_version"])
        return 0

    if not args.version:
        sys.exit("--version is required unless --core-version is given")

    manifest = {
        "version": args.version,
        "channel": args.channel,
        "released_on": args.date,
        "commit": args.commit,
        "repository": args.repository,
        "generated_with": args.engine,
        "notice": "Unofficial community conversion of SAREF into LinkML. Not endorsed by ETSI.",
        "schemas": described,
    }
    text = json.dumps(manifest, indent=2) + "\n"
    if args.out:
        args.out.write_text(text)
        print(f"{args.out}: {len(described)} schema(s)")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
