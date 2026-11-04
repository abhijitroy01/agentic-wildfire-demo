"""Shared infrastructure baseline gate: safe CloudFormation YAML parsing and a failing exit status by default."""
import pytest
from yaml.constructor import ConstructorError

from deployment.validate_infra_baseline import DEFAULT_TEMPLATES, load_template, main

# One finding: an ECR repository whose release tags can be overwritten.
MUTABLE_REPOSITORY = "Resources:\n  Repository:\n    Type: AWS::ECR::Repository\n    Properties: {}\n"


def test_intrinsic_tags_parse_as_one_key_mappings():
    template = load_template(
        "Resources:\n"
        "  Repository:\n"
        "    Type: AWS::ECR::Repository\n"
        "    Properties:\n"
        "      RepositoryName: !Sub '${AWS::StackName}-runtime'\n"
        "      Arn: !GetAtt Role.Arn\n"
        "      Tags: !If [HasTags, [!Ref TagValue], !Ref AWS::NoValue]\n")
    properties = template["Resources"]["Repository"]["Properties"]
    assert properties["RepositoryName"] == {"Sub": "${AWS::StackName}-runtime"}
    assert properties["Arn"] == {"GetAtt": "Role.Arn"}
    assert properties["Tags"] == {"If": ["HasTags", [{"Ref": "TagValue"}], {"Ref": "AWS::NoValue"}]}


@pytest.mark.parametrize("payload", [
    "value: !!python/object/apply:builtins.len [[1, 2]]\n",
    "value: !!python/name:builtins.len\n",
    "value: !!python/object:builtins.object {}\n",
])
def test_python_object_tags_are_rejected(payload):
    with pytest.raises(ConstructorError, match="could not determine a constructor"):
        load_template(payload)


@pytest.mark.parametrize("flags,status", [([], 1), (["--strict"], 1), (["--report-only"], 0)])
def test_findings_fail_the_gate_unless_report_only(tmp_path, capsys, flags, status):
    template = tmp_path / "template.yaml"
    template.write_text(MUTABLE_REPOSITORY)
    assert main([*flags, str(template)]) == status
    assert f"- {template} :: Repository :: ecr-tag-immutability" in capsys.readouterr().out


def test_strict_and_report_only_cannot_be_combined(capsys):
    with pytest.raises(SystemExit) as stopped:
        main(["--strict", "--report-only"])
    assert stopped.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


def test_default_templates_exist_and_pass(capsys):
    assert DEFAULT_TEMPLATES and all(path.is_file() for path in DEFAULT_TEMPLATES)
    assert main([]) == 0
    assert capsys.readouterr().out.startswith("OK:")
