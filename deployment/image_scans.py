"""Release gate for completed, paginated exact-digest ECR basic scans."""
import json
import time
from datetime import UTC, date, datetime, timedelta

from botocore.exceptions import ClientError

from deployment import ROOT

# The checked-in exception policy, relative to the repository root; each release captures it with its sources.
POLICY = "deployment/image-scan-policy.json"


def load_policy(path):
    """Read an image-scan exception policy, require its owner and review, and parse its expiry date."""
    policy = json.loads(path.read_text())
    if not policy.get("owner") or not policy.get("review"):
        raise ValueError("Image policy needs an owner and review")
    return policy, date.fromisoformat(policy["expires"])


def exceptions_apply(expires):
    """Return whether policy exceptions still apply today (UTC): only before the expiry date, not on it."""
    return datetime.now(UTC).date() < expires


def refresh_image_scan(client, repository, digest):
    """Request a fresh exact-digest scan for reuse; ECR's daily limit requires an already recent completed scan."""
    requested_at = datetime.now(UTC).timestamp()
    try:
        client.start_image_scan(repositoryName=repository, imageId={"imageDigest": digest})
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in {"LimitExceededException", "ScanFrequencyExceededException"}:
            raise
        page = client.describe_image_scan_findings(
            repositoryName=repository, imageId={"imageDigest": digest}, maxResults=1000)
        completed = page.get("imageScanFindings", {}).get("imageScanCompletedAt")
        if (page.get("imageId", {}).get("imageDigest") != digest
                or page.get("imageScanStatus", {}).get("status") != "COMPLETE"
                or not isinstance(completed, datetime) or completed.tzinfo is None
                or completed < datetime.now(UTC) - timedelta(days=1)):
            raise RuntimeError("Reusable image has no completed scan within ECR's daily scan window") from None
        return completed.timestamp()
    return requested_at


def verify_image(client, repository, digest, component, receipt, *, timeout_seconds=600, policy_path=None,
                 scan_not_before=None):
    """Reject new/changed severe findings; record narrowly reviewed temporary dispositions."""
    policy, expires = load_policy(policy_path or ROOT / POLICY)
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            page = client.describe_image_scan_findings(
                repositoryName=repository, imageId={"imageDigest": digest}, maxResults=1000)
            status = page["imageScanStatus"]["status"]
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ScanNotFoundException":
                raise
            status = "IN_PROGRESS"
        if status == "COMPLETE":
            completed = page.get("imageScanFindings", {}).get("imageScanCompletedAt")
            if scan_not_before is None or (
                    isinstance(completed, datetime) and completed.tzinfo is not None
                    and completed.timestamp() >= scan_not_before):
                break
            status = "IN_PROGRESS"
        if status != "IN_PROGRESS" or time.monotonic() >= deadline:
            raise RuntimeError(f"Image scan is not complete: {component} ({status})")
        time.sleep(5)
    if page["imageId"]["imageDigest"] != digest:
        raise ValueError("Image scan digest differs from the release")
    findings = list(page["imageScanFindings"].get("findings", []))
    token = page.get("nextToken")
    while token:
        following = client.describe_image_scan_findings(
            repositoryName=repository, imageId={"imageDigest": digest}, maxResults=1000, nextToken=token)
        if following["imageId"]["imageDigest"] != digest or following["imageScanStatus"]["status"] != "COMPLETE":
            raise ValueError("Image scan changed during pagination")
        findings.extend(following["imageScanFindings"].get("findings", []))
        token = following.get("nextToken")
    severe = [f for f in findings if f["severity"] in ("CRITICAL", "HIGH")]
    counts = page["imageScanFindings"].get("findingSeverityCounts", {})
    if any(sum(f["severity"] == severity for f in severe) != counts.get(severity, 0)
           for severity in ("CRITICAL", "HIGH")):
        raise ValueError("Image scan severity counts differ from its complete findings")
    accepted, blocked = [], []
    for finding in severe:
        attributes = {a["key"]: a["value"] for a in finding.get("attributes", [])}
        exception = next((e for e in policy["exceptions"] if (
            component in e["components"] and finding["name"] in e["findings"]
            and finding["severity"] == e["severity"]
            and attributes.get("package_name") == e["package"]
            and attributes.get("package_version") == e["version"]
            and e.get("reason") and exceptions_apply(expires))), None)
        identity = {"finding": finding["name"], "severity": finding["severity"], **attributes}
        (accepted if exception else blocked).append(
            {**identity, "reason": exception["reason"]} if exception else identity)
    result = {"component": component, "digest": digest, "scan": page, "findings": findings,
              "exceptions": accepted, "blocked": blocked, "exception_expiry": policy["expires"],
              "status": "failed" if blocked else "passed"}
    receipt.write_text(json.dumps(result, indent=2, default=str))
    if blocked:
        raise RuntimeError(f"Image scan blocked {component}: {len(blocked)} unapproved Critical/High finding(s)")
    return result
