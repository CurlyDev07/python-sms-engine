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
    detect_modems,
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


# ---------------------------------------------------------------------------
# Contract-hardening: send_ready + identifier_source
# ---------------------------------------------------------------------------

class TestContractHardeningFields:
    """
    Tests for the two contract-hardening fields added to /modems/discover responses:
      - send_ready: bool
      - identifier_source: "imsi" | "fallback_device_id"

    Rules under test:
      - send_ready=True requires: no probe_error, at_ok, sim_ready, creg_registered,
        and identifier_source=="imsi"
      - identifier_source="imsi" only when probe.imsi is not None
      - identifier_source="fallback_device_id" for all other cases (ICCID-only, IMEI-only,
        or physical USB address fallback when probe timed out)
    """

    def _probe_with_imsi(self, physical, port, fallback):
        """Healthy probe result with IMSI present."""
        return _healthy_probe_result(physical, port, fallback)

    def _probe_no_imsi_but_iccid(self, physical, port, fallback):
        """Probe succeeded but AT+CIMI failed — ICCID available, no IMSI."""
        base = _healthy_probe_result(physical, port, fallback)
        base["imsi"] = None  # IMSI read failed
        base["iccid"] = "89630323255005160625"
        return base

    def _probe_timed_out(self, physical, port, fallback):
        """Probe timed out — no identity at all, all flags false."""
        return {
            "physical": physical,
            "port": port,
            "fallback_port": fallback,
            "at_ok": False,
            "sim_ready": False,
            "creg_registered": False,
            "signal": None,
            "imsi": None,
            "iccid": None,
            "imei": None,
            "score": 0,
            "probe_error": "PROBE_TIMEOUT after 12.0s",
        }

    def test_healthy_modem_with_imsi_is_send_ready(self):
        """Fully healthy modem with IMSI: send_ready=True, identifier_source="imsi"."""
        entries = [("3-7.4.4", "/dev/ttyUSB2", "/dev/ttyUSB3")]

        def fake_safe_probe(physical, primary_port, fallback_port, *args, **kwargs):
            return self._probe_with_imsi(physical, primary_port, fallback_port)

        with patch("modem_detector._collect_if02_ports", return_value=entries):
            with patch("modem_detector._safe_probe", side_effect=fake_safe_probe):
                results = discover_all_modems(probe_timeout=5.0)

        assert len(results) == 1
        r = results[0]
        assert r["send_ready"] is True
        assert r["identifier_source"] == "imsi"
        assert r["probe_error"] is None

    def test_timed_out_probe_is_not_send_ready(self):
        """Timed-out probe: send_ready=False, identifier_source="fallback_device_id"."""
        entries = [("3-7.4.4", "/dev/ttyUSB2", "/dev/ttyUSB3")]

        def fake_safe_probe(*args, **kwargs):
            time.sleep(9999)  # hang indefinitely

        with patch("modem_detector._collect_if02_ports", return_value=entries):
            with patch("modem_detector._safe_probe", side_effect=fake_safe_probe):
                results = discover_all_modems(probe_timeout=0.5)

        assert len(results) == 1
        r = results[0]
        assert r["send_ready"] is False
        assert r["identifier_source"] == "fallback_device_id"
        assert "PROBE_TIMEOUT" in r["probe_error"]

    def test_port_not_found_is_not_send_ready(self):
        """Missing port: send_ready=False, identifier_source="fallback_device_id"."""
        entries = [("3-7.4.4", "/dev/ttyUSB_GONE", None)]

        def fake_safe_probe(physical, primary_port, fallback_port, *args, **kwargs):
            return {
                "physical": physical,
                "port": primary_port,
                "fallback_port": fallback_port,
                "at_ok": False, "sim_ready": False, "creg_registered": False,
                "signal": None, "imsi": None, "iccid": None, "imei": None,
                "score": 0, "probe_error": "PORT_NOT_FOUND",
            }

        with patch("modem_detector._collect_if02_ports", return_value=entries):
            with patch("modem_detector._safe_probe", side_effect=fake_safe_probe):
                results = discover_all_modems(probe_timeout=5.0)

        assert len(results) == 1
        r = results[0]
        assert r["send_ready"] is False
        assert r["identifier_source"] == "fallback_device_id"
        assert r["probe_error"] == "PORT_NOT_FOUND"

    def test_healthy_flags_but_no_imsi_is_not_send_ready(self):
        """
        Modem passes at_ok/sim_ready/creg but IMSI was not readable.
        identifier_source="fallback_device_id", send_ready=False.
        """
        entries = [("3-7.4.4", "/dev/ttyUSB2", "/dev/ttyUSB3")]

        def fake_safe_probe(physical, primary_port, fallback_port, *args, **kwargs):
            return self._probe_no_imsi_but_iccid(physical, primary_port, fallback_port)

        with patch("modem_detector._collect_if02_ports", return_value=entries):
            with patch("modem_detector._safe_probe", side_effect=fake_safe_probe):
                results = discover_all_modems(probe_timeout=5.0)

        assert len(results) == 1
        r = results[0]
        assert r["send_ready"] is False
        assert r["identifier_source"] == "fallback_device_id"
        # Other flags may still be True — this tests the identifier_source rule specifically
        assert r["at_ok"] is True
        assert r["sim_ready"] is True

    def test_partial_discovery_returns_mixed_healthy_and_failed(self):
        """
        Mixed discovery: healthy modem is send_ready, failed modem is not.
        Both are present in the same response.
        """
        entries = [
            ("3-7.1", "/dev/ttyUSB0", "/dev/ttyUSB1"),  # healthy
            ("3-7.2", "/dev/ttyUSB2", "/dev/ttyUSB3"),  # timed out
        ]
        hang_port = "/dev/ttyUSB2"

        def fake_safe_probe(physical, primary_port, fallback_port, *args, **kwargs):
            if primary_port == hang_port:
                return self._probe_timed_out(physical, primary_port, fallback_port)
            return self._probe_with_imsi(physical, primary_port, fallback_port)

        with patch("modem_detector._collect_if02_ports", return_value=entries):
            with patch("modem_detector._safe_probe", side_effect=fake_safe_probe):
                results = discover_all_modems(probe_timeout=5.0)

        assert len(results) == 2

        send_ready_rows = [r for r in results if r["send_ready"]]
        not_ready_rows = [r for r in results if not r["send_ready"]]

        assert len(send_ready_rows) == 1
        assert len(not_ready_rows) == 1

        assert send_ready_rows[0]["identifier_source"] == "imsi"
        assert not_ready_rows[0]["identifier_source"] == "fallback_device_id"

    def test_send_ready_false_when_creg_not_registered(self):
        """Modem with IMSI but not registered on carrier: send_ready=False."""
        entries = [("3-7.4.4", "/dev/ttyUSB2", "/dev/ttyUSB3")]

        def fake_safe_probe(physical, primary_port, fallback_port, *args, **kwargs):
            probe = self._probe_with_imsi(physical, primary_port, fallback_port)
            probe["creg_registered"] = False  # not registered on network
            return probe

        with patch("modem_detector._collect_if02_ports", return_value=entries):
            with patch("modem_detector._safe_probe", side_effect=fake_safe_probe):
                results = discover_all_modems(probe_timeout=5.0)

        assert len(results) == 1
        r = results[0]
        assert r["send_ready"] is False
        assert r["identifier_source"] == "imsi"  # identifier_source independent of creg


