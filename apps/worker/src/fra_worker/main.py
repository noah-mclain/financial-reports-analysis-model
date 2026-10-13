"""``fra-worker``: claim queued jobs and run them, one at a time. The only place the worker
reads the environment and the settings.

    fra-worker           run until stopped, looking for a job every poll interval
    fra-worker --once    run every queued job, then exit
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from fra_analytics.policy import load_policy
from fra_core.jobs import JobStore, load_job_settings
from fra_core.paths import ANALYTICS_POLICY, JOB_SETTINGS, REPO_ROOT
from fra_core.profile import read_profile
from fra_ingest.config import load_config
from fra_worker.pipeline import Pipeline, real_pipeline, run_job


def drain(store: JobStore, pipeline: Pipeline, max_pages: int) -> int:
    """Run queued jobs until none is left; the number run."""
    count = 0
    while (job := store.claim()) is not None:
        print(f"job {job.id}: running", file=sys.stderr, flush=True)
        finished = run_job(store, job, pipeline, max_pages)
        reason = f": {finished.error}" if finished.error else ""
        print(f"job {job.id}: {finished.status.value}{reason}", file=sys.stderr, flush=True)
        count += 1
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fra-worker", description=__doc__)
    parser.add_argument("--once", action="store_true", help="run the queued jobs, then exit")
    args = parser.parse_args(argv)
    profile = read_profile(os.environ)
    settings = load_job_settings(JOB_SETTINGS, REPO_ROOT)
    store = JobStore(settings.root)
    for job_id in store.fail_interrupted():
        print(f"job {job_id}: failed, left running by a stopped worker", file=sys.stderr)
    pipeline = real_pipeline(load_config(), load_policy(ANALYTICS_POLICY))
    print(f"worker ready: profile {profile.value}, jobs in {settings.root}", file=sys.stderr)
    while True:
        drain(store, pipeline, settings.max_pages)
        if args.once:
            return 0
        time.sleep(settings.poll_seconds)


if __name__ == "__main__":
    sys.exit(main())
