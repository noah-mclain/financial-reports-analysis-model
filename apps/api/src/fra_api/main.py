"""The served application: ``uvicorn fra_api.main:app``. The only place the API reads the
environment and its settings."""

from __future__ import annotations

import os

from fra_api.app import create_app
from fra_core.jobs import load_job_settings
from fra_core.paths import JOB_SETTINGS, REPO_ROOT
from fra_core.profile import read_profile

app = create_app(read_profile(os.environ), load_job_settings(JOB_SETTINGS, REPO_ROOT))
