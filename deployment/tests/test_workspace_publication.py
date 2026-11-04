import json
import re
import secrets
import struct
from pathlib import Path

import pytest

from deployment import publish_workspace
from deployment.publish_workspace import build_environment, publish, verify_assets

REPOSITORY = Path(__file__).resolve().parents[2]
# PEM armor is assembled at runtime so no literal key header exists in the repository.
DASHES = "-" * 5


def pem_header(label):
    return f"{DASHES}BEGIN {label}{DASHES}".encode()


def test_build_cannot_inherit_browser_secrets(tmp_path):
    (tmp_path / ".env.local").write_text("VITE_AWS_SECRET_ACCESS_KEY='local-fixture-secret'\nVITE_NEW_API_KEY=fixture-provider-key\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/config.ts").write_text("import.meta.env.VITE_ANOTHER_TOKEN")
    env, secrets = build_environment(tmp_path, {
        "PATH": "/bin", "VITE_AWS_ACCESS_KEY_ID": "AKIA" + "Q"*16, "VITE_BROADCASTIFY_API_KEY": "fixture-radio-key"})
    assert env["VITE_AWS_ACCESS_KEY_ID"] == env["VITE_AWS_SECRET_ACCESS_KEY"] == ""
    assert env["VITE_NEW_API_KEY"] == env["VITE_ANOTHER_TOKEN"] == env["VITE_BROADCASTIFY_API_KEY"] == ""
    assert env["VITE_USE_AUTH_BROKER"] == env["VITE_EVACUATION_ENABLED"] == "true"
    assert env["PATH"] == "/bin" and "local-fixture-secret" in secrets


def test_published_builds_carry_no_camera_configuration(tmp_path):
    """The camera goes through the broker, so every camera Vite variable is blank, even when a checkout sets them."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src/cameraService.ts").write_text(
        "import.meta.env.VITE_WILDFIREAI_DEVICE_ID; import.meta.env.VITE_WILDFIREAI_SENSOR;"
        " import.meta.env.VITE_WILDFIREAI_API_KEY")
    key = "-".join(("cam", secrets.token_hex(8)))
    env, scanned = build_environment(tmp_path, {"VITE_WILDFIREAI_DEVICE_ID": "local-device",
                                                "VITE_WILDFIREAI_SENSOR": "camera_a",
                                                "VITE_WILDFIREAI_API_KEY": key})
    assert [env[name] for name in ("VITE_WILDFIREAI_DEVICE_ID", "VITE_WILDFIREAI_SENSOR",
                                   "VITE_WILDFIREAI_API_KEY")] == ["", "", ""]
    assert env["VITE_USE_AUTH_BROKER"] == env["VITE_EVACUATION_ENABLED"] == "true"
    assert key in scanned, "A key-named value still feeds the published assets' secret scan"


@pytest.mark.parametrize("name,payload,secrets", [
    ("app.js", b"AKIA"+b"Q"*16, set()), ("app.js", b"fixture-provider-secret", {"fixture-provider-secret"}),
    ("app.js.map", b"{}", set()), (".env", b"x", set()),
    # Hosting metadata is not exempt: an Amplify _redirects file is an unexpected asset type.
    ("_redirects", b"/*  /index.html  200", set()),
])
def test_unpublishable_assets_stop_release(tmp_path, name, payload, secrets):
    (tmp_path / "index.html").write_text("safe")
    (tmp_path / name).write_bytes(payload)
    with pytest.raises(ValueError):
        verify_assets(tmp_path, secrets)


@pytest.mark.parametrize("kind", ["", "RSA ", "EC ", "DSA ", "OPENSSH ", "ENCRYPTED ", "PGP "])
def test_every_pem_private_key_header_blocks_publication(tmp_path, kind):
    label = f"{kind}PRIVATE KEY BLOCK" if kind == "PGP " else f"{kind}PRIVATE KEY"
    (tmp_path / "index.html").write_text("safe")
    (tmp_path / "app.js").write_bytes(b"const key = `" + pem_header(label) + b"\nMIIfixture`;")
    with pytest.raises(ValueError, match="Credential signature found in app.js"):
        verify_assets(tmp_path, set())


def test_public_certificates_and_keys_remain_publishable(tmp_path):
    (tmp_path / "index.html").write_text("safe")
    (tmp_path / "app.js").write_bytes(pem_header("CERTIFICATE") + b"\n" + pem_header("PUBLIC KEY"))
    assert [name for name, _ in verify_assets(tmp_path, set())] == ["app.js", "index.html"]


def image_sizes(data):
    """List the 'WxH' pixel sizes of a PNG, or of every frame in an ICO directory."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ["{}x{}".format(*struct.unpack(">II", data[16:24]))]
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind) == (0, 1), "neither a PNG nor an ICO file"
    # Each 16-byte ICO directory entry stores width and height in one byte each; 0 means 256.
    return [f"{data[6 + 16 * i] or 256}x{data[7 + 16 * i] or 256}" for i in range(count)]


def test_published_icons_name_fireguard_and_match_their_declared_sizes():
    """The manifest names FireGuard AI; the page, manifest and header point at existing icons of that size."""
    public = REPOSITORY / "ui/public"
    manifest = json.loads((public / "manifest.json").read_text())
    assert manifest["name"] == manifest["short_name"] == "FireGuard AI"
    for icon in manifest["icons"]:
        assert sorted(image_sizes((public / icon["src"]).read_bytes())) == sorted(icon["sizes"].split())
    page = (REPOSITORY / "ui/index.html").read_text()
    links = re.findall(r'<link rel="(?:icon|apple-touch-icon|manifest)" href="/([^"]+)"', page)
    assert len(links) == 3 and all((public / name).is_file() for name in links)
    header = (REPOSITORY / "ui/src/features/layout/components/AppHeader.tsx").read_text()
    logo = re.search(r'<img src="/([^"]+)"', header)[1]
    # workspace-theme.css shows the logo 40 px high, so it ships at twice that, not at 1024 px.
    assert " " not in logo and image_sizes((public / logo).read_bytes()) == ["80x80"]


def test_publisher_uses_verified_bytes_and_uploads_index_last(tmp_path):
    (tmp_path / "index.html").write_text("safe")
    (tmp_path / "app.js").write_text("verified")
    files = verify_assets(tmp_path, set())
    (tmp_path / "app.js").write_text("changed after verification")
    calls = []

    class AWS:
        def client(self, name):
            return self

        def put_object(self, **params):
            calls.append(params)

        def create_invalidation(self, **params):
            calls.append(params)
    publish(AWS(), {"WorkspaceAssetsBucket": "private-ui", "IntegratedDistributionId": "distribution"}, files)
    assert calls[0]["Key"] == "app.js" and calls[0]["Body"] == b"verified"
    assert calls[1]["Key"] == "index.html"
    assert calls[2]["DistributionId"] == "distribution"


@pytest.mark.parametrize("argv", [[], ["--profile", "fixture", "--apply"]])
def test_standalone_publisher_is_retired_in_favor_of_deploy_all(argv):
    with pytest.raises(SystemExit) as stopped:
        publish_workspace.main(argv)
    assert str(stopped.value) == publish_workspace.RETIRED
    assert "deploy-all.sh --apply" in publish_workspace.RETIRED and "deployment/README.md" in publish_workspace.RETIRED
    assert not hasattr(publish_workspace, "boto3"), "the retired command must not reach AWS"
