"""
Environment Loader - Loads .env file at module import time.

Resolution order:
1. AGENTCORE_RUNTIME=true → skip .env entirely; CloudFormation injects all env vars.
2. Path /app/.env (container) — used when running inside the deployed image.
3. backend/.env from repo (developer machines) — relative to this file.

This MUST be imported first by any module that reads env vars at module load time.
Diagnostics go to the debug log, never stdout, and never include secrets.
"""
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)
_IS_AGENTCORE = os.getenv("AGENTCORE_RUNTIME", "").lower() == "true"

if _IS_AGENTCORE:
    # CloudFormation EnvironmentVariables is the source of truth in production.
    logger.debug("env_loader: AGENTCORE_RUNTIME=true; skipping .env, using CloudFormation env vars")
else:
    # Try /app/.env first (container layout), then walk up from this file (dev machines).
    candidates = [Path("/app/.env")]
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidates.append(parent / ".env")
        if parent.name == "backend":
            break

    loaded_path = None
    for path in candidates:
        if path.is_file():
            load_dotenv(path)
            loaded_path = path
            break

    if loaded_path:
        logger.debug("env_loader: loaded %s", loaded_path)
    else:
        logger.debug("env_loader: no .env file found; tried %s", ", ".join(str(p) for p in candidates))

logger.debug("env_loader: supervisor model %s, Memory %s, Gateway %s",
             (os.getenv("SUPERVISOR_MODEL_ID") or "NOT_SET")[:60], os.getenv("DEPLOYED_MemoryId", "NOT_SET"),
             (os.getenv("DEPLOYED_GatewayUrl") or "NOT_SET")[:60])
