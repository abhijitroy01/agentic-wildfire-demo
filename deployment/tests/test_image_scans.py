"""Release image policy rejects incomplete, changed, new and expired severe findings.

Preflight refuses expired exceptions before any build, from the same day the release gate stops applying them.
"""
import json
from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from deployment import ROOT
from deployment.deploy_workspace import require_unexpired_image_exceptions
from deployment.image_scans import POLICY, refresh_image_scan, verify_image


def finding(name="CVE-fixture", version="1"):
    return {"name": name, "severity": "HIGH", "attributes": [
        {"key": "package_name", "value": "fixture"}, {"key": "package_version", "value": version}]}


def page(findings, **extra):
    return {"imageId": {"imageDigest": "digest"}, "imageScanStatus": {"status": "COMPLETE"},
            "imageScanFindings": {"findings": findings, "findingSeverityCounts": {"HIGH": 2}}, **extra}


def write_policy(root, expires, exceptions=True):
    """Write root's image-scan policy: its expiry date, and one exception for both fixture findings or none."""
    exception = {"components": ["api"], "package": "fixture", "version": "1", "severity": "HIGH",
                 "findings": ["CVE-fixture", "CVE-second"], "reason": "test boundary"}
    path = root / POLICY
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"owner": "test", "review": "architecture/36-public-demo-security.md",
                                "expires": expires, "exceptions": [exception] if exceptions else []}))
    return path


class NoonUTC(datetime):
    """The image gate's clock, fixed at noon UTC on DAY so a date-boundary test never races midnight."""
    DAY = date(2026, 10, 14)

    @classmethod
    def now(cls, tz=None):
        return datetime(cls.DAY.year, cls.DAY.month, cls.DAY.day, 12, tzinfo=tz)


@pytest.mark.parametrize("change", [None, "new-cve", "new-version", "expired", "count", "digest", "incomplete"])
def test_exact_digest_complete_scan_requires_explicit_unexpired_dispositions(tmp_path, change):
    policy = write_policy(tmp_path, "2000-01-01" if change == "expired" else "2099-01-01")
    first = page([finding()], nextToken="next")
    second = page([finding("CVE-new" if change == "new-cve" else "CVE-second",
                           "2" if change == "new-version" else "1")])
    if change == "count":
        first["imageScanFindings"]["findingSeverityCounts"]["HIGH"] = 3
    if change == "digest":
        second["imageId"]["imageDigest"] = "another-digest"
    if change == "incomplete":
        first["imageScanStatus"]["status"] = "FAILED"
    client = MagicMock()
    client.describe_image_scan_findings.side_effect = [first, second]
    receipt = tmp_path / "scan.json"
    if change:
        with pytest.raises((ValueError, RuntimeError)):
            verify_image(client, "repo", "digest", "api", receipt, policy_path=policy)
    else:
        result = verify_image(client, "repo", "digest", "api", receipt, policy_path=policy)
        assert result["status"] == "passed" and len(result["exceptions"]) == 2 and not result["blocked"]
        assert client.describe_image_scan_findings.call_args.kwargs["nextToken"] == "next"
        assert json.loads(receipt.read_text())["digest"] == "digest"


def test_scan_on_push_start_delay_is_bounded(tmp_path, monkeypatch):
    from deployment import image_scans
    client = MagicMock()
    client.describe_image_scan_findings.side_effect = ClientError(
        {"Error": {"Code": "ScanNotFoundException"}}, "DescribeImageScanFindings")
    monkeypatch.setattr(image_scans.time, "sleep", lambda _: pytest.fail("Must not poll after its deadline"))
    with pytest.raises(RuntimeError, match="not complete"):
        verify_image(client, "repo", "digest", "api", tmp_path / "scan.json", timeout_seconds=0)


def test_reused_digest_waits_for_the_new_scan_instead_of_an_old_complete_result(tmp_path, monkeypatch):
    from deployment import image_scans
    now = datetime.now(UTC)
    old, fresh = page([finding(), finding("CVE-second")]), page([finding(), finding("CVE-second")])
    old["imageScanFindings"]["imageScanCompletedAt"] = now - timedelta(hours=3)
    fresh["imageScanFindings"]["imageScanCompletedAt"] = now + timedelta(seconds=1)
    client = MagicMock()
    client.describe_image_scan_findings.side_effect = [old, fresh]
    sleeps = []
    monkeypatch.setattr(image_scans.time, "sleep", sleeps.append)
    result = verify_image(client, "repo", "digest", "api", tmp_path / "scan.json",
                          policy_path=write_policy(tmp_path, "2099-01-01"), scan_not_before=now.timestamp())
    assert result["status"] == "passed" and sleeps == [5]


