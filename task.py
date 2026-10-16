import dataclasses
import json
import logging
import os
import posixpath
import tempfile
import uuid

import boto3
import sentry_sdk
from confluent_kafka import Producer

from checksum import ChecksumVerificationError, resolve_resource_key, verify_resources
from mapping import (
    ERN_43_NAMESPACE,
    MappingError,
    is_new_release_message,
    is_purge_release_message,
    map_purged_release,
    map_release,
)
from validator import ManifestValidationError, validate_manifest

# --- LOGGING ---
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("ddex-ingest")

# --- SENTRY ---
sentry_dsn = os.environ.get("SENTRY_DSN")
if sentry_dsn:
    sentry_sdk.init(
        dsn=sentry_dsn, environment=os.environ.get("SENTRY_ENVIRONMENT", "production")
    )

# --- EVENTBRIDGE ---
EVENT_BUS_NAME = os.environ.get("EVENTBRIDGE_BUS_NAME", "production-ufb")
EVENT_SOURCE = "ufb.ddex-ingest"

# --- KAFKA ---
KAFKA_BOOTSTRAP_SERVERS = os.environ.get("KAFKA_BOOTSTRAP_SERVERS")
# Matches modules/ddex-delivery/ecs.tf in ufb-iac - a pipeline-wide reply
# topic (not specific to this one task), so future steps (checksum
# verification, catalog ingestion) can publish their own "step" onto it too.
KAFKA_DDEX_DELIVERY_EVENTS_TOPIC = os.environ.get(
    "KAFKA_DDEX_DELIVERY_EVENTS_TOPIC", "ufb.ddex_delivery_events"
)

_kafka_producer = None


