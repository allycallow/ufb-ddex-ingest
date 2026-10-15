import logging
import os

from lxml import etree

logger = logging.getLogger("ddex-ingest.validator")

SCHEMAS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schemas")

# Maps the ERN namespace URI declared on a manifest's root element to the
# vendored schema that validates it. Only the versions we've actually
# vendored and tested are listed - an unrecognized namespace is rejected
# explicitly rather than silently skipped.
SUPPORTED_NAMESPACES = {
    "http://ddex.net/xml/ern/43": os.path.join(
        SCHEMAS_DIR, "ern-43", "release-notification.xsd"
    ),
    "http://ddex.net/xml/ern/382": os.path.join(
        SCHEMAS_DIR, "ern-382", "release-notification.xsd"
    ),
}

_schema_cache: dict[str, etree.XMLSchema] = {}


class ManifestValidationError(Exception):
    """Raised when a manifest is malformed, declares an unsupported ERN
    version, or fails schema validation."""


def _get_schema(namespace: str) -> etree.XMLSchema:
    if namespace not in _schema_cache:
        schema_doc = etree.parse(SUPPORTED_NAMESPACES[namespace])
        _schema_cache[namespace] = etree.XMLSchema(schema_doc)
    return _schema_cache[namespace]


def validate_manifest(path: str) -> str:
    """Validate an ERN manifest file against the DDEX XSD for its declared
    version. Returns the matched ERN namespace on success. Raises
    ManifestValidationError on malformed XML, an unsupported/unrecognized
    ERN namespace, or a schema validation failure.
    """

    try:
        doc = etree.parse(path)
    except etree.XMLSyntaxError as exc:
        raise ManifestValidationError(
            f"Manifest is not well-formed XML: {exc}"
        ) from exc

    namespace = etree.QName(doc.getroot()).namespace

    if namespace not in SUPPORTED_NAMESPACES:
        raise ManifestValidationError(
            f"Unsupported or unrecognized ERN namespace: {namespace!r}. "
            f"Supported: {sorted(SUPPORTED_NAMESPACES)}"
        )

    schema = _get_schema(namespace)

    if not schema.validate(doc):
        errors = "; ".join(str(e) for e in schema.error_log)
        raise ManifestValidationError(
            f"Manifest failed ERN schema validation: {errors}"
        )

    logger.info("Manifest validated successfully against %s", namespace)
    return namespace
