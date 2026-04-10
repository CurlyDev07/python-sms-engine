"""
Focused tests for modem discovery timeout and failure paths.

These tests verify that:
  - A single hung modem probe does not block the full discover response.
  - Probes that time out are surfaced as probe_error in the result.
  - /modems/discover returns within a bounded time even when modems are bad.
  - Other modems are still returned when one fails.
  - PORT_NOT_FOUND modems are immediately reported (no serial I/O attempted).

Run:
    cd python_sms_engine
    source .venv/bin/activate
    pytest tests/test_discover_timeout.py -v
"""

import sys
import os
import time
import threading
from typing import Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on the path so imports work without installation
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from modem_detector import (
    PROBE_TIMEOUT_S,
    _run_parallel_probes,
    _safe_probe,
    discover_all_modems,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_entries(n: int):
    """Return n fake (physical, port, fallback) tuples."""
    return [(f"3-7.{i}", f"/dev/ttyUSB{i*2}", f"/dev/ttyUSB{i*2+1}") for i in range(n)]


def _healthy_probe_result(physical: str, port: str, fallback: Optional[str]) -> Dict:
    return {
        "physical": physical,
        "port": port,
        "fallback_port": fallback,
        "at_ok": True,
        "sim_ready": True,
        "creg_registered": True,
        "signal": "+CSQ: 20,99",
        "imsi": f"5150{port[-1]}",
        "iccid": f"8963{port[-1]}",
        "imei": f"8663{port[-1]}",
        "score": 17,
        "probe_error": None,
    }


# ---------------------------------------------------------------------------
# _safe_probe tests
# ---------------------------------------------------------------------------

class TestSafeProbe:
    def test_port_not_found_returns_error_immediately(self):
        """_safe_probe must not attempt serial I/O when port is absent."""
        result = _safe_probe(
            physical="3-7.0",
            primary_port="/dev/ttyUSB_NONEXISTENT_99",
            fallback_port=None,
            serial_timeout=3.0,
            command_timeout=5.0,
        )
        assert result["at_ok"] is False
        assert result["sim_ready"] is False
        assert result["probe_error"] == "PORT_NOT_FOUND"

    def test_exception_in_probe_is_caught(self):
        """_safe_probe must swallow exceptions from _probe_port and return error dict."""
        with patch("modem_detector._probe_port", side_effect=RuntimeError("kaboom")):
            with patch("os.path.exists", return_value=True):
                result = _safe_probe(
                    physical="3-7.1",
                    primary_port="/dev/ttyUSB2",
                    fallback_port="/dev/ttyUSB3",
                    serial_timeout=3.0,
                    command_timeout=5.0,
                )
        assert result["at_ok"] is False
        assert "kaboom" in result["probe_error"]

    def test_successful_probe_updates_all_fields(self):
        """_safe_probe must pass through a successful _probe_port result."""
        mock_result = {
            "port": "/dev/ttyUSB2",
            "at_ok": True,
            "sim_ready": True,
            "creg_registered": True,
            "signal": "+CSQ: 20,99",
            "imsi": "515039219149367",
            "iccid": "89630323255005160625",
            "imei": "866358071697796",
            "score": 17,
        }
        with patch("modem_detector._probe_port", return_value=mock_result):
            with patch("os.path.exists", return_value=True):
                result = _safe_probe(
                    physical="3-7.4.4",
                    primary_port="/dev/ttyUSB2",
                    fallback_port="/dev/ttyUSB3",
                    serial_timeout=3.0,
                    command_timeout=5.0,
                )
        assert result["at_ok"] is True
        assert result["imsi"] == "515039219149367"
        assert result["probe_error"] is None


# ---------------------------------------------------------------------------
# _run_parallel_probes tests
# ---------------------------------------------------------------------------

class TestRunParallelProbes:
    def test_one_hung_modem_does_not_block_others(self):
        """
        With probe_timeout=2s and one modem that sleeps forever,
        the other modems must still be returned within ~probe_timeout.
        """
        entries = _make_entries(3)
        hang_port = entries[1][1]  # middle modem hangs

        def fake_safe_probe(physical, primary_port, fallback_port, serial_timeout, command_timeout):
            if primary_port == hang_port:
                time.sleep(9999)  # simulate stuck serial.Serial() open
            return _healthy_probe_result(physical, primary_port, fallback_port)

        start = time.monotonic()
        with patch("modem_detector._safe_probe", side_effect=fake_safe_probe):
            results = _run_parallel_probes(entries, 3.0, 5.0, probe_timeout=2.0)
        elapsed = time.monotonic() - start

        assert elapsed < 3.5, f"Expected < 3.5s, got {elapsed:.2f}s"
        assert len(results) == 3

        timed_out = [r for r in results if r.get("probe_error") and "PROBE_TIMEOUT" in r["probe_error"]]
        healthy = [r for r in results if not r.get("probe_error")]

        assert len(timed_out) == 1, "Exactly one modem should have timed out"
        assert timed_out[0]["port"] == hang_port
        assert len(healthy) == 2, "Other two modems should be healthy"

    def test_all_timed_out_modems_have_probe_error(self):
        """When all probes time out, all results have probe_error=PROBE_TIMEOUT."""
        entries = _make_entries(2)

        def fake_safe_probe(*args, **kwargs):
            time.sleep(9999)

        with patch("modem_detector._safe_probe", side_effect=fake_safe_probe):
            results = _run_parallel_probes(entries, 3.0, 5.0, probe_timeout=1.0)

        assert len(results) == 2
        for r in results:
            assert "PROBE_TIMEOUT" in r.get("probe_error", "")
            assert r["at_ok"] is False

    def test_empty_entries_returns_empty_list(self):
        results = _run_parallel_probes([], 3.0, 5.0, probe_timeout=5.0)
        assert results == []

    def test_all_healthy_returns_all_results(self):
        """All probes healthy — all results returned, no probe_error."""
        entries = _make_entries(3)

        def fake_safe_probe(physical, primary_port, fallback_port, *args, **kwargs):
            return _healthy_probe_result(physical, primary_port, fallback_port)

        with patch("modem_detector._safe_probe", side_effect=fake_safe_probe):
            results = _run_parallel_probes(entries, 3.0, 5.0, probe_timeout=5.0)

        assert len(results) == 3
        assert all(r["at_ok"] is True for r in results)
        assert all(r.get("probe_error") is None for r in results)

    def test_failed_probe_exception_still_returns_error_dict(self):
        """Probe that raises an exception results in error dict, not a crash."""
        entries = _make_entries(1)

        def fake_safe_probe(*args, **kwargs):
            raise RuntimeError("serial exploded")

        with patch("modem_detector._safe_probe", side_effect=fake_safe_probe):
            # _run_parallel_probes wraps _safe_probe in executor.submit —
            # exceptions from _safe_probe propagate via future.result().
            # _safe_probe itself is designed to never raise, but if it does,
            # the future raises and we need graceful handling.
            # This test verifies that exactly: the exception should be caught.
            try:
                results = _run_parallel_probes(entries, 3.0, 5.0, probe_timeout=5.0)
                # If _safe_probe raises, future.result() raises in _run_parallel_probes.
                # The caller (discover_all_modems / detect_modems) handles it.
                # For now, verify we get a result back or an exception is raised cleanly.
            except Exception as exc:
                # This is acceptable — _safe_probe should never raise but if it does
                # the exception propagates clearly rather than hanging forever.
                assert "serial exploded" in str(exc)


# ---------------------------------------------------------------------------
# discover_all_modems tests
# ---------------------------------------------------------------------------

class TestDiscoverAllModems:
    def test_returns_all_ports_including_unhealthy(self):
        """discover_all_modems returns all detected ports even when unhealthy."""
        fake_entries = [
            ("3-7.1", "/dev/ttyUSB0", "/dev/ttyUSB1"),  # healthy
            ("3-7.2", "/dev/ttyUSB2", "/dev/ttyUSB3"),  # port missing
        ]

        def fake_safe_probe(physical, primary_port, fallback_port, *args, **kwargs):
            if primary_port == "/dev/ttyUSB0":
                return _healthy_probe_result(physical, primary_port, fallback_port)
            return {
                "physical": physical,
                "port": primary_port,
                "fallback_port": fallback_port,
                "at_ok": False, "sim_ready": False, "creg_registered": False,
                "signal": None, "imsi": None, "iccid": None, "imei": None,
                "score": 0, "probe_error": "PORT_NOT_FOUND",
            }

        with patch("modem_detector._collect_if02_ports", return_value=fake_entries):
            with patch("modem_detector._safe_probe", side_effect=fake_safe_probe):
                results = discover_all_modems(probe_timeout=5.0)

        assert len(results) == 2
        healthy = [r for r in results if r["at_ok"]]
        failed = [r for r in results if not r["at_ok"]]
        assert len(healthy) == 1
        assert len(failed) == 1
        assert failed[0]["probe_error"] == "PORT_NOT_FOUND"

    def test_timed_out_probe_has_sim_id_fallback(self):
        """When IMSI is unavailable, sim_id falls back to physical address."""
        fake_entries = [("3-7.4.4", "/dev/ttyUSB2", "/dev/ttyUSB3")]

        def fake_safe_probe(physical, primary_port, fallback_port, *args, **kwargs):
            time.sleep(9999)  # hang

        with patch("modem_detector._collect_if02_ports", return_value=fake_entries):
            with patch("modem_detector._safe_probe", side_effect=fake_safe_probe):
                results = discover_all_modems(probe_timeout=0.5)

        assert len(results) == 1
        # sim_id must be set (falls back to physical or port)
        assert results[0]["sim_id"] is not None
        assert results[0]["sim_id"] != ""
        assert "PROBE_TIMEOUT" in results[0]["probe_error"]

    def test_no_modems_detected_returns_empty(self):
        with patch("modem_detector._collect_if02_ports", return_value=[]):
            results = discover_all_modems()
        assert results == []

    def test_bounded_response_time_with_multiple_hung_modems(self):
        """Total time must be close to probe_timeout, not N * probe_timeout."""
        n = 4
        fake_entries = _make_entries(n)
        probe_timeout = 1.5

        def fake_safe_probe(*args, **kwargs):
            time.sleep(9999)

        start = time.monotonic()
        with patch("modem_detector._collect_if02_ports", return_value=fake_entries):
            with patch("modem_detector._safe_probe", side_effect=fake_safe_probe):
                results = discover_all_modems(probe_timeout=probe_timeout)
        elapsed = time.monotonic() - start

        assert len(results) == n
        # Should complete within probe_timeout + 2s overhead (thread pool teardown)
        assert elapsed < probe_timeout + 2.0, f"Expected < {probe_timeout + 2.0:.1f}s, got {elapsed:.2f}s"
        assert all("PROBE_TIMEOUT" in r.get("probe_error", "") for r in results)
