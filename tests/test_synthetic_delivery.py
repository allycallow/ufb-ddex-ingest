"""The dry-run delivery (scripts/make_synthetic_delivery.py) must pass every
stage ddex-ingest runs, or the end-to-end test would fail for reasons that
have nothing to do with the pipeline. This is also the only schema-valid
ERN 4.3 NewReleaseMessage in the suite - the mapping fixtures are
deliberately simplified."""

import os
import sys

import pytest

from checksum import ChecksumVerificationError, verify_resources
from task import _with_resource_locations, map_delivery_if_possible
from validator import validate_manifest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from make_synthetic_delivery import write_delivery, write_takedown  # noqa: E402

ERN_43 = "http://ddex.net/xml/ern/43"
MANIFEST_KEY = "partners/PADPIDA2026000001S/synthetic-001/synthetic-001.xml"


@pytest.fixture
def delivery(tmp_path):
    manifest = write_delivery(
        str(tmp_path),
        "synthetic-001",
        "PADPIDA2026000001S",
        "PADPIDA2026000002S",
        "SYN001",
    )
    return manifest, os.path.dirname(manifest)


def _fetch_from(folder):
    def fetch(bucket, key):
        with open(
            os.path.join(folder, os.path.relpath(key, os.path.dirname(MANIFEST_KEY))),
            "rb",
        ) as f:
            return f.read()

    return fetch


def test_passes_xsd_validation(delivery):
    manifest, _ = delivery

    assert validate_manifest(manifest) == ERN_43


def test_passes_checksum_verification(delivery):
    manifest, folder = delivery

    verified = verify_resources(
        manifest, "bucket", MANIFEST_KEY, fetch=_fetch_from(folder)
    )

    assert sorted(os.path.basename(k) for k in verified) == [
        "A1.wav",
        "A2.wav",
        "cover.jpg",
    ]


def test_fails_checksum_verification_when_a_resource_is_tampered_with(delivery):
    manifest, folder = delivery
    with open(os.path.join(folder, "resources", "A1.wav"), "ab") as f:
        f.write(b"corrupt")

    with pytest.raises(ChecksumVerificationError, match="A1.wav"):
        verify_resources(manifest, "bucket", MANIFEST_KEY, fetch=_fetch_from(folder))


def test_maps_to_a_new_release_with_tracks_and_cover_art(delivery):
    manifest, _ = delivery

    mapped = map_delivery_if_possible(manifest, ERN_43)
    mapped = _with_resource_locations(mapped, "bucket", MANIFEST_KEY, None)

    assert mapped["delivery_type"] == "new_release"
    assert mapped["catalog_number"] == "SYN001"
    assert [t["isrc"] for t in mapped["tracks"]] == ["GBSYN2600001", "GBSYN2600002"]
    assert [t["audio_key"] for t in mapped["tracks"]] == [
        "partners/PADPIDA2026000001S/synthetic-001/resources/A1.wav",
        "partners/PADPIDA2026000001S/synthetic-001/resources/A2.wav",
    ]
    assert mapped["cover_art_uri"] == "resources/cover.jpg"


def test_takedown_is_schema_valid_and_maps_to_the_catalog_number(tmp_path):
    manifest = write_takedown(
        str(tmp_path),
        "synthetic-takedown",
        "PADPIDA2026000001S",
        "PADPIDA2026000002S",
        "SYN001",
    )

    assert validate_manifest(manifest) == ERN_43
    assert map_delivery_if_possible(manifest, ERN_43) == {
        "delivery_type": "takedown",
        "catalog_number": "SYN001",
        "grid": None,
    }
