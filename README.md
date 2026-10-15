# UFB DDEX Ingest

Validates DDEX ERN delivery manifests landed in S3 against the official DDEX XSD schema for their declared version, as the first stage of the DDEX delivery pipeline (see `ufb-iac`'s `modules/ddex-delivery`).

This currently covers **validation only**. Checksum verification, ERN→catalog mapping, catalog ingestion, delivery-type (new/update/takedown) handling, and acknowledgment emission are separate, not-yet-built stages of the same pipeline.

## How It Works

1. Downloads the manifest from S3 (`S3_BUCKET` / `S3_KEY`)
2. Reads the root element's namespace to detect the declared ERN version
3. Validates the manifest against the matching vendored XSD (`schemas/ern-43` or `schemas/ern-382`)
4. Publishes a progress event to EventBridge (`validated` or `rejected`/`failed`)
5. Publishes its final result to Kafka (`success` or `failed`), for a Temporal workflow to react to

An unrecognized ERN namespace, malformed XML, or a schema validation failure all reject the delivery with a specific error rather than silently continuing.

## Vendored Schemas

`schemas/ern-43` and `schemas/ern-382` are the official DDEX XSDs (release-notification + the allowed-value-sets/avs schema each one imports), fetched from `service.ddex.net`/`ddex.net` (the ERN 3.8.2 release-notification file came from the `sshaw/ddex` GitHub mirror, as DDEX no longer serves that specific version at a direct URL — its imported `avs.xsd` is still fetched directly from `ddex.net`). Each `release-notification.xsd`'s single `xs:import` was rewritten to a local relative path so schema compilation never depends on reaching the internet at runtime.

**Licensing**: these schemas carry DDEX's standard licence notice — evaluating against them is covered by DDEX's Evaluation Licence, but production use requires an Implementation Licence from DDEX (free — apply via the DPID portal at https://ddex.net/implementation/implementation-licence-and-ddex-party-identifiers/, which also issues the DPID needed to identify UFB as sender/recipient on ERN messages). Confirm UFB holds one before this goes live against real partner deliveries.

Supporting a new ERN version means vendoring its schema bundle the same way and adding its namespace to `validator.SUPPORTED_NAMESPACES`.

## Progress Events

Publishes to the `production-ufb` EventBridge bus (source `ufb.ddex-ingest`), detail-type `DdexDelivery.<Status>`: `started`, `validated`, `rejected` (bad manifest), `failed` (unexpected error, also sent to Sentry). Best-effort — a failure to reach EventBridge is logged but never fails the task. This is observability only — it doesn't drive anything downstream.

## Kafka Result (not yet consumed by anything)

On exit, publishes `{delivery_key, step: "validate", status: "success"|"failed", data, error}` to `KAFKA_DDEX_DELIVERY_EVENTS_TOPIC` (default `ufb.ddex_delivery_events`), mirroring how `ufb-audio-processing`/`ufb-track-meta` report results for `TrackIngestionSagaWorkflow` to react to via `ufb-temporal-worker`'s `reply_listener.py`.

**This currently goes nowhere**: `ufb-temporal-worker` has no DDEX equivalent of `kafka_trigger.py` (nothing starts a workflow when a delivery lands on `ufb.ddex_deliveries`) or `reply_listener.py` (nothing consumes this topic to signal one). Building that workflow/trigger/listener is a separate, necessary piece of work before this pipeline does anything end-to-end — see `ufb-iac`'s DDEX tickets.

## Environment Variables

| Variable | Description | Default |
| --- | --- | --- |
| `S3_BUCKET` | S3 bucket name (required) | — |
| `S3_KEY` | S3 object key of the manifest (required) | — |
| `EVENTBRIDGE_BUS_NAME` | EventBridge bus to publish progress events to | `production-ufb` |
| `SENTRY_DSN` | Sentry DSN for error reporting. If unset, Sentry is not initialized. | — |
| `SENTRY_ENVIRONMENT` | Environment tag sent to Sentry. | `production` |

## Local Development

### Prerequisites

- Python 3.12
- [Pipenv](https://pipenv.pypa.io/)

### Setup

```sh
pipenv install --dev
```

### Running Tests

```sh
pipenv run pytest
```

## Docker

```sh
docker build -t ufb-ddex-ingest .
docker run --rm \
  -e S3_BUCKET=production-ufb-ddex-delivery \
  -e S3_KEY=partners/example/delivery.xml \
  -e AWS_DEFAULT_REGION=eu-west-2 \
  -e AWS_ACCESS_KEY_ID \
  -e AWS_SECRET_ACCESS_KEY \
  -e AWS_SESSION_TOKEN \
  ufb-ddex-ingest
```

## CI/CD

CircleCI builds and pushes the Docker image to AWS ECR on every push. See `.circleci/config.yml`.
