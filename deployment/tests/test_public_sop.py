import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from deployment import change_sets, deploy_public_sop
from deployment.build_public_sop import COLLECTION, COLLECTION_PATH, validate
from deployment.deploy_public_sop import preflight
from deployment.deployment_target import Target

REPOSITORY = Path(__file__).resolve().parents[2]
OPERATOR = "arn:aws:iam::123456789012:role/fixture-deployer"
# An operator's acknowledgment, in the form --acknowledge takes (the same text as test_workspace_deployment.py's).
ACKNOWLEDGED = "training-backend/InfrastructureStack/APIKeysSecret=AWS::SecretsManager::Secret:SecretString"


def test_reviewed_collection_is_reproducible_and_checklists_are_complete():
    files = validate()
    assert len(files) == 20
    orders = files["documents/nps-ten-standard-firefighting-orders.txt"].decode()
    watch = files["documents/nps-eighteen-watch-out-situations.txt"].decode()
    assert all(f"\n{i}. " in orders for i in range(1, 11))
    assert all(f"\n{i}. " in watch for i in range(1, 19))
    for path, data in files.items():
        if path.endswith(".json"):
            metadata = json.loads(data)["metadataAttributes"]
            assert metadata["classification"] == "public"
            assert metadata["source_url"].startswith("https://www.")


def test_collection_rejects_tampering_and_unreviewed_documents(tmp_path):
    root = tmp_path / "collection"
    shutil.copytree(COLLECTION, root)
    path = root / "documents/nps-ten-standard-firefighting-orders.txt"
    path.write_text("Unreviewed replacement")
    with pytest.raises(ValueError, match="drift"):
        validate(root)
    shutil.copyfile(COLLECTION / path.relative_to(root), path)
    (root / "documents/extra.txt").write_text("Unreviewed source")
    with pytest.raises(ValueError, match="Unreviewed"):
        validate(root)


def test_public_preflight_checks_identity_before_resource_access():
    session = MagicMock()
    session.client.return_value.get_caller_identity.return_value = {"Account": "999999999999"}
    with pytest.raises(ValueError, match="does not match"):
        preflight(session, Target("123456789012", "us-west-2", "test"), root=REPOSITORY)
    assert [c.args[0] for c in session.client.call_args_list] == ["sts"]


def test_standalone_public_sop_command_is_retired_in_favor_of_deploy_all():
    with pytest.raises(SystemExit) as stopped:
        deploy_public_sop.main(["--profile", "fixture", "--apply"])
    assert str(stopped.value) == deploy_public_sop.RETIRED
    assert 'public_guidance = "managed"' in deploy_public_sop.RETIRED and "deploy-all.sh --apply" in deploy_public_sop.RETIRED


def test_public_guidance_stack_deploys_through_the_change_set_guard(tmp_path, monkeypatch):
    shutil.copytree(COLLECTION, tmp_path / COLLECTION_PATH)
    session = MagicMock()
    commands = []

    class Guarded(Exception):
        pass

    # N1 (C6): the operator's acknowledgment reaches the public-guidance change set unchanged.
    acknowledgments = (change_sets.parse_acknowledgment(ACKNOWLEDGED),)

    def guard(cfn, stack, change_set, *, created, receipt, acknowledged):
        assert stack.endswith("-public-guidance") and created and receipt.parent == tmp_path / "receipts"
        assert acknowledged == acknowledgments
        raise Guarded(change_set)
    monkeypatch.setattr(deploy_public_sop, "inspect_stack", lambda *_: None)
    monkeypatch.setattr(change_sets, "execute_guarded", guard)
    with pytest.raises(Guarded) as guarded:
        deploy_public_sop.deploy_public_sop(session, Target("123456789012", "us-west-2", "test"),
                                            tmp_path / "receipts", root=tmp_path, run=commands.append,
                                            parameters={"OperatorRoleArns": OPERATOR}, install=False,
                                            acknowledged=acknowledgments)
    synth, deploy = commands
    assert "synth" in synth and "includePublicGuidance=true" in synth
    assert deploy[deploy.index("--method") + 1] == "prepare-change-set"
    # The Documents bucket policy admits the installation file's operator roles (L1, D52).
    assert deploy[deploy.index("--parameters") + 1] == f"OperatorRoleArns={OPERATOR}"
    assert deploy[deploy.index("--change-set-name") + 1] == str(guarded.value)
    # deploy_workspace.preflight already checked this session's account and the collection before any AWS write.
    assert "sts" not in [call.args[0] for call in session.client.call_args_list]


def test_public_preflight_validates_the_collection_of_the_given_root(tmp_path):
    shutil.copytree(COLLECTION, tmp_path / COLLECTION_PATH)
    (tmp_path / COLLECTION_PATH / "documents/extra.txt").write_text("Unreviewed source")
    session = MagicMock()
    session.client.return_value.get_caller_identity.return_value = {"Account": "123456789012"}
    with pytest.raises(ValueError, match="Unreviewed"):
        preflight(session, Target("123456789012", "us-west-2", "test"), root=tmp_path)
    session.client.return_value.describe_stacks.assert_not_called()
