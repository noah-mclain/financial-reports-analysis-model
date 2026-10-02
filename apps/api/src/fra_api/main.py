"""The served application: ``uvicorn fra_api.main:app``. The only place the API reads the
environment."""

from __future__ import annotations

import os

from fra_api.app import create_app
from fra_core.profile import read_profile

app = create_app(read_profile(os.environ))
