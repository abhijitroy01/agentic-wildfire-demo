"""Provision and ingest the reviewed public SOP collection for deploy_workspace.py (deploy-all.sh)."""
import json
import time
from datetime import UTC, datetime

from deployment import NPM_CI
from deployment.build_public_sop import COLLECTION_ID, COLLECTION_PATH, sha256, validate
from deployment.change_sets import deploy_guarded
from deployment.deployment_target import inspect_stack, safe_outputs

# The standalone command left the collection unbound; deploy-all.sh provisions and binds it in one release.
RETIRED = ("deployment/deploy_public_sop.py no longer runs on its own. Set [knowledge] public_guidance = "
           '"managed" in deployment/installation.toml and run bash deployment/deploy-all.sh --apply, which '
           "provisions, ingests, verifies and binds the collection (see deployment/README.md).")


def preflight(session, target, *, root):
    """Read-only public guidance preflight: account, root's reviewed collection, stack ownership, model access."""
    if session.client("sts").get_caller_identity()["Account"] != target.account:
        raise ValueError("Profile account does not match [aws] account_id; no changes made")
    validate(root / COLLECTION_PATH)
    existing = inspect_stack(session.client("cloudformation"), target.stack("public-guidance"), target)
    session.client("bedrock").get_foundation_model(modelIdentifier="amazon.titan-embed-text-v2:0")
    session.client("s3vectors").list_vector_buckets(maxResults=1)
    return existing


def deploy_public_sop(session, target, receipt_dir, *, root, run, parameters, install=True, acknowledged=()):
    """Deploy the retained public guidance stack from root's CDK app and ingest it; never touches a private KB.

    deploy_workspace.preflight already ran this module's preflight for the same session before any AWS write. run
    executes one npm or cdk argument list in root's CDK app with the release environment. parameters are the stack's
    parameters (the operator roles, D52). acknowledged reaches the change-set guard.
    """
    collection = root / COLLECTION_PATH
    files = validate(collection)
    receipt_dir.mkdir(parents=True, exist_ok=True)
    if install:
        run(NPM_CI)
    cdk = root / "deployment/infra/node_modules/.bin/cdk"
    assembly = (receipt_dir / "public-guidance.cdk.out").resolve()
    run([cdk, "-c", f"deploymentName={target.name}", "-c", "includePublicGuidance=true",
         "synth", "WildfireLabPublicGuidance", "--quiet", "--output", assembly])
    cfn = session.client("cloudformation")
    stack = target.stack("public-guidance")
    deploy_guarded(cfn, stack, f"public-guidance-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}", run=run,
                   cdk=[cdk, "--app", assembly], construct="WildfireLabPublicGuidance",
                   created=inspect_stack(cfn, stack, target) is None,
                   receipt=receipt_dir / "public-guidance-changes.json", parameters=parameters,
                   acknowledged=acknowledged)
    outputs = safe_outputs(inspect_stack(cfn, stack, target))
    s3, bedrock = session.client("s3"), session.client("bedrock-agent")
    bucket = outputs["DocumentsBucket"]
    # Refuse stale/unreviewed objects instead of silently deleting data or indexing it.
    existing = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="documents/"):
        existing.update(item["Key"] for item in page.get("Contents", []))
    if existing - files.keys():
        raise ValueError("Public source prefix contains objects outside the reviewed manifest; review before ingestion")
    versions = {}
    for key, data in files.items():
        response = s3.put_object(Bucket=bucket, Key=key, Body=data,
                                 ContentType="application/json" if key.endswith(".json") else "text/plain; charset=utf-8")
        versions[key] = {"version_id": response["VersionId"], "sha256": sha256(data)}
    for name in ("sources.json", "collection.json"):
        data = (collection / name).read_bytes()
        s3.put_object(Bucket=bucket, Key=f"manifests/{sha256(data)}/{name}", Body=data, ContentType="application/json")
    kb_id, ds_id = outputs["PublicKnowledgeBaseId"], outputs["PublicDataSourceId"]
    job = bedrock.start_ingestion_job(
        knowledgeBaseId=kb_id, dataSourceId=ds_id, description="Reviewed official public guidance"
    )["ingestionJob"]
    receipt = {"account": target.account, "region": target.region, "name": target.name,
               "collection": COLLECTION_ID, "outputs": outputs, "source_versions": versions,
               "ingestion_job_id": job["ingestionJobId"], "status": "ingesting"}
    path = receipt_dir / "public-sop-deployment.json"
    path.write_text(json.dumps(receipt, indent=2)+"\n")
    deadline = time.monotonic()+1200
    while job["status"] not in {"COMPLETE", "FAILED", "STOPPED"}:
        if time.monotonic() > deadline:
            raise TimeoutError(f"Ingestion still running; resume verification using {path}")
        print("Public SOP ingestion:", job["status"], flush=True)
        time.sleep(10)
        job = bedrock.get_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id,
                                       ingestionJobId=job["ingestionJobId"])["ingestionJob"]
    receipt["ingestion"] = job
    receipt["status"] = job["status"]
    path.write_text(json.dumps(receipt, indent=2, default=str)+"\n")
    if job["status"] != "COMPLETE" or job.get("statistics", {}).get("numberOfDocumentsFailed", 0):
        raise RuntimeError(f"Public guidance ingestion did not complete cleanly; inspect {path}")
    checks = []
    for query, required in [
        ("What are the ten standard firefighting orders for crew communications and escape routes?", "nps-firefighter-safety"),
        ("How should a household prepare evacuation routes and transportation before evacuating?", "fema-evacuation"),
        ("When may residents return home after a wildfire?", "fema-wildfires"),
    ]:
        result = session.client("bedrock-agent-runtime").retrieve(
            knowledgeBaseId=kb_id, retrievalQuery={"text": query},
            retrievalConfiguration={"vectorSearchConfiguration": {
                "numberOfResults": 3, "filter": {"equals": {"key": "collection", "value": COLLECTION_ID}},
            }},
        )["retrievalResults"]
        if not result or required not in {r.get("metadata", {}).get("source_id") for r in result}:
            raise RuntimeError(f"Public retrieval relevance check failed: {query}")
        checks.append({"query": query, "results": [
            {"source_id": r["metadata"]["source_id"], "title": r["metadata"]["title"],
             "source_url": r["metadata"]["source_url"], "score": r["score"]} for r in result
        ]})
    receipt.update(status="verified", retrieval_checks=checks)
    path.write_text(json.dumps(receipt, indent=2, default=str)+"\n")
    print(f"Public SOP collection verified: {len(files)//2} sections, KB {kb_id}", flush=True)
    return outputs


def main(argv=None):
    """Retired standalone entry point: deploy-all.sh provisions, ingests, verifies and binds the collection."""
    raise SystemExit(RETIRED)


if __name__ == "__main__":
    main()
