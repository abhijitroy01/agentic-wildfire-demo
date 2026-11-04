"""Build a credential-free FireGUard UI and publish to the private CloudFront S3 origin (used by deploy-all.sh)."""
import hashlib
import mimetypes
import re

# A standalone publish rebuilt the UI from the checkout outside a release, so it could publish a UI that does not
# match the deployed API. deploy-all.sh --apply publishes the UI it captured and verified for that release.
RETIRED = ("deployment/publish_workspace.py no longer runs on its own. Run bash deployment/deploy-all.sh --apply, "
           "which builds, verifies and publishes the UI with the release it belongs to (see deployment/README.md).")
SECRET_NAME = re.compile(r"(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.IGNORECASE)
AWS_KEY = re.compile(rb"(?:AKIA|ASIA)[A-Z0-9]{16}")
# Any PEM private key header: PKCS#8, RSA, EC, DSA, OPENSSH, ENCRYPTED and PGP blocks. The dashes are joined
# at runtime so this source never contains a literal header for secret scanners to flag.
PEM_DASHES = b"-" * 5
PRIVATE_KEY = re.compile(PEM_DASHES + rb"BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?" + PEM_DASHES)
ALLOWED = {".html", ".js", ".css", ".json", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".ico",
           ".woff", ".woff2", ".ttf", ".webp", ".wasm", ".mp3", ".wav", ".mp4", ".txt"}


def secret_values(variables):
    """Return the values of secret-named variables (KEY, TOKEN, SECRET, ...) long enough to search a bundle for."""
    return {str(value) for name, value in variables.items() if SECRET_NAME.search(name) and len(str(value)) >= 12}


def build_environment(ui, inherited):
    """Override every configured VITE variable; permit only fixed public-demo values."""
    names = {name for name in inherited if name.startswith("VITE_")}
    secrets = secret_values(inherited)
    # Vite loads .env and .env.local even with a dedicated mode; blank them explicitly.
    for path in ui.glob(".env*"):
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            match = re.match(r"\s*(?:export\s+)?(VITE_[A-Z0-9_]+)\s*=\s*(.*)", line)
            if match:
                name, value = match.groups()
                names.add(name)
                value = value.strip().strip("'\"")
                if SECRET_NAME.search(name) and len(value) >= 12:
                    secrets.add(value)
    for path in (ui / "src").rglob("*"):
        if path.suffix in {".ts", ".tsx", ".js"}:
            names.update(re.findall(r"\bVITE_[A-Z0-9_]+\b", path.read_text()))
    # The camera, like every other service, goes through the broker, so no VITE_WILDFIREAI_* value is pinned.
    env = {**inherited, **dict.fromkeys(names, ""), "VITE_EVACUATION_ENABLED": "true",
           "VITE_USE_AUTH_BROKER": "true"}
    # Ensure these are absent even in checkouts whose environment schema omitted them.
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "BROADCASTIFY_API_KEY"):
        env["VITE_" + name] = ""
    return env, secrets


def verify_assets(dist, secrets):
    """Return publishable (name, bytes) UI assets; block symlinks, odd types, keys and known secret values."""
    files = []
    if not (dist / "index.html").is_file():
        raise ValueError("UI build is missing index.html.")
    for path in sorted(dist.rglob("*")):
        if path.is_symlink():
            raise ValueError("Symlinks are not publishable UI assets.")
        if not path.is_file():
            continue
        rel = path.relative_to(dist)
        if any(part.startswith(".") for part in rel.parts) or path.suffix not in ALLOWED:
            raise ValueError(f"Unexpected public asset type: {rel}")
        payload = path.read_bytes()
        if AWS_KEY.search(payload) or PRIVATE_KEY.search(payload):
            raise ValueError(f"Credential signature found in {rel}; publication blocked.")
        if any(secret.encode() in payload for secret in secrets):
            raise ValueError(f"Configured secret found in {rel}; publication blocked.")
        files.append((str(rel), payload))
    return files


def publish(session, stack, files):
    """Publish validated bytes; never upload a changed file after verification."""
    s3 = session.client("s3")
    # Upload index last, after its referenced immutable Vite assets exist.
    for key, body in sorted(files, key=lambda item: item[0] == "index.html"):
        s3.put_object(Bucket=stack["WorkspaceAssetsBucket"], Key=key, Body=body,
                      ContentType=mimetypes.guess_type(key)[0] or "application/octet-stream",
                      CacheControl="no-store")
    digest = hashlib.sha256(b"".join(key.encode()+body for key, body in files)).hexdigest()
    session.client("cloudfront").create_invalidation(
        DistributionId=stack["IntegratedDistributionId"],
        InvalidationBatch={"CallerReference": digest, "Paths": {"Quantity": 1, "Items": ["/*"]}})
    return digest


def main(argv=None):
    """Retired standalone entry point: deploy-all.sh publishes the UI captured in each release."""
    raise SystemExit(RETIRED)


if __name__ == "__main__":
    main()
