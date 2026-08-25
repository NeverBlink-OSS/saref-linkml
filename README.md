# saref-linkml
SAREF ontologies in LinkML

## Releases

See: https://github.com/NeverBlink-OSS/saref-linkml/releases

Releases contain the source LinkML schemas and SHACL shapes, RDFS, and JSON Schema generated from them. 

Releases are tagged with the SAREF version they target, and the date of the release, following the pattern `v<SAREF core version>-<date>`, for example `v4.1.1-2026-08-25`. When we fix issues in the translation to LinkML, we make a new release with the same SAREF version but a later date. Dated releases go out automatically on Mondays. There is also the `dev` prerelease, which is rebuilt on every push to `main`.

## License and provenance

The LinkML schemas in this repository are a **conversion** of the [SAREF-core ontology](https://labs.etsi.org/rep/saref/saref-core), from OWL/Turtle into LinkML. The source ontology is Copyright 2019 ETSI and is licensed under the BSD 3-Clause License. That same license applies to this repository, and its terms carry over to the converted schemas. Class and slot definitions, labels, and descriptions are derived from the ETSI source. The LinkML modelling decisions, structure, and
tooling are the work of this project's contributors. See: [LICENSE](LICENSE).

This is an **unofficial, community conversion**. It is not endorsed by, affiliated with, or maintained by ETSI, and it is not a normative representation of SAREF. Please report issues with SAREF-LinkML here rather than to ETSI. "SAREF" is used descriptively to identify the source
ontology.
