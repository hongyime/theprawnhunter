# API health endpoint disconnects under moderate concurrency

**Date:** 2026-09-05  
**Status:** Symptom confirmed; transport boundary under investigation  
**Scope:** Local production Compose API via `127.0.0.1:8011`

## Symptom contract

The public `/health/` endpoint is a constant-time in-process response. A bounded smoke load of 200
requests with 10 concurrent clients must return 200 for every request without connection resets;
the default p95 budget is 1,000 ms.

## Evidence

The opt-in test at `tests/load/test_health_load.py` produced:

- 100 requests / concurrency 5: 0 errors, p95 603.0 ms, 15.0 requests/second.
- 200 requests / concurrency 10: failed after 13.14 seconds with
  `httpx.RemoteProtocolError: Server disconnected without sending a response`.
- 500 requests / concurrency 25: failed after 14.83 seconds with the same exception.

The API container remained Docker-healthy. Recent API container logs contained no application
traceback for the failures. Compose currently runs four Gunicorn Uvicorn workers, limits the API to
2 CPUs / 2 GiB, and exposes it through Docker Desktop's loopback port mapping.

## Root-cause status

The application route itself performs no I/O, so database/Redis latency is excluded. The absent
application exception makes either the Gunicorn/Uvicorn transport or Docker Desktop's Windows port
forwarding the leading boundary. This is not yet sufficient to choose a production-code fix.

## Next discriminating checks

1. Repeat 200 / 10 from inside the API container against `127.0.0.1:8001`.
2. Capture Gunicorn worker exits/restarts and container CPU/memory during the run.
3. Repeat from the host with keep-alive disabled; compare errors and latency.
4. If only host port forwarding fails, document the machine limitation instead of changing the app.
5. If both paths fail, tune/test Gunicorn worker, backlog, and keep-alive settings one variable at a
   time.

## Verification plan

Run the committed load test with `RUN_HTTP_LOAD_TEST=1`, 200 requests, and concurrency 10. Require
zero non-200 responses and zero transport exceptions in three consecutive runs, followed by the
full deterministic suite.

## Recurrence control

Keep the bounded load test opt-in for local/release environments. Record requests, concurrency,
error count, p95, and throughput in the release report so capacity regressions remain visible.
