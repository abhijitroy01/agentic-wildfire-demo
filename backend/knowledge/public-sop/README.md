# Reviewed public guidance collection

This collection supports the public FireGUard SOP/reference experience. It contains official public guidance, not adopted local SOPs, real incident orders, or validated evacuation instructions.

The source register is `sources.json`; `collection.json` hashes every text and metadata artifact. `snapshots/` preserves the retrieved official HTML. `documents/` contains ten complete reviewed text sections and Bedrock S3 metadata sidecars. Only `documents/` is ingested. No private KB documents, user incident records, copyrighted third-party images, recordings or generated summaries are included in retrieval.

From the repository root:

```sh
.venv/bin/python -m deployment.build_public_sop --check
```

Rebuild with `.venv/bin/python -m deployment.build_public_sop` after an explicit source review. Source refresh is intentionally not a crawler or automatic ingestion task: retrieve the official page, review publication changes and applicability, update its pinned snapshot/hash and section boundaries, rebuild, inspect all affected text, run checks, then deploy. Do not edit the generated text alone. Removed section IDs require a reviewed index/source migration; the deployer refuses unexpected existing objects rather than deleting them silently.

See the [public SOP collection guide](../../../architecture/28-public-sop-collection.md) for source coverage, deployment, metadata, retrieval behavior and maintenance.
