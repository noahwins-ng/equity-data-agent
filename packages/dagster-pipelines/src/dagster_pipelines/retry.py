"""Deploy-window retry protection (QNT-110).

Two complementary layers:

1. **Op-level** (`DEPLOY_WINDOW_RETRY`) — handles flaky ops *inside* a running run
   (yfinance timeout, transient ClickHouse error). Retries the failing step in
   place without re-launching the whole run.

2. **Run-level** (`DEPLOY_WINDOW_RUN_RETRY_TAGS` + `run_retries` in `dagster.yaml`)
   — handles whole-run failures including dequeue/launch errors. The observed
   Apr 19 incident fell into this bucket: daemon got `gRPC UNAVAILABLE` while
   dequeuing a run because the code-server container was mid-restart from a
   deploy, so no op ever ran. Op-level retry doesn't help — the daemon re-launches
   the whole run instead.

See `docs/patterns.md` §"Retry policy" for application guidance.
"""

from dagster import Backoff, Jitter, RetryPolicy

DEPLOY_WINDOW_RETRY = RetryPolicy(
    max_retries=3,
    delay=30,
    backoff=Backoff.EXPONENTIAL,
    jitter=Jitter.PLUS_MINUS,
)

# Tags applied to job definitions so the instance-level `run_retries` mechanism
# (enabled in dagster.yaml) re-launches failed runs up to max_retries times.
#
# retry_on_asset_or_op_failure=false in dagster.yaml ensures only LAUNCH-level
# failures trigger a re-launch. User errors inside ops still fail loud — a real
# bug in an asset should not silently retry.
DEPLOY_WINDOW_RUN_RETRY_TAGS = {
    "dagster/max_retries": "3",
}
