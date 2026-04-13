#!/usr/bin/env python3
"""
Verification script — emit one synthetic inbound event and show the full
request/response cycle so you can validate the Laravel contract quickly.

Usage:
    python3 test_inbound_webhook.py
    python3 test_inbound_webhook.py --url http://127.0.0.1:8081/api/gateway/inbound
    python3 test_inbound_webhook.py --url http://... --phone +639171234567 --sim 515039219149367
"""

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone

# Allow running from the project root without installing
import os
sys.path.insert(0, os.path.dirname(__file__))

import urllib.request
import urllib.error


def post_payload(url: str, payload: dict, timeout: float = 10.0) -> dict:
    body_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body_bytes,
        headers={
            "Content-Type": "application/json",
            "Accept":       "application/json",
        },
        method="POST",
    )

    status = None
    raw_body = ""

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            raw_body = resp.read(4096).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            raw_body = exc.read(4096).decode("utf-8", errors="replace")
        except Exception:
            raw_body = ""

    return {"status": status, "body": raw_body}


def main() -> None:
    parser = argparse.ArgumentParser(description="Test inbound webhook contract")
    parser.add_argument(
        "--url",
        default=os.getenv("SMS_ENGINE_INBOUND_WEBHOOK_URL", "http://127.0.0.1:8081/api/gateway/inbound"),
        help="Laravel webhook URL",
    )
    parser.add_argument("--phone",   default="+639170000001", help="Synthetic sender phone")
    parser.add_argument("--sim",     default="515039219149367", help="Synthetic runtime_sim_id")
    parser.add_argument("--message", default="Test inbound from verification script", help="Message body")
    args = parser.parse_args()

    key = str(uuid.uuid4())
    payload = {
        "idempotency_key": key,
        "runtime_sim_id":  args.sim,
        "customer_phone":  args.phone,
        "message":         args.message,
        "received_at":     datetime.now(timezone.utc).isoformat(),
    }

    print("=" * 60)
    print("REQUEST")
    print(f"  URL:          {args.url}")
    print(f"  payload_keys: {list(payload.keys())}")
    print(f"  body:\n{json.dumps(payload, indent=4)}")
    print()

    result = post_payload(args.url, payload)
    status = result["status"]
    raw_body = result["body"]

    print("RESPONSE")
    print(f"  status: {status}")
    print(f"  body:   {raw_body[:500]}")
    print()

    # Parse ok field
    ok_value = None
    try:
        parsed = json.loads(raw_body)
        ok_value = bool(parsed.get("ok"))
    except Exception:
        ok_value = None

    is_2xx = status is not None and 200 <= status < 300
    success = is_2xx and ok_value is True

    print("DELIVERY DECISION")
    print(f"  HTTP 2xx:       {is_2xx}")
    print(f"  ok == true:     {ok_value}")
    print(f"  => would mark:  {'INBOUND_DELIVERED ✓' if success else 'INBOUND_DELIVERY_FAILED (retry)'}")

    if not success:
        if not is_2xx:
            print(f"\n  ✗ HTTP {status} — check Laravel route and token")
        elif ok_value is None:
            print("\n  ✗ Response body did not parse as JSON with 'ok' field")
        else:
            print("\n  ✗ Laravel returned ok != true — check Laravel handler logic")
        sys.exit(1)

    print(f"\n  idempotency_key: {key}")
    print("  Run this to verify Laravel persisted the row:")
    print(f"    SELECT * FROM inbound_messages WHERE idempotency_key = '{key}';")


if __name__ == "__main__":
    main()
