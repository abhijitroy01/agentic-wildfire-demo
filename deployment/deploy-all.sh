#!/usr/bin/env bash
# Check, plan and deploy one FireGUard installation from this checkout (see deployment/README.md).
#   bash deployment/deploy-all.sh                  check and plan deployment/installation.toml; no AWS changes
#   bash deployment/deploy-all.sh --apply          deploy or update that installation
#   bash deployment/deploy-all.sh FILE [--apply]   use another installation file (path relative to the repository root)
# Identity and choices live in the installation file (copy deployment/installation.example.toml); operational
# settings live in deployment/settings.toml. --resume-release RELEASE recovers captured assets
# before backend preparation, rejoining an eligible early Runtime build. --acknowledge VALUE (with --apply)
# approves one change the change-set guard stopped for review; copy VALUE from its stop message.
set -euo pipefail
repository="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${WILDFIRE_DEPLOY_PYTHON:-$repository/.venv/bin/python}"
if [[ ! -x "$python_bin" ]]; then
  echo "Python environment not found at $python_bin. Run scripts/bootstrap-dev.sh (Python 3.14) first," >&2
  echo "or set WILDFIRE_DEPLOY_PYTHON to an interpreter with requirements.txt installed." >&2
  exit 2
fi
cd "$repository"
exec "$python_bin" -m deployment.deploy_workspace "$@"
