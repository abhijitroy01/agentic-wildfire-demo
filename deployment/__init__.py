"""Secure single-installation deployer for FireGUard."""
from pathlib import Path

# Repository root of this checkout. Deployer modules import this one value and pass it explicitly.
ROOT = Path(__file__).resolve().parents[1]
# Frozen npm install for host builds. Install scripts would run with the deploy profile in the environment;
# the locked packages need none (esbuild finds its binary in the optional platform package, and the fsevents
# and vaadin usage-statistics scripts are not used by vite build, npm test or cdk synth).
NPM_CI = ("npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund")
