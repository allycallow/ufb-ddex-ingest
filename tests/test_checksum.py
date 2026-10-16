import os

import pytest
from lxml import etree

from checksum import (
    ChecksumVerificationError,
    _hash_bytes,
    _resource_refs,
    resolve_resource_key,
    verify_resources,
)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

CONTENT = b"hello world\n"
CONTENT_MD5 = "6f5902ac237024bdd0c176cb93063dc4"
CONTENT_CRC32 = "af083b2d"


def fixture(name: str) -> str:
    return os.path.join(FIXTURES, name)


def test_resource_refs_parses_ern_43_shape():
    doc = etree.parse(fixture("checksum-ern43.xml"))
    assert _resource_refs(doc) == [("track.wav", "MD5", CONTENT_MD5)]


def test_resource_refs_parses_ern_382_shape():
    doc = etree.parse(fixture("checksum-ern382.xml"))
    assert _resource_refs(doc) == [("resources/track.wav", "CRC32", CONTENT_CRC32)]


def test_resource_refs_skips_files_without_hashsum():
    doc = etree.fromstring(
        b"<ResourceList><File><URI>no-hash.wav</URI></File></ResourceList>"
    )
    assert _resource_refs(doc) == []


@pytest.mark.parametrize(
    "algorithm,expected",
    [
        ("MD5", CONTENT_MD5),
        ("SHA256", "a948904f2f0f479b8f8197694b30184b0d2ed1c1cd2a1ec0fb85d299a192a447"),
        # SHA-256 (hyphenated) is the actual HashSumAlgorithmType enum value
        # in ERN 4.3's AVS schema - SHA256 above is kept for leniency, but
        # a real delivery declares the hyphenated form.
        ("SHA-256", "a948904f2f0f479b8f8197694b30184b0d2ed1c1cd2a1ec0fb85d299a192a447"),
        ("CRC32", CONTENT_CRC32),
        ("crc32", CONTENT_CRC32),  # case-insensitive
    ],
)
def test_hash_bytes_matches_known_values(algorithm, expected):
    assert _hash_bytes(CONTENT, algorithm) == expected


def test_hash_bytes_rejects_unsupported_algorithm():
    with pytest.raises(ChecksumVerificationError, match="Unsupported hash algorithm"):
        _hash_bytes(CONTENT, "MD2")


def test_verify_resources_succeeds_when_hash_matches():
    def fake_fetch(bucket: str, key: str) -> bytes:
        assert bucket == "test-bucket"
        assert key == "partners/labelwok/track.wav"
        return CONTENT

    verified = verify_resources(
        fixture("checksum-ern43.xml"),
        "test-bucket",
        "partners/labelwok/manifest.xml",
        fetch=fake_fetch,
    )

    assert verified == ["partners/labelwok/track.wav"]


def test_verify_resources_resolves_ern_382_filepath():
    def fake_fetch(bucket: str, key: str) -> bytes:
        assert key == "partners/labelwok/resources/track.wav"
        return CONTENT

    verified = verify_resources(
        fixture("checksum-ern382.xml"),
        "test-bucket",
        "partners/labelwok/manifest.xml",
        fetch=fake_fetch,
    )

    assert verified == ["partners/labelwok/resources/track.wav"]


def test_verify_resources_fails_on_hash_mismatch():
    def fake_fetch(bucket: str, key: str) -> bytes:
        return b"tampered content"

    with pytest.raises(ChecksumVerificationError, match="MD5 mismatch"):
        verify_resources(
            fixture("checksum-ern43.xml"),
            "test-bucket",
            "partners/labelwok/manifest.xml",
            fetch=fake_fetch,
        )


def test_verify_resources_fails_when_resource_missing():
    def fake_fetch(bucket: str, key: str) -> bytes:
        raise Exception("NoSuchKey")

    with pytest.raises(ChecksumVerificationError, match="could not download"):
        verify_resources(
            fixture("checksum-ern43.xml"),
            "test-bucket",
            "partners/labelwok/manifest.xml",
            fetch=fake_fetch,
        )


def test_verify_resources_skips_externally_hosted_files():
    doc = b"""<ResourceList>
      <File>
        <URI>https://example.com/track.wav</URI>
        <HashSum><Algorithm>MD5</Algorithm><HashSumValue>abc</HashSumValue></HashSum>
      </File>
    </ResourceList>"""
    path = os.path.join(FIXTURES, "_tmp_external.xml")
    with open(path, "wb") as f:
        f.write(doc)
    try:

        def fake_fetch(bucket: str, key: str) -> bytes:
            raise AssertionError("should not fetch an externally-hosted resource")

        verified = verify_resources(
            path, "test-bucket", "x/manifest.xml", fetch=fake_fetch
        )
        assert verified == []
    finally:
        os.remove(path)


def test_verify_resources_returns_empty_list_when_no_resources_have_hashes():
    path = os.path.join(FIXTURES, "valid-ern-43-purge.xml")
    assert verify_resources(path, "test-bucket", "x/manifest.xml") == []


def test_resolve_resource_key_joins_relative_to_manifest_dir():
    assert (
        resolve_resource_key("partners/labelworx/x/manifest.xml", "resources/track.wav")
        == "partners/labelworx/x/resources/track.wav"
    )


def test_resolve_resource_key_normalizes_dot_segments():
    assert (
        resolve_resource_key("partners/labelworx/x/manifest.xml", "./track.wav")
        == "partners/labelworx/x/track.wav"
    )


def test_resolve_resource_key_with_manifest_at_bucket_root():
    assert resolve_resource_key("manifest.xml", "track.wav") == "track.wav"
