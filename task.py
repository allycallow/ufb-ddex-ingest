import json
import logging
import os
import tempfile

import boto3
import sentry_sdk
from confluent_kafka import Producer

from checksum import ChecksumVerificationError, verify_resources
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
    """Publish this delivery's final validation result to Kafka, mirroring
    audio-processing's emit_saga_result.

    NOTE: as of writing, nothing consumes this topic yet. ufb-temporal-worker
    has no DDEX equivalent of track_ingestion_saga.py / kafka_trigger.py /
    reply_listener.py - no workflow starts when a delivery lands on
    ufb.ddex_deliveries, and nothing signals one from this topic. This is
    wired up ready for that workflow, which is a separate piece of work. The
    "delivery_key" field (rather than track_id) is a placeholder correlation
    key - whatever starts the eventual workflow needs to derive the same
    workflow ID from the same value for a signal to find it.

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


def main():
    s3_bucket = os.environ["S3_BUCKET"]
    s3_key = os.environ["S3_KEY"]

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
    except (ManifestValidationError, ChecksumVerificationError) as exc:
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

    logger.info("Checksums verified for %d resource(s)", len(verified_resources))
    emit_progress_event(s3_key, "checksums_verified", resources=verified_resources)
    emit_saga_result(
        s3_key,
        "success",
        data={"ern_namespace": ern_namespace, "verified_resources": verified_resources},
    )


if __name__ == "__main__":
    main()