def _get_kafka_producer() -> Producer:
    global _kafka_producer
    if _kafka_producer is None:
        _kafka_producer = Producer({"bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS})
    return _kafka_producer


def emit_saga_result(
    delivery_key: str, status: str, data: dict | None = None, error: str | None = None
):
    """Publish this delivery's final result to Kafka, mirroring
    audio-processing's emit_saga_result. Consumed by ufb-temporal-worker's
    ddex_reply_listener.py, which signals DdexDeliveryWorkflow - the
    "delivery_key" field (rather than track_id) is the correlation key that
    workflow's ID is derived from.

    Best-effort: a failure to publish is logged but never fails the task -
    same reasoning as emit_progress_event, since Kafka publish failures
    shouldn't be fatal for a task whose actual job (validation) already
    succeeded or failed on its own terms.
    """
    payload = {"delivery_key": delivery_key, "step": "validate", "status": status}
    if data is not None:
        payload["data"] = data
    if error is not None:
        payload["error"] = error

    logger.info("Publishing saga result to Kafka: %s", payload)

    try:
        producer = _get_kafka_producer()
        producer.produce(
            topic=KAFKA_DDEX_DELIVERY_EVENTS_TOPIC,
            key=delivery_key.encode("utf-8"),
            value=json.dumps(payload).encode("utf-8"),
        )
        producer.flush(timeout=10)
    except Exception:
        logger.exception("Failed to publish saga result to Kafka (status=%s)", status)


def emit_progress_event(delivery_key: str, status: str, **detail):
    """Publish a progress event for this delivery to EventBridge.

    Best-effort: a failure to publish is logged but never fails the task,
    same as audio-processing's emit_progress_event.
    """

    payload = {"delivery_key": delivery_key, "status": status, **detail}
    detail_type = f"DdexDelivery.{status.capitalize()}"

    logger.info("Publishing progress event to EventBridge: %s", payload)

    try:
        response = boto3.client("events").put_events(
            Entries=[
                {
                    "Source": EVENT_SOURCE,
                    "DetailType": detail_type,
                    "Detail": json.dumps(payload),
                    "EventBusName": EVENT_BUS_NAME,
                }
            ]
        )
        if response.get("FailedEntryCount"):
            logger.warning(
                "EventBridge rejected progress event (status=%s): %s",
                status,
                response["Entries"],
            )
    except Exception:
        logger.exception(
            "Failed to publish progress event to EventBridge (status=%s)", status
        )


def map_delivery_if_possible(manifest_path: str, ern_namespace: str) -> dict | None:
    """Maps a validated manifest into the fields a catalog entry needs, for
    inclusion in this task's Kafka result - this task does not call the
    backend itself.

    Mirrors how audio-processing/track-meta work: the leaf task does its
    job and reports its own result to Kafka; the actual backend write
    happens from DdexDeliveryWorkflow (via a create_catalog_entry activity
    in ufb-temporal-worker), the same way report_audio_success/
    report_metadata_success do for the audio pipeline - not from this task
    directly. That also means the backend write gets Temporal's retry
    policy for free, instead of a single bare HTTP call with no retries.

    Returns a dict tagged with "delivery_type" - "new_release" (from
    map_release()) or "takedown" (from map_purged_release()) - so
    DdexDeliveryController on the backend knows which one it's handling.

    Returns None (not an error) for an ERN version other than 4.3
    (mapping.py is scoped to 4.3 only) - a real, documented gap, not a
    failure of this delivery.

    Raises MappingError if the mapper for whichever message type this is
    can't find what it needs (e.g. a NewReleaseMessage with no title, or a
    PurgeReleaseMessage with no GRid/ICPN) - that's a genuine problem with
    this delivery's content, not a gap in what we support.
    """
    if ern_namespace != ERN_43_NAMESPACE:
        logger.info("Skipping delivery mapping: only ERN 4.3 is mapped")
        return None

    if is_new_release_message(manifest_path):
        return {
            "delivery_type": "new_release",
            **dataclasses.asdict(map_release(manifest_path)),
        }

    if is_purge_release_message(manifest_path):
        return {
            "delivery_type": "takedown",
            **dataclasses.asdict(map_purged_release(manifest_path)),
        }

    logger.info(
        "Skipping delivery mapping: neither a NewReleaseMessage nor a "
        "PurgeReleaseMessage"
    )
    return None


def _stage_cover_art(
    media_bucket: str,
    delivery_bucket: str,
    delivery_key: str,
    *,
    s3_client=None,
) -> str | None:
    """Copies a cover art object from the (KMS-restricted) delivery
    bucket into the media bucket CloudFront already serves from, under a
    disposable staging key - the Release this cover art belongs to
    doesn't exist yet at this point, so its final
    images/artwork/<release_id>/... path isn't knowable here; the
    backend downloads this staging object itself once that id exists
    (see ufb/backend's ddex_deliveries.py:_set_cover_art), and this one
    is never read again afterward.

    Doing the copy here, rather than having the backend read the
    delivery bucket directly, means only ddex-ingest (which already has
    KMS decrypt on it for checksum verification) ever touches that
    bucket - see ufb-iac's modules/ddex-delivery security notes for why
    that's deliberately narrow.

    Returns the staged key, or None on any failure - logged, not raised:
    a release should still get cataloged even if its cover art hiccups.
    """
    _, ext = posixpath.splitext(delivery_key)
    staging_key = f"pending-ddex-covers/{uuid.uuid4().hex}{ext}"
    s3 = s3_client or boto3.client("s3")
    try:
        image_bytes = s3.get_object(Bucket=delivery_bucket, Key=delivery_key)[
            "Body"
        ].read()
        s3.put_object(Bucket=media_bucket, Key=staging_key, Body=image_bytes)
    except Exception:
        logger.exception(
            "Failed to stage cover art s3://%s/%s", delivery_bucket, delivery_key
        )
        return None
    return staging_key


def _with_resource_locations(
    mapped_delivery: dict,
    s3_bucket: str,
    manifest_key: str,
    media_bucket: str | None,
) -> dict:
    """Resolves each track's audio_uri (see mapping.py:MappedTrack) into
    the absolute bucket/key DdexDeliveryWorkflow needs to start encoding
    directly from this delivery's bucket - mirrors checksum.py's own
    resource resolution, since it's the same convention. Also stages the
    release's own cover_art_uri (see mapping.py:MappedRelease) into the
    media bucket via _stage_cover_art, when one is configured.

    A takedown's dict has no "tracks"/"cover_art_uri" keys at all, so
    this is a no-op for one rather than needing its own branch. A
    missing, externally-hosted, or unstageable URI is left unresolved -
    there's nothing to act on, or nowhere to act on it.
    """
    for track in mapped_delivery.get("tracks", []):
        audio_uri = track.get("audio_uri")
        if not audio_uri or "://" in audio_uri:
            continue
        track["audio_bucket"] = s3_bucket
        track["audio_key"] = resolve_resource_key(manifest_key, audio_uri)

    cover_art_uri = mapped_delivery.get("cover_art_uri")
    if cover_art_uri and "://" not in cover_art_uri and media_bucket:
        cover_art_key = resolve_resource_key(manifest_key, cover_art_uri)
        staged_key = _stage_cover_art(media_bucket, s3_bucket, cover_art_key)
        if staged_key is not None:
            mapped_delivery["cover_art_bucket"] = media_bucket
            mapped_delivery["cover_art_key"] = staged_key

    return mapped_delivery


def main():
    s3_bucket = os.environ["S3_BUCKET"]
    s3_key = os.environ["S3_KEY"]
    # Optional: without it, cover art is simply never staged - a release
    # should still get cataloged even where this isn't configured.
    media_bucket = os.environ.get("MEDIA_BUCKET")

    logger.info("Validating manifest s3://%s/%s", s3_bucket, s3_key)
    emit_progress_event(s3_key, "started", s3_bucket=s3_bucket)

    try:
        with tempfile.NamedTemporaryFile(suffix=".xml") as tmp:
            boto3.client("s3").download_file(s3_bucket, s3_key, tmp.name)
            ern_namespace = validate_manifest(tmp.name)

            logger.info("Manifest validated successfully (%s)", ern_namespace)
            emit_progress_event(s3_key, "validated", ern_namespace=ern_namespace)

            logger.info("Verifying resource checksums")
            verified_resources = verify_resources(tmp.name, s3_bucket, s3_key)

            logger.info(
                "Checksums verified for %d resource(s)", len(verified_resources)
            )
            emit_progress_event(
                s3_key, "checksums_verified", resources=verified_resources
            )

            mapped_delivery = map_delivery_if_possible(tmp.name, ern_namespace)
            if mapped_delivery is not None:
                mapped_delivery = _with_resource_locations(
                    mapped_delivery, s3_bucket, s3_key, media_bucket
                )
    except (ManifestValidationError, ChecksumVerificationError, MappingError) as exc:
        # Not sent to Sentry - a rejected delivery is expected partner input,
        # not a bug in this task. The "rejected" EventBridge event is the
        # signal to alert on (see the DLQ-alerting ticket).
        logger.error("Delivery rejected: %s", exc)
        emit_progress_event(s3_key, "rejected", reason=str(exc))
        emit_saga_result(s3_key, "failed", error=str(exc))
        raise
    except Exception as exc:
        sentry_sdk.capture_exception(exc)
        emit_progress_event(s3_key, "failed", error=str(exc))
        emit_saga_result(s3_key, "failed", error=str(exc))
        raise

    if mapped_delivery is None:
        emit_progress_event(s3_key, "delivery_mapping_skipped")
    elif mapped_delivery["delivery_type"] == "new_release":
        emit_progress_event(s3_key, "release_mapped", title=mapped_delivery["title"])
    else:
        emit_progress_event(
            s3_key,
            "takedown_mapped",
            catalog_number=mapped_delivery["catalog_number"],
            grid=mapped_delivery["grid"],
        )

    emit_saga_result(
        s3_key,
        "success",
        data={
            "ern_namespace": ern_namespace,
            "verified_resources": verified_resources,
            "mapped_delivery": mapped_delivery,
        },
    )


if __name__ == "__main__":
    main()
