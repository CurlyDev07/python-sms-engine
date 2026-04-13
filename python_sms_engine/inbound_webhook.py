"""
Inbound SMS webhook client — delivers spooled inbound messages to Laravel.

Delivery contract:
  - POST JSON payload to SMS_ENGINE_INBOUND_WEBHOOK_URL
  - HTTP 200 = ACK → mark spool record delivered
  - Any non-200 or network error = retry with exponential backoff
  - Idempotency key is stable across retries — Laravel dedupes

Retry schedule (seconds between attempts):
  attempt 1 → immediate
  attempt 2 → 5s
  attempt 3 → 15s
  attempt 4 → 60s
  attempt 5+ → 300s (cap)
"""

import logging
import threading
import time
from typing import Dict, Optional

import urllib.request
import urllib.error
import json

from inbound_spool import InboundSpool

logger = logging.getLogger("python_sms_engine.inbound_webhook")

_BACKOFF_SCHEDULE = [0, 5, 15, 60, 300]  # seconds, last value is the cap


def _backoff_for(attempts: int) -> float:
    idx = min(attempts, len(_BACKOFF_SCHEDULE) - 1)
    return float(_BACKOFF_SCHEDULE[idx])


def _post_to_laravel(url: str, payload: Dict, timeout: float = 10.0) -> bool:
    """
    POST JSON payload to Laravel webhook URL.
    Returns True on HTTP 200, False on any error.
    """
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as exc:
        logger.warning(
            "INBOUND_WEBHOOK_HTTP_ERROR status=%s key=%s",
            exc.code, payload.get("idempotency_key"),
        )
        return False
    except Exception as exc:
        logger.warning(
            "INBOUND_WEBHOOK_ERROR error=%s key=%s",
            exc, payload.get("idempotency_key"),
        )
        return False


def deliver_one(
    spool: InboundSpool,
    record: Dict,
    webhook_url: str,
    max_attempts: int,
) -> bool:
    """
    Attempt delivery of a single spool record.
    Updates attempt counter. Returns True if delivered.
    """
    key = record["idempotency_key"]

    if record["attempts"] >= max_attempts:
        logger.error(
            "INBOUND_DELIVERY_ABANDONED key=%s attempts=%s",
            key, record["attempts"],
        )
        return False

    payload = {
        "idempotency_key": key,
        "runtime_sim_id":  record["runtime_sim_id"],
        "from":            record["from_number"],
        "message":         record["message"],
        "received_at":     record["received_at"],
    }

    spool.record_attempt(key)

    success = _post_to_laravel(webhook_url, payload)

    if success:
        spool.mark_delivered(key)
        logger.info(
            "INBOUND_DELIVERED key=%s sim=%s from=%s",
            key, record["runtime_sim_id"], record["from_number"],
        )
    else:
        logger.warning(
            "INBOUND_DELIVERY_FAILED key=%s attempt=%s next_retry_in=%ss",
            key, record["attempts"] + 1,
            _backoff_for(record["attempts"] + 1),
        )

    return success


class InboundRetryWorker(threading.Thread):
    """
    Background thread that wakes periodically and retries all pending
    spool records that are due for another attempt.

    Runs for the lifetime of the process (daemon=True).
    """

    def __init__(
        self,
        spool: InboundSpool,
        webhook_url: str,
        max_attempts: int = 10,
        poll_interval: float = 30.0,
    ) -> None:
        super().__init__(name="inbound-retry-worker", daemon=True)
        self._spool = spool
        self._webhook_url = webhook_url
        self._max_attempts = max_attempts
        self._poll_interval = poll_interval
        self._stop_event = threading.Event()

    def run(self) -> None:
        logger.info("INBOUND_RETRY_WORKER_STARTED poll_interval=%ss", self._poll_interval)

        # Drain any pending records left from before the last restart
        self._drain()

        while not self._stop_event.wait(self._poll_interval):
            self._drain()

    def _drain(self) -> None:
        if not self._webhook_url:
            return

        pending = self._spool.get_pending()
        if not pending:
            return

        logger.info("INBOUND_RETRY_DRAIN pending=%s", len(pending))

        for record in pending:
            if self._stop_event.is_set():
                break

            attempts = record["attempts"]
            backoff = _backoff_for(attempts)
            last = record.get("last_attempt_at")

            # Check if enough time has passed since last attempt
            if last and backoff > 0:
                import datetime
                try:
                    last_dt = datetime.datetime.fromisoformat(last)
                    elapsed = (
                        datetime.datetime.now(datetime.timezone.utc) - last_dt
                    ).total_seconds()
                    if elapsed < backoff:
                        continue  # not due yet
                except Exception:
                    pass

            deliver_one(
                spool=self._spool,
                record=record,
                webhook_url=self._webhook_url,
                max_attempts=self._max_attempts,
            )

    def stop(self) -> None:
        self._stop_event.set()
