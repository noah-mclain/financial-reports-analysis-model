"""The harness tests build structured inputs with the ingest tests' shared fixtures.

Running this directory alone does not collect `packages/ingest/tests`, so its `support` module
is put on the path here and not left to the order in which pytest collects the suite."""

from __future__ import annotations

import sys
from pathlib import Path

_INGEST_TESTS = Path(__file__).resolve().parents[2] / "packages" / "ingest" / "tests"
if str(_INGEST_TESTS) not in sys.path:
    sys.path.insert(0, str(_INGEST_TESTS))
