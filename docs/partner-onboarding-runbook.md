# DDEX delivery partner onboarding runbook

How to onboard a new distributor who will deliver releases to UFB as DDEX ERN messages. Read the whole thing once before starting; the order matters.

Anything marked **Manual / not yet automated** is a gap in the pipeline today, not a step you can skip.

## How a delivery flows (for context)

1. The partner uploads a delivery to the deliveries bucket under `partners/<dpid>/...`.
2. An S3 event for any `.xml` object under `partners/` goes to SQS, then Kafka (`ufb.ddex_deliveries`).
3. `ufb-temporal-worker` starts one `DdexDeliveryWorkflow` per manifest key.
4. The workflow runs the `ufb-ddex-ingest` ECS task. It validates the manifest against the XSD, verifies every resource checksum, maps the release, and tags the delivery objects `ddex-processed=true`.
5. The workflow creates the catalog entry (a draft release, an in-place update, or a takedown) through the backend, and starts audio ingestion per track.

Resource names, bucket and queue, are defined in `ufb-iac` under `modules/ddex-delivery`.

## What UFB supports today

| | Supported | Notes |
|---|---|---|
| ERN 4.3 | Validated, checksummed, **mapped to the catalog** | Preferred version. |
| ERN 3.8.2 | Validated and checksummed only | **No catalog entry is created.** The delivery succeeds but is skipped at the mapping step. Do not onboard a partner on 3.8.2 until 3.8.2 mapping exists. |
| New release | `NewReleaseMessage`, creates a **draft** release | A release is never published automatically. |
| Update | A `NewReleaseMessage` matching an existing release by catalog number or GRid | There is no separate Update message type. Updates a release in place and keeps its published status. |
| Takedown | `PurgeReleaseMessage` | Unpublishes (sets to draft), does not delete. |
| Deals and territories | Not mapped | Availability terms in a delivery are ignored today. |

## 1. Before you contact the partner

- Confirm UFB holds a DDEX **Implementation Licence** and a UFB DPID (apply through the DPID portal at ddex.net). The vendored XSDs are covered only by the Evaluation Licence; production use needs the Implementation Licence.
- Decide who owns the relationship and who gets paged for this partner's failed deliveries.

## 2. Exchange identifiers and expectations

Get these from the partner and record them somewhere durable:

- [ ] The partner's **DPID** (`MessageSender/PartyId`). This is the `<dpid>` in their bucket prefix.
- [ ] UFB's DPID, to give to the partner as `MessageRecipient`.
- [ ] The **ERN version** they will send. Confirm 4.3 (see the table above).
- [ ] Whether they send updates and takedowns, and how. Updates must reuse the original release's catalog number or GRid, or UFB will create a second release.
- [ ] How they upload (see step 4) and whether they upload the manifest **last**. The pipeline triggers on the `.xml` manifest, so uploading it before its resources fails checksum verification.
- [ ] **Acknowledgment expectations.** Does the partner expect a receipt message back? UFB emits internal success/failure events but does **not** write a DDEX receipt to the partner (UPF-267). Confirm this is acceptable.

## 3. Agree the delivery layout

Each delivery is a folder under the partner's prefix:

```
partners/<dpid>/<batch-id>/<batch-id>.xml      # manifest, uploaded last
partners/<dpid>/<batch-id>/resources/...       # audio and cover art
```

- Resource paths in the manifest are resolved **relative to the manifest's own folder**. Absolute URLs are skipped, not fetched.
- Only `.xml` objects under `partners/` trigger ingestion. Any other `.xml` uploaded there is treated as a manifest and will be rejected.

## 4. Issue access

**Manual / not yet automated (UPF-268).** No per-partner scoping exists. Until it does, a partner with credentials has bucket-wide access, which is not acceptable for a real partner.

When per-partner credentials are issued, the policy must:

- Allow `s3:PutObject` only on `arn:aws:s3:::<bucket>/partners/<dpid>/*`.
- Allow `kms:GenerateDataKey` and `kms:Encrypt` on the delivery bucket's CMK (`alias/<bucket name>`). The bucket encrypts by default with that key and its key policy only grants the UFB task roles. Without this grant, partner uploads fail with `AccessDenied` even when the S3 permission is correct.
- Grant no read access. Reads of delivery objects are denied outside the VPC by design.

Uploads must use TLS. Plain HTTP is denied by the bucket policy.

## 5. Pre-flight checklist (before the first real delivery)

- [ ] Partner DPID, ERN version, and layout confirmed and recorded (steps 2 and 3).
- [ ] Scoped credentials issued and tested by the partner uploading a small file under their own prefix, and **failing** to upload under another prefix.
- [ ] The DLQ alert is live in Grafana: the `ddex-dlq` rule in the `DDEX-Alerts` folder, paging PagerDuty. Check it is not paused.
- [ ] A synthetic delivery of the partner's ERN version has passed end to end (UPF-271) with correct catalog state.
- [ ] Someone knows how to read a failed delivery (see below).

## 6. Going live and watching the first delivery

1. Ask the partner to send one real, low-stakes release.
2. In Temporal, find the workflow for the manifest key (`DdexDeliveryWorkflow`). It should complete within about 10 minutes. It fails with `DdexDeliveryFailed` on a validation failure or timeout.
3. Check the catalog: the release exists as a **draft**, tracks are present, cover art is set.
4. Check the delivery objects in S3 carry `ddex-processed=true`.
5. Review and publish the draft release by hand. Nothing publishes automatically.

## When a delivery fails

| Symptom | Likely cause |
|---|---|
| Workflow fails with a checksum error naming a resource | The resource is missing, corrupted, or the manifest was uploaded before its resources. Ask the partner to redeliver. |
| Workflow fails with a schema validation error | The manifest is not valid for its declared ERN version. Send the partner the error. |
| Workflow fails on an unsupported ERN version | The manifest declares a namespace UFB does not support. |
| Workflow succeeds but no release appears | The delivery was ERN 3.8.2 (mapping skipped), or not a new-release or purge message. |
| A takedown is rejected | No release matches the catalog number or GRid. |
| A message sits in the DLQ | The pipeline failed to process the S3 event three times. This pages. Investigate, then redrive or discard. |

Delivery objects can only be read from inside the VPC, so inspecting one means going through a Teleport-fronted exec into an ECS task, not `aws s3 cp` from a laptop.

A redelivered manifest is safe: the workflow ID is derived from the manifest key and the backend deduplicates by delivery key. To reprocess a failed delivery, the partner re-uploads the manifest (or you re-trigger it).
