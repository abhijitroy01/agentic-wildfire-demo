#!/usr/bin/env python3
"""Validate shared CloudFormation baseline rules on the backend templates.

The rules: Fargate tasks get no public IP, CloudFront distributions have a WAF web
ACL and HTTPS custom origins, ECR release tags are immutable, and task definitions
and the AgentCore Runtime never reference an image by ':latest'. By default it checks
the backend nested templates that define the Runtime ECR repository and the AgentCore
Runtime. The historical Broadcastify and Firetruck stack templates it was written for
were removed on 2026-10-01; those services deploy with the CDK application stack.

Any finding fails the check with exit status 1; --report-only prints the findings and
exits 0. --strict names the default and is kept for existing commands.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - exercised on lean dev machines
    yaml = None


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATES = (
    ROOT / "backend/deployment/templates/nested/1-infrastructure.yaml",
    ROOT / "backend/deployment/templates/nested/4-agentcore-resources.yaml",
)


if yaml:
    class CloudFormationLoader(yaml.SafeLoader):
        """YAML loader that keeps CloudFormation intrinsic tags parseable."""

    def _load_intrinsic(
        loader: CloudFormationLoader,
        tag_suffix: str,
        node: Any,
    ) -> Any:
        if isinstance(node, yaml.ScalarNode):
            value = loader.construct_scalar(node)
        elif isinstance(node, yaml.SequenceNode):
            value = loader.construct_sequence(node)
        else:
            value = loader.construct_mapping(node)
        return {tag_suffix: value}

    CloudFormationLoader.add_multi_constructor("!", _load_intrinsic)
else:
    CloudFormationLoader = None


def load_template(text: str) -> Any:
    """Parse CloudFormation YAML safely: intrinsic tags such as !Ref become one-key mappings, Python tags fail."""
    # CloudFormationLoader subclasses SafeLoader, so yaml.load (Bandit B506) is not needed to keep the tags.
    loader = CloudFormationLoader(text)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


@dataclass(frozen=True)
class Finding:
    template: Path
    resource: str
    rule: str
    message: str


def _resources(template: dict[str, Any]) -> dict[str, dict[str, Any]]:
    resources = template.get("Resources", {})
    return resources if isinstance(resources, dict) else {}


def _container_definitions(resource: dict[str, Any]) -> list[dict[str, Any]]:
    properties = resource.get("Properties", {})
    definitions = properties.get("ContainerDefinitions", [])
    return definitions if isinstance(definitions, list) else []


def _stringish(value: Any) -> str:
    if isinstance(value, str):
        return value
    return str(value)


def _validate_template(path: Path) -> list[Finding]:
    if yaml is None:
        return _validate_template_text(path)

    template = load_template(path.read_text(encoding="utf-8"))
    findings: list[Finding] = []

    for name, resource in _resources(template).items():
        resource_type = resource.get("Type")
        properties = resource.get("Properties", {})

        if resource_type == "AWS::ECS::Service":
            awsvpc = (
                properties.get("NetworkConfiguration", {})
                .get("AwsvpcConfiguration", {})
            )
            if awsvpc.get("AssignPublicIp") == "ENABLED":
                findings.append(Finding(
                    path,
                    name,
                    "ecs-private-subnets",
                    "Fargate services should run without public task IPs; place tasks in private subnets behind ALB/CloudFront.",
                ))

        if resource_type == "AWS::CloudFront::Distribution":
            distribution = properties.get("DistributionConfig", {})
            if "WebACLId" not in distribution:
                findings.append(Finding(
                    path,
                    name,
                    "cloudfront-waf",
                    "CloudFront distributions should attach an AWS WAFv2 web ACL with managed and rate-based rules.",
                ))
            for origin in distribution.get("Origins", []) or []:
                custom_origin = origin.get("CustomOriginConfig", {})
                if custom_origin.get("OriginProtocolPolicy") == "http-only":
                    findings.append(Finding(
                        path,
                        name,
                        "cloudfront-origin-tls",
                        "CloudFront custom origins should use HTTPS-only or match-viewer once the ALB has ACM TLS.",
                    ))

        if resource_type == "AWS::ECR::Repository":
            mutability = properties.get("ImageTagMutability")
            if mutability not in {"IMMUTABLE", "IMMUTABLE_WITH_EXCLUSION"}:
                findings.append(Finding(
                    path,
                    name,
                    "ecr-tag-immutability",
                    "ECR repositories should prevent release tags from being overwritten.",
                ))

        if resource_type == "AWS::ECS::TaskDefinition":
            for container in _container_definitions(resource):
                image = _stringish(container.get("Image", ""))
                if image.endswith(":latest") or ":latest" in image:
                    findings.append(Finding(
                        path,
                        f"{name}/{container.get('Name', 'container')}",
                        "immutable-image-reference",
                        "Task definitions should reference an immutable image tag or digest, not :latest.",
                    ))

        if resource_type == "AWS::BedrockAgentCore::Runtime":
            artifact = properties.get("AgentRuntimeArtifact", {})
            container_uri = (
                artifact.get("ContainerConfiguration", {}).get("ContainerUri", "")
            )
            image = _stringish(container_uri)
            if image.endswith(":latest") or ":latest" in image:
                findings.append(Finding(
                    path,
                    name,
                    "immutable-image-reference",
                    "AgentCore Runtime should reference an immutable image tag or digest, not :latest.",
                ))

    return findings


def _validate_template_text(path: Path) -> list[Finding]:
    """Fallback scanner used when PyYAML is not installed."""
    text = path.read_text(encoding="utf-8")
    findings: list[Finding] = []

    def add(resource: str, rule: str, message: str) -> None:
        findings.append(Finding(path, resource, rule, message))

    if "AssignPublicIp: ENABLED" in text:
        add(
            "AWS::ECS::Service",
            "ecs-private-subnets",
            "Fargate services should run without public task IPs; place tasks in private subnets behind ALB/CloudFront.",
        )
    if "Type: AWS::CloudFront::Distribution" in text and "WebACLId:" not in text:
        add(
            "AWS::CloudFront::Distribution",
            "cloudfront-waf",
            "CloudFront distributions should attach an AWS WAFv2 web ACL with managed and rate-based rules.",
        )
    if "OriginProtocolPolicy: http-only" in text:
        add(
            "AWS::CloudFront::Distribution",
            "cloudfront-origin-tls",
            "CloudFront custom origins should use HTTPS-only or match-viewer once the ALB has ACM TLS.",
        )
    if "Type: AWS::ECR::Repository" in text and "ImageTagMutability:" not in text:
        add(
            "AWS::ECR::Repository",
            "ecr-tag-immutability",
            "ECR repositories should prevent release tags from being overwritten.",
        )
    if ":latest" in text:
        add(
            "container image",
            "immutable-image-reference",
            "Task and runtime definitions should reference immutable image tags or digests, not :latest.",
        )

    return findings


def main(argv: list[str] | None = None) -> int:
    """Check CloudFormation templates against the shared baseline; exit 1 on findings unless --report-only."""
    parser = argparse.ArgumentParser(
        description="Validate shared CloudFormation baseline rules. Findings fail the check (exit 1)."
    )
    parser.add_argument(
        "templates",
        nargs="*",
        type=Path,
        default=list(DEFAULT_TEMPLATES),
        help="Template paths to validate. Defaults to the backend templates in DEFAULT_TEMPLATES.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 when findings are present. This is the default; the flag is kept for existing commands.",
    )
    mode.add_argument(
        "--report-only",
        action="store_true",
        help="Print findings and exit 0, for a review that must not fail.",
    )
    args = parser.parse_args(argv)

    findings: list[Finding] = []
    for template in args.templates:
        path = template if template.is_absolute() else ROOT / template
        findings.extend(_validate_template(path))

    if not findings:
        print("OK: all templates satisfy the shared infrastructure baseline")
        return 0

    print(f"Found {len(findings)} shared infrastructure baseline finding(s):")
    for finding in findings:
        rel = finding.template.relative_to(ROOT) if finding.template.is_relative_to(ROOT) else finding.template
        print(f"- {rel} :: {finding.resource} :: {finding.rule}")
        print(f"  {finding.message}")

    return 0 if args.report_only else 1


if __name__ == "__main__":
    raise SystemExit(main())
