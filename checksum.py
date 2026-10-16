import hashlib
import logging
import posixpath
import zlib

import boto3
from lxml import etree

logger = logging.getLogger("ddex-ingest.checksum")

# Covers what both ERN 4.3's and 3.8.2's HashSumAlgorithmType enum actually
# offer that Python can verify. CRC32 isn't a hashlib algorithm, handled
# separately. Anything else declared (MD2, legacy variants, etc.) is
# rejected explicitly via ChecksumVerificationError rather than silently
# skipped - a declared-but-unverifiable checksum is not the same as no
# checksum.
_HASHLIB_ALGORITHMS = {
    "MD4": "md4",
    "MD5": "md5",
    "SHA": "sha1",
    "SHA1": "sha1",
    "SHA224": "sha224",
    "SHA-224": "sha224",
    "SHA256": "sha256",
    "SHA-256": "sha256",
    "SHA384": "sha384",
    "SHA-384": "sha384",
    "SHA512": "sha512",
    "SHA-512": "sha512",
}


class ChecksumVerificationError(Exception):
    """Raised when one or more resources fail checksum verification."""


def _crc32_of(data: bytes) -> str:
    return format(zlib.crc32(data) & 0xFFFFFFFF, "08x")


def _hash_bytes(data: bytes, algorithm: str) -> str:
    key = algorithm.upper()
    if key == "CRC32":
        return _crc32_of(data)

    hashlib_name = _HASHLIB_ALGORITHMS.get(key)
    if hashlib_name is None:
        raise ChecksumVerificationError(f"Unsupported hash algorithm: {algorithm}")

    try:
        h = hashlib.new(hashlib_name, usedforsecurity=False)
    except ValueError as exc:
        raise ChecksumVerificationError(
            f"Hash algorithm {algorithm} not available in this runtime: {exc}"
        ) from exc

    h.update(data)
    return h.hexdigest()


def _resource_refs(doc) -> list[tuple[str, str, str]]:
    """Returns (relative_path, algorithm, expected_hash) for every <File>
    with a HashSum in the manifest.

    Handles both ERN 4.3 (URI + HashSum/Algorithm + HashSum/HashSumValue)
    and ERN 3.8.2 (FileName[+FilePath] + HashSum/HashSum +
    HashSum/HashSumAlgorithmType - note the inner element is also named
    HashSum in 3.8.2's schema) shapes, detected by which children are
    present rather than by the manifest's declared ERN version, since the
    shape itself is unambiguous.

    elementFormDefault="unqualified" in both schemas means these are plain,
    unprefixed local element names regardless of the root's namespace, so
    a bare find()/findall() (no namespace map) is correct here.
    """
    refs = []
    for file_el in doc.findall(".//File"):
        hashsum_el = file_el.find("HashSum")
        if hashsum_el is None:
            continue  # HashSum is optional in both schemas - nothing to verify

        uri_el = file_el.find("URI")
        if uri_el is not None:
            path = uri_el.text or ""
            algorithm_el = hashsum_el.find("Algorithm")
            value_el = hashsum_el.find("HashSumValue")
        else:
            filename_el = file_el.find("FileName")
            if filename_el is None:
                continue  # URL-only reference - nothing local to verify
            filepath_el = file_el.find("FilePath")
            path = (filepath_el.text or "" if filepath_el is not None else "") + (
                filename_el.text or ""
            )
            algorithm_el = hashsum_el.find("HashSumAlgorithmType")
            value_el = hashsum_el.find("HashSum")

        if algorithm_el is None or value_el is None or not path:
            continue

        refs.append(
            (
                path.strip(),
                (algorithm_el.text or "").strip(),
                (value_el.text or "").strip(),
            )
        )

    return refs


def _default_fetch(bucket: str, key: str) -> bytes:
    return boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()


def resolve_resource_key(manifest_key: str, relative_path: str) -> str:
    """Resolves a File/URI's relative path against the manifest's own S3
    "directory" (its key's dirname) - the DDEX-documented convention this
    bucket's deliveries follow (see modules/ddex-delivery in ufb-iac).
    Shared with task.py, which uses it to resolve a track's audio file
    into the bucket/key DdexDeliveryWorkflow needs to start encoding -
    the same resolution verify_resources applies below, just for a
    reference that isn't being checksum-verified here.
    """
    manifest_dir = posixpath.dirname(manifest_key)
    return posixpath.normpath(posixpath.join(manifest_dir, relative_path))


def verify_resources(
    manifest_path: str, s3_bucket: str, manifest_key: str, *, fetch=_default_fetch
) -> list[str]:
    """Verifies every resource file referenced in the manifest against its
    declared checksum. Returns the list of verified S3 keys on success.

    Resources are resolved relative to the manifest's own S3 "directory"
    (its key's dirname) - the DDEX-documented convention for a delivery
    where resources are uploaded alongside the manifest, which is how this
    bucket's deliveries land (see modules/ddex-delivery in ufb-iac). A
    File/URI that looks like an absolute URL (contains "://") points at
    something hosted outside this delivery entirely and is skipped rather
    than guessed at.

    Raises ChecksumVerificationError naming every resource that failed, not
    just the first - a tampered or incomplete delivery is worth describing
    precisely rather than stopping at the first problem.
    """
    doc = etree.parse(manifest_path)
    refs = _resource_refs(doc)

    if not refs:
        logger.info("No resources with a HashSum to verify")
        return []

    verified = []
    failures = []

    for relative_path, algorithm, expected_hash in refs:
        if "://" in relative_path:
            logger.warning(
                "Skipping externally-hosted resource (not in this bucket): %s",
                relative_path,
            )
            continue

        resource_key = resolve_resource_key(manifest_key, relative_path)

        try:
            data = fetch(s3_bucket, resource_key)
        except Exception as exc:
            failures.append(f"{resource_key}: could not download ({exc})")
            continue

        try:
            actual_hash = _hash_bytes(data, algorithm)
        except ChecksumVerificationError as exc:
            failures.append(f"{resource_key}: {exc}")
            continue

        if actual_hash.lower() != expected_hash.lower():
            failures.append(
                f"{resource_key}: {algorithm} mismatch "
                f"(expected {expected_hash}, got {actual_hash})"
            )
        else:
            logger.info("Verified %s (%s)", resource_key, algorithm)
            verified.append(resource_key)

    if failures:
        raise ChecksumVerificationError("; ".join(failures))

    return verified
