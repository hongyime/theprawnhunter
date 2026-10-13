"""Scanner task implementations for GitHub Gist."""

import json

from ..common import *


GIST_RETRY_FILES_KEY = "scanner:gist:retry-files"


def _gist_file_key(file_job: dict[str, str]) -> tuple[str, str, str]:
    return (
        str(file_job.get("gist_id", "")),
        str(file_job.get("file", "")),
        str(file_job.get("raw_url", "")),
    )


def _unique_gist_files(file_jobs: list[dict[str, str]]) -> list[dict[str, str]]:
    found = {}
    for file_job in file_jobs:
        if not isinstance(file_job, dict):
            continue
        gist_id = file_job.get("gist_id")
        filename = file_job.get("file")
        raw_url = file_job.get("raw_url")
        if not isinstance(raw_url, str) or not raw_url.startswith("https://"):
            continue
        clean_job = {
            "gist_id": str(gist_id or "unknown"),
            "file": str(filename or "unknown"),
            "raw_url": raw_url,
        }
        found[_gist_file_key(clean_job)] = clean_job
    return list(found.values())


def _load_gist_retry_files() -> list[dict[str, str]]:
    stored = redis_client.get(GIST_RETRY_FILES_KEY)
    if stored is None:
        return []
    if isinstance(stored, bytes):
        stored = stored.decode("utf-8")
    file_jobs = json.loads(stored)
    if not isinstance(file_jobs, list):
        raise ValueError("saved Gist retry list is not a list")
    if any(
        not isinstance(item, dict)
        or not isinstance(item.get("raw_url"), str)
        or not item["raw_url"].startswith("https://")
        for item in file_jobs
    ):
        raise ValueError("saved Gist retry list has an invalid file entry")
    return _unique_gist_files(file_jobs)


def _store_gist_retry_files(file_jobs: list[dict[str, str]]) -> None:
    unique_files = _unique_gist_files(file_jobs)
    if not unique_files:
        redis_client.delete(GIST_RETRY_FILES_KEY)
        return
    redis_client.set(
        GIST_RETRY_FILES_KEY,
        json.dumps(unique_files, separators=(",", ":")),
    )


async def _send_gist_log(message: str) -> None:
    try:
        await _send_log_async(message)
    except Exception as error:
        logger.warning("[Gist] Could not send this message to the scan log: %s", error)


@app.task(name="scanner.scan_gist", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def scan_gist(query: str = None):
    return _run_sync(_scan_gist_async(query))


async def _scan_gist_async(query: str = None):
    scan_state = {}

    async def search_gists():
        try:
            retry_files = _load_gist_retry_files()
        except Exception as error:
            scan_state["must_retry_task"] = True
            logger.error(
                "[Gist] Could not read the saved file retry list: %s. Its Redis value is left in place; "
                "this scan will be retried, and the scheduled scan will try again if needed.",
                error,
            )
            raise

        try:
            outcome = await gist_srv.search(retry_files=retry_files)
        except Exception as error:
            message = (
                f"[Gist] Public Gist search failed: {type(error).__name__}: {error}. "
                "Saved file retries were left in Redis; the scheduled scan will try again."
            )
            logger.error(message)
            await _send_gist_log(message)
            raise
        scan_state["outcome"] = outcome

        if outcome.missing_token:
            message = (
                "[Gist] GitHub login token is missing or empty. The scan was skipped, "
                "saved file retries were kept, and the next scheduled scan will try again."
            )
            logger.warning(message)
            await _send_gist_log(message)
        elif outcome.search_error:
            message = (
                f"[Gist] Public Gist page 1 failed: {outcome.search_error}. "
                "Saved file retries were still checked; the scheduled scan will request this page again."
            )
            logger.error(message)
            await _send_gist_log(message)
        elif outcome.gist_count == 0:
            logger.info(
                "[Gist] Public Gist page 1 returned no Gists; the next scheduled scan will check again."
            )
        else:
            logger.info(
                "[Gist] Checked %s public Gists and found %s matching tokens.",
                outcome.gist_count,
                len(outcome.results),
            )

        for message in outcome.messages:
            logger.error("[Gist] %s", message)
            await _send_gist_log(f"[Gist] {message}")

        return outcome.results

    async def save_gist_results(results, source_name: str):
        outcome = scan_state.get("outcome")
        if outcome is None:
            return await _save_credentials_async(results, source_name)

        all_files = _unique_gist_files(outcome.completed_files + outcome.failed_files)
        try:
            _store_gist_retry_files(all_files)
        except Exception as error:
            scan_state["must_retry_task"] = True
            logger.error(
                "[Gist] Could not save file work for another try: %s. Results from this scan "
                "were not queued; this scan will be retried before the next scheduled scan.",
                error,
            )
            raise

        for file_job in outcome.failed_files:
            message = (
                f"[Gist] Gist {file_job['gist_id']} file {file_job['file']} failed: "
                f"{file_job.get('error', 'unknown error')}. Saved for the next scan."
            )
            logger.error(message)
            await _send_gist_log(message)

        try:
            saved = await _save_credentials_async(results, source_name)
        except Exception as error:
            scan_state["must_retry_task"] = True
            logger.error(
                "[Gist] Could not save this scan's results: %s. All checked files remain "
                "saved for another try; this scan will be retried.",
                error,
            )
            raise

        try:
            _store_gist_retry_files(outcome.failed_files)
        except Exception as error:
            logger.error(
                "[Gist] Could not remove finished files from the retry list: %s. They remain "
                "saved and will be checked again; the saved results will not be lost.",
                error,
            )
        return saved

    result = await _run_scanner(
        label="Gist",
        search_fn=search_gists,
        source_name="gist",
        save_fn=save_gist_results,
        send_log_fn=_send_log_async,
        redis_client=redis_client,
    )
    if scan_state.get("must_retry_task"):
        raise RuntimeError("Gist retry state could not be saved or read; retry this scan")
    outcome = scan_state.get("outcome")
    if outcome and outcome.missing_token:
        return "Gist scan skipped: GitHub login token is missing or empty."
    if outcome and outcome.search_error:
        return f"Gist scan completed with errors: {outcome.search_error}. {result}"
    if outcome and outcome.failed_files:
        return f"{result} {len(outcome.failed_files)} Gist files remain saved for the next scan."
    return result
