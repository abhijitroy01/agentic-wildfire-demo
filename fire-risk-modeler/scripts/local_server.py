"""Start the private modeler locally; FIRE_RISK_SERVICE_KEY is required."""
import os
import sys
from pathlib import Path


def main():
    repository = Path(__file__).resolve().parents[2]
    os.environ.setdefault("FIRE_RISK_ENVIRONMENT", "local")
    os.environ["PYTHONPATH"] = str(repository / "fire-risk-modeler")
    os.chdir(repository)
    # Replaces the process on fixed arguments, with the credential only in the inherited environment.
    # nosemgrep: python.lang.security.audit.dangerous-os-exec-audit
    os.execv(sys.executable, [sys.executable, "-m", "uvicorn", "fire_risk_modeler.service:app",  # nosec B606
                             "--host", "127.0.0.1", "--port", "8085"])


if __name__ == "__main__":
    main()
