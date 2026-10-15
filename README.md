# UFB DDEX Ingest

Validates DDEX ERN delivery manifests landed in S3 against the official DDEX XSD schema for their declared version, then verifies every resource file's checksum, as the first stages of the DDEX delivery pipeline (see `ufb-iac`'s `modules/ddex-delivery`).

This covers **XSD validation, checksum verification, and ERN→catalog field mapping**. This task does **not** call the backend itself - it maps the release and includes it in its Kafka result; `DdexDeliveryWorkflow` in `ufb-temporal-worker` is what actually creates the draft Release/Track/Artist/Label, via a `create_catalog_entry` activity (mirrors `report_audio_success`/`report_metadata_success` for the audio pipeline - the leaf task reports its result, the workflow owns the backend write, which gets Temporal's retry policy for free). Delivery-type (new/update/takedown) handling and acknowledgment emission are separate, not-yet-built stages of the same pipeline.

## ERN → Catalog Mapping (`mapping.py`)

Maps a validated manifest's `PartyList`/`ReleaseList`/`ResourceList` into the fields UFB's catalog (`ufb/backend/upfrontbeats/models/`) actually has: release title/artists/label/genre/release_date/catalog_number, and per-track title/artists/isrc/duration.

**ERN 4.3 only** - 3.8.2's equivalent shapes differ enough to need their own pass, and aren't done yet. **Deliberately not covered**, because nothing in the Django schema models them (or it's a design decision beyond "map the field that exists"): deals/territories, parental warnings, version types, compilation flags, and true multi-disc `ResourceGroup` sequencing - track order falls back to each `SoundRecording`'s position in `ResourceList`'s document order, correct for the common single-disc case but not a real read of `ResourceGroup`.

The label is only ever communicated as a reference into `PartyList` (`ReleaseLabelReference` → `Party/PartyReference` → `PartyName/FullName`), never as a plain string - there's no simpler path to skip.

Mapping is skipped (not an error) for a `PurgeReleaseMessage` (takedown - nothing to create a draft release from) or any ERN version other than 4.3 - `task.py`'s Kafka result carries `mapped_release: null` in both cases, and `DdexDeliveryWorkflow` just doesn't create a catalog entry for that delivery.

## How It Works

1. Downloads the manifest from S3 (`S3_BUCKET` / `S3_KEY`)
2. Reads the root element's namespace to detect the declared ERN version
3. Validates the manifest against the matching vendored XSD (`schemas/ern-43` or `schemas/ern-382`)
4. Verifies every resource file referenced in the manifest (`checksum.py`) against its declared checksum, resolving each one relative to the manifest's own S3 "directory" — handles both ERN 4.3's `File/URI` + `HashSum/Algorithm`+`HashSumValue` shape and 3.8.2's `File/FileName`+`FilePath` + `HashSum/HashSum`+`HashSumAlgorithmType` shape (note: 3.8.2 reuses `HashSum` as both the wrapper and inner value element name)
5. Maps the manifest to catalog fields (`mapping.py`), if it's an ERN 4.3 `NewReleaseMessage`
6. Publishes a progress event to EventBridge at each stage (`validated`, `checksums_verified`, `release_mapped`/`release_mapping_skipped`, or `rejected`/`failed`)
7. Publishes its final result to Kafka (`success` or `failed`, with the mapped release data), for `DdexDeliveryWorkflow` to act on

An unrecognized ERN namespace, malformed XML, a schema validation failure, a missing resource, or a checksum mismatch all reject the delivery with a specific error (naming every failing resource, not just the first) rather than silently continuing. A `File/URI` that's an absolute URL (points outside this delivery) is skipped rather than guessed at.

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