@pytest.mark.parametrize("age,status,digest", [(2, "COMPLETE", "digest"), (25, "COMPLETE", "digest"),
                                             (2, "IN_PROGRESS", "digest"), (2, "COMPLETE", "other")])
def test_ecr_daily_scan_limit_accepts_only_a_recent_complete_exact_digest(age, status, digest):
    completed = datetime.now(UTC) - timedelta(hours=age)
    client = MagicMock()
    client.start_image_scan.side_effect = ClientError(
        {"Error": {"Code": "LimitExceededException"}}, "StartImageScan")
    result = page([])
    result["imageScanFindings"]["imageScanCompletedAt"] = completed
    result["imageScanStatus"]["status"] = status
    result["imageId"]["imageDigest"] = digest
    client.describe_image_scan_findings.return_value = result
    if age <= 24 and status == "COMPLETE" and digest == "digest":
        assert refresh_image_scan(client, "repo", "digest") == completed.timestamp()
    else:
        with pytest.raises(RuntimeError, match="daily scan window"):
            refresh_image_scan(client, "repo", "digest")


def test_preflight_refuses_expired_exceptions_naming_the_policy_expiry_and_review(tmp_path):
    write_policy(tmp_path, "2000-01-01")
    with pytest.raises(ValueError, match="expired") as refused:
        require_unexpired_image_exceptions(tmp_path)
    for named in (POLICY, "2000-01-01", "Exception re-review", "architecture/36-public-demo-security.md"):
        assert named in str(refused.value)


def test_preflight_accepts_unexpired_exceptions(tmp_path):
    write_policy(tmp_path, "2099-01-01")
    require_unexpired_image_exceptions(tmp_path)


def test_preflight_accepts_a_past_expiry_without_exceptions(tmp_path):
    write_policy(tmp_path, "2000-01-01", exceptions=False)
    require_unexpired_image_exceptions(tmp_path)


@pytest.mark.parametrize("days_left", [0, 1])
def test_preflight_expires_on_the_same_day_as_the_image_gate(tmp_path, monkeypatch, days_left):
    # image_scans applies an exception only while datetime.now(UTC).date() < expires, so the expiry date is
    # exclusive: the day before it is the last UTC day the exceptions excuse a finding, and on the expiry day
    # itself the gate blocks every finding they cover. The preflight must refuse from that same day on.
    monkeypatch.setattr("deployment.image_scans.datetime", NoonUTC)
    policy = write_policy(tmp_path, (NoonUTC.DAY + timedelta(days=days_left)).isoformat())
    client = MagicMock()
    client.describe_image_scan_findings.return_value = page([finding(), finding("CVE-second")])
    receipt = tmp_path / "scan.json"
    if days_left:
        assert verify_image(client, "repo", "digest", "api", receipt, policy_path=policy)["status"] == "passed"
        require_unexpired_image_exceptions(tmp_path)
    else:
        with pytest.raises(RuntimeError, match="blocked"):
            verify_image(client, "repo", "digest", "api", receipt, policy_path=policy)
        with pytest.raises(ValueError, match="expired"):
            require_unexpired_image_exceptions(tmp_path)


@pytest.mark.parametrize("change", [None, "new-cve", "new-version"])
def test_fire_risk_scope_preserves_exact_reviewed_findings(tmp_path, monkeypatch, change):
    """The new component uses only existing precise dispositions, never a blanket native-library exemption."""
    monkeypatch.setattr("deployment.image_scans.datetime", NoonUTC)
    policy = json.loads((ROOT / POLICY).read_text())
    findings = []
    for exception in policy["exceptions"]:
        assert "fire-risk" in exception["components"]
        for name in exception["findings"]:
            findings.append({"name": name, "severity": exception["severity"], "attributes": [
                {"key": "package_name", "value": exception["package"]},
                {"key": "package_version", "value": exception["version"]},
            ]})
    if change == "new-cve":
        findings[0]["name"] = "CVE-unreviewed"
    elif change == "new-version":
        findings[0]["attributes"][1]["value"] = "unreviewed-version"
    scanned = page(findings)
    scanned["imageScanFindings"]["findingSeverityCounts"] = {"HIGH": len(findings)}
    client = MagicMock()
    client.describe_image_scan_findings.return_value = scanned
    receipt = tmp_path / "scan-fire-risk.json"
    if change:
        with pytest.raises(RuntimeError, match="blocked"):
            verify_image(client, "repo", "digest", "fire-risk", receipt)
    else:
        result = verify_image(client, "repo", "digest", "fire-risk", receipt)
        assert len(result["exceptions"]) == len(findings) and not result["blocked"]
