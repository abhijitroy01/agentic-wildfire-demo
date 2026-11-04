#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="${VENV_DIR:-$PROJECT_ROOT/.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3.14}"
CONSTRAINTS_FILE="$PROJECT_ROOT/requirements-lock.txt"

fail() {
    printf 'ERROR: %s\n' "$1" >&2
    exit 1
}

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    fail "Python interpreter '$PYTHON_BIN' was not found. Set PYTHON_BIN to an installed Python 3.14 interpreter."
fi

PYTHON_VERSION="$($PYTHON_BIN -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
# Every image, Lambda and the shared lock target Python 3.14.
if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 14) else 1)'; then
    fail "Python 3.14 is required (images, Lambdas and requirements-lock.txt target it); '$PYTHON_BIN' is Python $PYTHON_VERSION."
fi

if [ ! -f "$CONSTRAINTS_FILE" ]; then
    fail "Python constraints lock not found: $CONSTRAINTS_FILE"
fi

if [ -x "$VENV_DIR/bin/python" ]; then
    VENV_VERSION="$($VENV_DIR/bin/python -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')"
    HOST_VERSION="$($PYTHON_BIN -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')"
    if [ "$VENV_VERSION" != "$HOST_VERSION" ]; then
        fail "Existing $VENV_DIR uses Python $VENV_VERSION, but $PYTHON_BIN uses $HOST_VERSION. Remove .venv and rerun the bootstrap."
    fi
else
    printf 'Creating %s with Python %s...\n' "$VENV_DIR" "$PYTHON_VERSION"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

printf 'Installing locked Python dependencies...\n'
"$VENV_DIR/bin/python" -m pip install --constraint "$CONSTRAINTS_FILE" --requirement "$PROJECT_ROOT/requirements.txt"
"$VENV_DIR/bin/python" -m pip check

if ! command -v osmium >/dev/null 2>&1; then
    fail "Local road preparation requires osmium-tool 1.19.1 with libosmium 2.23.1. Install osmium-tool (Homebrew on macOS) or use the verified worker container, then rerun."
fi
OSMIUM_VERSION="$(osmium --version)"
case "$OSMIUM_VERSION" in
    *"osmium version 1.19.1"*"libosmium version 2.23.1"*) ;;
    *) fail "Local Osmium does not match the reviewed 1.19.1 / libosmium 2.23.1 pins. Use matching tools or the verified worker container." ;;
esac

if [ "${SKIP_UI:-0}" = "1" ]; then
    printf 'Skipping UI dependencies because SKIP_UI=1.\n'
else
    command -v node >/dev/null 2>&1 || fail "Node.js 22.13 or newer was not found. Install Node.js or rerun with SKIP_UI=1."
    NODE_VERSION="$(node -p 'process.versions.node')"
    NODE_MAJOR="${NODE_VERSION%%.*}"
    NODE_MINOR="${NODE_VERSION#*.}"
    NODE_MINOR="${NODE_MINOR%%.*}"
    case "$NODE_MAJOR:$NODE_MINOR" in
        :*|*:|*[!0-9:]*) fail "Could not parse Node.js version '$NODE_VERSION'." ;;
    esac
    # The ui/package.json engines floor: ESLint 10 supports Node 22 from 22.13.
    if [ "$NODE_MAJOR" -lt 22 ] || { [ "$NODE_MAJOR" -eq 22 ] && [ "$NODE_MINOR" -lt 13 ]; }; then
        fail "Node.js 22.13 or newer is required; found Node.js $NODE_VERSION. Rerun with SKIP_UI=1 for Python-only setup."
    fi
    command -v npm >/dev/null 2>&1 || fail "npm was not found. Install npm or rerun with SKIP_UI=1."
    printf 'Installing locked UI dependencies...\n'
    npm ci --prefix "$PROJECT_ROOT/ui"
    printf 'Installing locked deployment (CDK) dependencies...\n'
    npm ci --prefix "$PROJECT_ROOT/deployment/infra"
    printf 'Generating UI schema types...\n'
    npm --prefix "$PROJECT_ROOT/ui" run generate:types
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
    printf 'WARNING: ffmpeg is not installed. It is only required for local Broadcastify audio processing.\n' >&2
fi

printf '\nDevelopment environment is ready.\n'
printf 'Activate Python: source %s/bin/activate\n' "$VENV_DIR"
printf 'Start the API:   %s/bin/python %s/backend/scripts/local_server.py\n' "$VENV_DIR" "$PROJECT_ROOT"
printf 'Start the UI:    npm --prefix %s/ui run dev\n' "$PROJECT_ROOT"
printf 'Run all checks:  %s/bin/python scripts/check_quality.py all\n' "$VENV_DIR"
printf 'Deploy to AWS:   copy deployment/installation.example.toml to deployment/installation.toml, then run\n'
printf '                 bash deployment/deploy-all.sh (check) and bash deployment/deploy-all.sh --apply\n'
