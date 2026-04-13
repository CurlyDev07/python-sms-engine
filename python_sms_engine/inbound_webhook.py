"""
Inbound SMS webhook client — delivers spooled inbound messages to Laravel.

Delivery contract:
  - POST JSON payload to SMS_ENGINE_INBOUND_WEBHOOK_URL
  - Success = HTTP 2xx AND response JSON has ok === true
  - Any non-2xx, network error, or ok !== true = retry with exponential backoff
  - Idempotency key is stable across retries — Laravel dedupes

Payload keys sent to Laravel:
  - idempotency_key  (string UUID)
  - runtime_sim_id   (string IMSI / runtime id)
  - customer_phone   (string E.164)
  - message          (string)
  - received_at      (ISO8601 string)

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


def _post_to_laravel(url: str, payload: Dict, timeout: float = 10.0) -> Dict:
    """
    POST JSON payload to Laravel webhook URL.

    Returns dict with:
      success (bool)  — True only if HTTP 2xx AND response JSON ok == True
      status  (int)   — HTTP status code, or None on network error
      ok      (bool)  — value of response JSON 'ok' field, or None if unparseable
      body    (str)   — first 200 chars of raw response body
    """
    key = payload.get("idempotency_key", "?")
    body_bytes = json.dumps(payload).encode("utf-8")

    logger.info(
        "INBOUND_WEBHOOK_REQUEST key=%s payload_keys=%s",
        key, list(payload.keys()),
    )

    req = urllib.request.Request(
        url,
        data=body_bytes,
        headers={
            "Content-Type": "application/json",
            "Accept":       "application/json",
        },
        method="POST",
    )

    status: Optional[int] = None
    raw_body = ""

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            raw_body = resp.read(512).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            raw_body = exc.read(512).decode("utf-8", errors="replace")
        except Exception:
            raw_body = ""
        logger.warning(
            "INBOUND_WEBHOOK_HTTP_ERROR key=%s status=%s body=%s",
            key, status, raw_body[:200],
        )
        return {"success": False, "status": status, "ok": None, "body": raw_body}
    except Exception as exc:
        logger.warning(
            "INBOUND_WEBHOOK_ERROR key=%s error=%s",
            key, exc,
        )
        return {"success": False, "status": None, "ok": None, "body": ""}

    # Parse response body — must have ok == true to count as delivered
    ok_value: Optional[bool] = None
    try:
        parsed = json.loads(raw_body)
        ok_value = bool(parsed.get("ok"))
    except Exception:
        ok_value = None

    is_2xx = 200 <= status < 300
    success = is_2xx and ok_value is True

    logger.info(
        "INBOUND_WEBHOOK_RESPONSE key=%s status=%s ok=%s",
        key, status, ok_value,
    )

    if is_2xx and not success:
        logger.warning(
            "INBOUND_ACK_FALSE key=%s status=%s body=%s",
            key, status, raw_body[:200],
        )

    return {"success": success, "status": status, "ok": ok_value, "body": raw_body}


def deliver_one(
    spool: InboundSpool,
    record: Dict,
    webhook_url: str,
    max_attempts: int,
) -> bool:
    """
    Attempt delivery of a single spool record.
    Updates attempt counter. Returns True only if Laravel ACKed (ok: true).
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
        "customer_phone":  record["from_number"],
        "message":         record["message"],
        "received_at":     record["received_at"],
    }

    spool.record_attempt(key)

    result = _post_to_laravel(webhook_url, payload)

    if result["success"]:
        spool.mark_delivered(key)
        logger.info(
            "INBOUND_DELIVERED key=%s sim=%s from=%s",
            key, record["runtime_sim_id"], record["from_number"],
        )
    else:
        logger.warning(
            "INBOUND_DELIVERY_FAILED key=%s attempt=%s next_retry_in=%ss status=%s ok=%s",
            key,
            record["attempts"] + 1,
            _backoff_for(record["attempts"] + 1),
            result["status"],
            result["ok"],
        )

    return result["success"]


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