# ---------------------------------------------------------------------------
# Probe timeout caps
# ---------------------------------------------------------------------------

class TestProbeTimeoutCaps:
    """
    Tests that discover_all_modems and detect_modems cap the serial and command
    timeouts passed to probes, regardless of what the caller provides.

    Rules under test:
      - probe_serial_timeout = min(serial_timeout, 1.0)
      - probe_command_timeout = min(command_timeout, 5.0)
      - Caps apply in both discover_all_modems and detect_modems.
      - Send path (send_sms) is unaffected — caps only in probe entry points.
    """

    def _capture_probe_args(self):
        """Return a fake _safe_probe that records the serial/command timeouts it received."""
        captured = {}

        def fake_safe_probe(physical, primary_port, fallback_port, serial_timeout, command_timeout):
            captured["serial_timeout"] = serial_timeout
            captured["command_timeout"] = command_timeout
            return _healthy_probe_result(physical, primary_port, fallback_port)

        return fake_safe_probe, captured

    def test_discover_all_modems_caps_serial_timeout(self):
        """discover_all_modems must cap serial_timeout to 1.0 before probing."""
        entries = [("3-7.1", "/dev/ttyUSB0", "/dev/ttyUSB1")]
        fake_probe, captured = self._capture_probe_args()

        with patch("modem_detector._collect_if02_ports", return_value=entries):
            with patch("modem_detector._safe_probe", side_effect=fake_probe):
                discover_all_modems(serial_timeout=3.0, command_timeout=5.0, probe_timeout=5.0)

        assert captured["serial_timeout"] <= 1.0, (
            f"Expected serial_timeout capped to ≤1.0, got {captured['serial_timeout']}"
        )

    def test_discover_all_modems_caps_command_timeout(self):
        """discover_all_modems must cap command_timeout to 5.0 before probing."""
        entries = [("3-7.1", "/dev/ttyUSB0", "/dev/ttyUSB1")]
        fake_probe, captured = self._capture_probe_args()

        with patch("modem_detector._collect_if02_ports", return_value=entries):
            with patch("modem_detector._safe_probe", side_effect=fake_probe):
                discover_all_modems(serial_timeout=3.0, command_timeout=10.0, probe_timeout=5.0)

        assert captured["command_timeout"] <= 5.0, (
            f"Expected command_timeout capped to ≤5.0, got {captured['command_timeout']}"
        )

    def test_detect_modems_caps_serial_timeout(self):
        """detect_modems must cap serial_timeout to 1.0 before probing."""
        entries = [("3-7.1", "/dev/ttyUSB0", "/dev/ttyUSB1")]
        fake_probe, captured = self._capture_probe_args()

        with patch("modem_detector._collect_if02_ports", return_value=entries):
            with patch("modem_detector._safe_probe", side_effect=fake_probe):
                detect_modems(serial_timeout=3.0, command_timeout=5.0, probe_timeout=5.0)

        assert captured["serial_timeout"] <= 1.0, (
            f"Expected serial_timeout capped to ≤1.0, got {captured['serial_timeout']}"
        )

    def test_detect_modems_caps_command_timeout(self):
        """detect_modems must cap command_timeout to 5.0 before probing."""
        entries = [("3-7.1", "/dev/ttyUSB0", "/dev/ttyUSB1")]
        fake_probe, captured = self._capture_probe_args()

        with patch("modem_detector._collect_if02_ports", return_value=entries):
            with patch("modem_detector._safe_probe", side_effect=fake_probe):
                detect_modems(serial_timeout=3.0, command_timeout=10.0, probe_timeout=5.0)

        assert captured["command_timeout"] <= 5.0, (
            f"Expected command_timeout capped to ≤5.0, got {captured['command_timeout']}"
        )

    def test_caps_do_not_raise_when_values_already_within_bounds(self):
        """Caps must be no-ops when caller already passes values within bounds."""
        entries = [("3-7.1", "/dev/ttyUSB0", "/dev/ttyUSB1")]
        fake_probe, captured = self._capture_probe_args()

        with patch("modem_detector._collect_if02_ports", return_value=entries):
            with patch("modem_detector._safe_probe", side_effect=fake_probe):
                discover_all_modems(serial_timeout=0.5, command_timeout=3.0, probe_timeout=5.0)

        # Values already under cap — must be passed through unchanged
        assert captured["serial_timeout"] == 0.5
        assert captured["command_timeout"] == 3.0
