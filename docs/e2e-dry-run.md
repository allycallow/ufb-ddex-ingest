# End-to-end dry run

Proves the whole DDEX pipeline works before the first real partner is onboarded (UPF-271): bucket, SQS, Kafka, Temporal, ddex-ingest, backend catalog.

`scripts/make_synthetic_delivery.py` builds the deliveries. They are schema-valid ERN 4.3 and pass this repo's validation, checksum and mapping code (see `tests/test_synthetic_delivery.py`). **What has not been tested is everything after that**, which is what this run is for.

This runs against the real pipeline. It creates a **draft** release in the catalog. Use the synthetic catalog numbers below so it is easy to find and remove.

## What you need

- AWS access to the delivery bucket, region `eu-west-2`. The bucket should be `production-ufb-ddex-delivery`, going by the name of its DLQ alert queue. Confirm it in `ufb-iac` (`module.ddex_delivery`) before uploading. Your own credentials need `s3:PutObject` and `kms:GenerateDataKey` on the bucket's key, since there are no partner credentials yet (UPF-268).
- Access to Temporal, and a way to run something inside the VPC (a Teleport-fronted exec into an ECS task). **Delivery objects, including their tags, cannot be read from a laptop.** The bucket policy denies `GetObject` and `GetObjectTagging` outside the VPC endpoint.
- A way to look at the catalog (admin or Retool) and, ideally, Kafka topic access.

## 0. Check the build is deployed

- [ ] The ddex-ingest image containing the `ddex-processed` tagging is deployed (PRs for UPF-265 and the rejected-delivery tagging).
- [ ] The `ufb-iac` lifecycle change is applied.
- [ ] `ufb-temporal-worker` is running with the DDEX trigger and reply listener.
- [ ] The `ddex-dlq` Grafana alert is not paused.

## 1. Build the deliveries

```
python scripts/make_synthetic_delivery.py out --tag syn-new-001 --catalog-number SYN-DRYRUN-001
python scripts/make_synthetic_delivery.py out --tag syn-update-001 --catalog-number SYN-DRYRUN-001
python scripts/make_synthetic_delivery.py out --tag syn-bad-001 --catalog-number SYN-DRYRUN-002 --corrupt
python scripts/make_synthetic_delivery.py out --tag syn-takedown-001 --catalog-number SYN-DRYRUN-001 --takedown
```

Use a test DPID in the prefix, for example `partners/PADPIDA2026000001S/`.

## 2. Upload rule

Upload the `resources/` files first and the `.xml` manifest **last**. Only a `.xml` under `partners/` triggers ingestion.

```
aws s3 cp out/syn-new-001/resources s3://$BUCKET/partners/PADPIDA2026000001S/syn-new-001/resources --recursive
aws s3 cp out/syn-new-001/syn-new-001.xml s3://$BUCKET/partners/PADPIDA2026000001S/syn-new-001/
```

## 3. Run the cases, in this order

### A. New release (`syn-new-001`)

Trace it in order:

1. **SQS and Kafka:** the S3 event reaches `ufb.ddex_deliveries`.
2. **Temporal:** one workflow with ID `ddex-delivery-partners/PADPIDA2026000001S/syn-new-001/syn-new-001.xml`.
3. **ddex-ingest:** an ECS task runs. Its EventBridge events go `started`, `validated`, `checksums_verified`, `release_mapped`.
4. **Kafka reply:** a `success` message on `ufb.ddex_delivery_events` carries `mapped_delivery`.
5. **Temporal:** the workflow runs `create_catalog_entry`, then starts one `track-ingestion-<track_id>` workflow per track (two).
6. **Catalog:** a **draft** release `Synthetic Release syn-new-001`, catalog number `SYN-DRYRUN-001`, two tracks (ISRCs `GBSYN2600001`, `GBSYN2600002`), cover art set.
7. **Audio:** both tracks finish encoding and metadata analysis.
8. **S3 (from inside the VPC):** the manifest and all three resources carry `ddex-processed=true`.

Pass if all eight hold with no manual intervention.

### B. Duplicate delivery

Re-upload the same manifest. Expected: no second workflow while the first is running, and no duplicate release or tracks afterward.

### C. Update (`syn-update-001`)

Same catalog number, different tag. Expected: the existing release is updated in place, tracks matched by ISRC keep their IDs, and the release stays a draft (or stays published if you published it first).

### D. Rejected delivery (`syn-bad-001`)

Expected: ddex-ingest emits `rejected` naming `A1.wav`; the workflow fails with `DdexDeliveryFailed`; **no catalog entry**; the manifest and the resources carry `ddex-processed=false`. This does **not** page, because only the DLQ pages (see the runbook).

### E. Takedown (`syn-takedown-001`)

Publish release `SYN-DRYRUN-001` first if you want to see it unpublished. Expected: the release is set back to draft, not deleted.

### F. DLQ alert (optional)

The DLQ alert needs a message the pipeline fails to process three times, which is hard to cause safely. Check the rule exists and is not paused instead.

## 4. What is not covered

- **The receipt to the partner.** None is sent (UPF-267).
- **ERN 3.8.2.** It validates but does not map (UPF-264).
- **Lifecycle expiry and archival.** Confirming objects actually move to Deep Archive or expire takes days. Check the lifecycle rules in the console, and look again after the retention window.
- **Real partner behavior.** A real distributor's ERN will differ from this synthetic one.

## 5. Clean up

- Delete the `SYN-DRYRUN-*` draft releases and their tracks from the catalog.
- Delete the `partners/PADPIDA2026000001S/` test prefix, or leave it to expire: the `false`-tagged objects expire on their own, the `true`-tagged ones are archived to Deep Archive and kept.
