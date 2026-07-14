"""Central paths and settings. Local-first; everything resolves off the repo root."""
from __future__ import annotations

import os
from pathlib import Path

# Application version-control identity (surfaced in every About panel / report).
APP_NAME = "DFM & Design-Criteria Feedback Tool"
APP_VERSION = "0.6.0"

# Upload guardrails (env-overridable like the other settings). Only these
# extensions are accepted on write paths, and uploads above the size cap are
# rejected with HTTP 400.
MAX_UPLOAD_MB = int(os.environ.get("DFM_MAX_UPLOAD_MB", "50"))
ALLOWED_UPLOAD_EXTENSIONS = {".stp", ".step", ".pdf", ".hlg"}

# repo root = two levels up from this file (backend/app/config.py -> repo root)
REPO_ROOT = Path(__file__).resolve().parents[2]

# The seed YAML is the documented single source of truth and lives at the repo
# root. Override with DFM_CRITERIA_SEED if you keep it elsewhere.
CRITERIA_SEED_PATH = Path(
    os.environ.get("DFM_CRITERIA_SEED", REPO_ROOT / "dfm-criteria.seed.yaml")
)

# Versioned criteria store + CTF capability history.
CRITERIA_DIR = REPO_ROOT / "criteria"
DB_PATH = Path(os.environ.get("DFM_DB_PATH", CRITERIA_DIR / "dfm.sqlite"))

# Where uploaded drawings/models land for a run.
UPLOAD_DIR = REPO_ROOT / "uploads"
EXAMPLES_DIR = REPO_ROOT / "examples"

for _d in (CRITERIA_DIR, UPLOAD_DIR, EXAMPLES_DIR):
    _d.mkdir(parents=True, exist_ok=True)
