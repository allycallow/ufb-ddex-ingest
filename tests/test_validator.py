import os

import pytest

from validator import ManifestValidationError, validate_manifest

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name: str) -> str:
    return os.path.join(FIXTURES, name)


def test_valid_ern_43_manifest_passes():
    assert (
        validate_manifest(fixture("valid-ern-43-purge.xml"))
        == "http://ddex.net/xml/ern/43"
    )


def test_valid_ern_382_manifest_passes():
    assert (
        validate_manifest(fixture("valid-ern-382-purge.xml"))
        == "http://ddex.net/xml/ern/382"
    )


def test_malformed_xml_is_rejected():
    with pytest.raises(ManifestValidationError, match="not well-formed"):
        validate_manifest(fixture("malformed.xml"))


def test_unsupported_ern_version_is_rejected():
    with pytest.raises(ManifestValidationError, match="Unsupported or unrecognized"):
        validate_manifest(fixture("unsupported-version.xml"))


def test_schema_invalid_manifest_is_rejected():
    with pytest.raises(ManifestValidationError, match="failed ERN schema validation"):
        validate_manifest(fixture("invalid-ern-43.xml"))
