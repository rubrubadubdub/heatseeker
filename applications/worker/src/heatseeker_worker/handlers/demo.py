"""Demo handlers proving the job framework (M0 acceptance: jobs can be observed)."""

import time

from heatseeker_common.job_registry import JobContext, job_handler


@job_handler("demo.echo")
def echo(ctx: JobContext) -> dict:
    ctx.logger.info("demo.echo executing", extra={"job_id": ctx.job_id})
    return {"echo": ctx.payload}


@job_handler("demo.sleep")
def sleep(ctx: JobContext) -> dict:
    seconds = float(ctx.payload.get("seconds", 0.1))
    time.sleep(seconds)
    return {"slept": seconds}


@job_handler("demo.fail_once")
def fail_once(ctx: JobContext) -> dict:
    """Fails on the first attempt, succeeds on retry — exercises backoff."""
    if ctx.attempt == 1:
        raise RuntimeError("planned first-attempt failure")
    return {"recovered": True, "attempt": ctx.attempt}
