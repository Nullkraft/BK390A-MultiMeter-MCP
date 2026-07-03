#!/usr/bin/env python3
# /// script
# dependencies = [
#   "mcp[cli]",
#   "pyserial",
# ]
# ///

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
import threading
import time
from typing import Any

import serial
from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hardware-mcp-common"))
from hardware_ports import SERIAL_GLOB_PATTERNS, list_serial_ports, prefer_by_marker, sort_by_marker


DEFAULT_PORT = "/dev/serial/by-id/usb-Prolific_Technology_Inc._USB-Serial_Controller_D-if00-port0"
BK390A_PORT_MARKERS = (
    "usb-Prolific_Technology_Inc._USB-Serial_Controller",
    "Prolific",
)

RS232_UNREACHABLE_HINT = (
    "Check that the BK 390A is on, the serial cable is connected, and the "
    "front-panel RS232 button has been pressed."
)


def load_parser():
    parser_path = Path(__file__).resolve().with_name("bk390a_parser.py")
    spec = importlib.util.spec_from_file_location("bk390a_parser", parser_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load parser module from %s" % parser_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


parser = load_parser()
mcp = FastMCP("bk390a", json_response=True)


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_candidate_ports() -> list[str]:
    return sort_by_marker(list_serial_ports(), BK390A_PORT_MARKERS)


def resolve_default_port() -> str:
    preferred_port = prefer_by_marker(list_candidate_ports(), BK390A_PORT_MARKERS)
    if preferred_port is not None:
        return preferred_port
    return DEFAULT_PORT


def resolve_port(port: str) -> str:
    if port == DEFAULT_PORT:
        return resolve_default_port()
    return port


def meter_timeout(port: str) -> TimeoutError:
    return TimeoutError(
        "timed out waiting for data from %s. %s" % (port, RS232_UNREACHABLE_HINT)
    )


class FrameCache:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._frames: deque[dict[str, Any]] = deque(maxlen=16)
        self._active_port: str | None = None
        self._thread: threading.Thread | None = None
        self._seq = 0
        self._last_error: str | None = None

    def ensure_port(self, port: str) -> None:
        with self._condition:
            if self._active_port != port:
                self._active_port = port
                self._frames.clear()
                self._last_error = None
                self._condition.notify_all()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._reader_loop, daemon=True)
                self._thread.start()

    def latest(self, port: str, timeout_s: float) -> dict[str, Any]:
        self.ensure_port(port)
        deadline = time.monotonic() + timeout_s
        with self._condition:
            while True:
                frame = self._latest_for_port_locked(port)
                if frame is not None:
                    return dict(frame)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if self._last_error is not None:
                        raise TimeoutError(self._last_error)
                    raise meter_timeout(port)
                self._condition.wait(timeout=remaining)

    def next_after(self, port: str, after_seq: int, timeout_s: float) -> dict[str, Any]:
        self.ensure_port(port)
        deadline = time.monotonic() + timeout_s
        with self._condition:
            while True:
                frame = self._latest_for_port_locked(port)
                if frame is not None and frame["seq"] > after_seq:
                    return dict(frame)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if self._last_error is not None:
                        raise TimeoutError(self._last_error)
                    raise meter_timeout(port)
                self._condition.wait(timeout=remaining)

    def _latest_for_port_locked(self, port: str) -> dict[str, Any] | None:
        for frame in reversed(self._frames):
            if frame["port"] == port:
                return frame
        return None

    def _reader_loop(self) -> None:
        handle: serial.Serial | None = None
        current_port: str | None = None
        while True:
            with self._condition:
                target_port = self._active_port
            if target_port is None:
                time.sleep(0.1)
                continue

            if current_port != target_port:
                if handle is not None:
                    handle.close()
                    handle = None
                try:
                    handle = serial.Serial(
                        port=target_port,
                        baudrate=2400,
                        bytesize=serial.SEVENBITS,
                        parity=serial.PARITY_ODD,
                        stopbits=serial.STOPBITS_ONE,
                        timeout=1.0,
                    )
                    current_port = target_port
                    with self._condition:
                        self._last_error = None
                        self._condition.notify_all()
                except Exception as exc:
                    current_port = None
                    with self._condition:
                        self._last_error = str(exc)
                        self._condition.notify_all()
                    time.sleep(0.5)
                    continue

            try:
                assert handle is not None
                raw = handle.readline()
                if not raw:
                    continue
                text = raw.decode("ascii", errors="strict").strip()
                if not text:
                    continue
                parsed = parser.parse_frame(text)
            except (serial.SerialException, OSError, UnicodeError) as exc:
                with self._condition:
                    self._last_error = str(exc)
                    self._condition.notify_all()
                handle = None
                current_port = None
                time.sleep(0.5)
                continue
            except Exception as exc:
                with self._condition:
                    self._last_error = str(exc)
                    self._condition.notify_all()
                continue

            frame = {
                "seq": self._seq + 1,
                "port": current_port,
                "arrival_timestamp": utc_timestamp(),
                "raw_frame": text,
                "measurement": parsed,
            }
            self._seq += 1
            with self._condition:
                self._frames.append(frame)
                self._condition.notify_all()


frame_cache = FrameCache()
snapshot_cache_lock = threading.Lock()
snapshot_cache: dict[str, dict[str, Any]] = {}
expected_profiles: dict[str, dict[str, Any]] = {}


def age_seconds(timestamp: str) -> float:
    now = datetime.now(timezone.utc)
    arrival = datetime.fromisoformat(timestamp)
    return (now - arrival).total_seconds()


def meter_setup_from_measurement(measurement: dict[str, Any]) -> dict[str, Any]:
    status = measurement.get("status", {})
    option1 = measurement.get("option1", {})
    option2 = measurement.get("option2", {})
    setup = {
        "function": measurement.get("function"),
        "mode": measurement.get("mode"),
        "range_code": measurement.get("range_code"),
        "range_label": measurement.get("range_label"),
        "unit": measurement.get("unit"),
        "decimals": measurement.get("decimals"),
        "dc": option2.get("dc"),
        "ac": option2.get("ac"),
        "auto": option2.get("auto"),
        "apo": option2.get("apo"),
        "pmax": option1.get("pmax"),
        "pmin": option1.get("pmin"),
        "vahz": option1.get("vahz"),
        "battery_low": status.get("battery_low"),
        "overflow": status.get("overflow"),
    }
    return {key: value for key, value in setup.items() if value is not None}


def nested_matches(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        for key, value in expected.items():
            if key not in actual or not nested_matches(actual[key], value):
                return False
        return True
    return actual == expected


def build_snapshot(port: str, frame: dict[str, Any]) -> dict[str, Any]:
    measurement = frame["measurement"]
    setup = meter_setup_from_measurement(measurement)
    expected_profile = expected_profiles.get(port)
    snapshot = {
        "timestamp": utc_timestamp(),
        "port": port,
        "raw_frame": frame["raw_frame"],
        "arrival_timestamp": frame["arrival_timestamp"],
        "age_s": age_seconds(frame["arrival_timestamp"]),
        "setup": setup,
        "measurement": measurement,
        "cache": {
            "state": "fresh",
            "reason": "snapshot_refresh",
            "timestamp": utc_timestamp(),
        },
    }
    if expected_profile is not None:
        comparison_target = snapshot if "setup" in expected_profile else setup
        snapshot["expected_profile"] = expected_profile
        snapshot["expected_match"] = nested_matches(comparison_target, expected_profile)
    return snapshot


def store_snapshot(port: str, snapshot: dict[str, Any]) -> None:
    with snapshot_cache_lock:
        snapshot_cache[port] = dict(snapshot)


def cached_snapshot(port: str) -> dict[str, Any] | None:
    with snapshot_cache_lock:
        snapshot = snapshot_cache.get(port)
        return None if snapshot is None else dict(snapshot)


def read_one_frame(port: str, timeout_s: float) -> tuple[str, dict[str, Any], dict[str, Any]]:
    if port == DEFAULT_PORT:
        return read_one_frame_from_candidates(timeout_s)
    port = resolve_port(port)
    frame = frame_cache.latest(port, timeout_s)
    return frame["raw_frame"], frame["measurement"], frame


def read_one_frame_from_candidates(timeout_s: float) -> tuple[str, dict[str, Any], dict[str, Any]]:
    candidates = list_candidate_ports()
    if not candidates:
        raise RuntimeError("no candidate serial ports found")

    failures = []
    for candidate in candidates:
        try:
            frame = frame_cache.latest(candidate, timeout_s)
            return frame["raw_frame"], frame["measurement"], frame
        except Exception as exc:
            failures.append({"port": candidate, "error": str(exc)})

    raise RuntimeError(
        "no BK390A meter stream found; checked=%s" % (
            ", ".join("%s (%s)" % (item["port"], item["error"]) for item in failures)
        )
    )


@mcp.tool()
def list_ports() -> dict[str, Any]:
    """List likely serial devices for the BK Precision 390A."""
    return {
        "timestamp": utc_timestamp(),
        "ports": list_candidate_ports(),
        "resolved_default_port": resolve_default_port(),
        "patterns": list(SERIAL_GLOB_PATTERNS),
    }


@mcp.tool(name="list-tools")
async def user_list_tools() -> dict[str, Any]:
    """List the user-facing tools exposed by this server."""
    tools = await mcp.list_tools()
    tool_rows = [
        {
            "name": tool.name,
            "title": tool.title,
            "description": tool.description,
            "input_schema": tool.inputSchema,  # How to call a tool: Used by AI
        }
        for tool in tools
    ]
    lines = ["Available tools:"]
    for tool in tool_rows:
        title = f" ({tool['title']})" if tool["title"] else ""
        description = tool["description"] or "No description."
        lines.append(f"- {tool['name']}{title}: {description}")
    return {
        "server": mcp.name,
        "tool_count": len(tool_rows),
        "tools": tool_rows,
        "display_text": "\n".join(lines),
    }


@mcp.tool()
def bk390a_snapshot_refresh(
    port: str = DEFAULT_PORT,
    timeout_s: float = 2.0,
    require_stable: bool = True,
    max_frames: int = 6,
) -> dict[str, Any]:
    """Read a meter frame, derive the current setup, and store it in the cache."""
    read_result = bk390a_read(
        port=port,
        timeout_s=timeout_s,
        require_stable=require_stable,
        max_frames=max_frames,
    )
    port = read_result["port"]
    frame = {
        "raw_frame": read_result["raw_frame"],
        "arrival_timestamp": read_result["arrival_timestamp"],
        "measurement": read_result["measurement"],
    }
    snapshot = build_snapshot(port, frame)
    snapshot["stable"] = read_result["stable"]
    snapshot["frames_seen"] = read_result["frames_seen"]
    store_snapshot(port, snapshot)
    return {
        "timestamp": utc_timestamp(),
        "port": port,
        "source": "meter",
        "snapshot": snapshot,
    }


@mcp.tool()
def bk390a_snapshot_get(
    port: str = DEFAULT_PORT,
    timeout_s: float = 2.0,
    require_stable: bool = True,
    max_frames: int = 6,
) -> dict[str, Any]:
    """Return the cached meter setup, refreshing from the meter if no cache exists."""
    snapshot = None if port == DEFAULT_PORT else cached_snapshot(resolve_port(port))
    if snapshot is not None:
        return {
            "timestamp": utc_timestamp(),
            "port": snapshot["port"],
            "source": "cache",
            "snapshot": snapshot,
        }
    return bk390a_snapshot_refresh(
        port=port,
        timeout_s=timeout_s,
        require_stable=require_stable,
        max_frames=max_frames,
    )


@mcp.tool()
def bk390a_snapshot_cached(port: str = DEFAULT_PORT) -> dict[str, Any]:
    """Return the cached meter setup without touching the serial port."""
    port = resolve_port(port)
    snapshot = cached_snapshot(port)
    return {
        "timestamp": utc_timestamp(),
        "port": port,
        "found": snapshot is not None,
        "snapshot": snapshot,
    }


@mcp.tool()
def bk390a_apply_profile(
    profile: dict[str, Any],
    port: str = DEFAULT_PORT,
    refresh_after: bool = False,
    timeout_s: float = 2.0,
) -> dict[str, Any]:
    """Store the expected meter setup for later cached verification.

    The BK 390A serial protocol is output-only, so this records what the
    technician says the front panel should be; it does not command the meter.
    """
    port = resolve_port(port)
    with snapshot_cache_lock:
        expected_profiles[port] = dict(profile)

    if refresh_after:
        return bk390a_snapshot_refresh(
            port=port,
            timeout_s=timeout_s,
            require_stable=True,
            max_frames=6,
        )

    snapshot = cached_snapshot(port)
    if snapshot is not None:
        comparison_target = snapshot if "setup" in profile else snapshot.get("setup", {})
        snapshot["expected_profile"] = dict(profile)
        snapshot["expected_match"] = nested_matches(comparison_target, profile)
        store_snapshot(port, snapshot)

    return {
        "timestamp": utc_timestamp(),
        "port": port,
        "status": "recorded",
        "note": "BK 390A setup is controlled from the meter front panel, not over serial.",
        "expected_profile": dict(profile),
        "snapshot": snapshot,
    }


@mcp.tool()
def bk390a_read(
    port: str = DEFAULT_PORT,
    timeout_s: float = 2.0,
    require_stable: bool = True,
    max_frames: int = 6,
) -> dict[str, Any]:
    """Read and decode a measurement frame from the BK Precision 390A."""
    raw_frame, parsed, frame = read_one_frame(port, timeout_s)
    port = frame["port"]
    frames_seen = 1

    if not require_stable:
        now = datetime.now(timezone.utc)
        arrival = datetime.fromisoformat(frame["arrival_timestamp"])
        return {
            "timestamp": utc_timestamp(),
            "port": port,
            "stable": False,
            "frames_seen": frames_seen,
            "raw_frame": raw_frame,
            "measurement": parsed,
            "arrival_timestamp": frame["arrival_timestamp"],
            "age_s": (now - arrival).total_seconds(),
        }

    previous_seq = frame["seq"]
    previous_raw = raw_frame

    for _ in range(1, max_frames):
        next_frame = frame_cache.next_after(port, previous_seq, timeout_s)
        frames_seen += 1
        if next_frame["raw_frame"] == previous_raw:
            now = datetime.now(timezone.utc)
            arrival = datetime.fromisoformat(next_frame["arrival_timestamp"])
            return {
                "timestamp": utc_timestamp(),
                "port": port,
                "stable": True,
                "frames_seen": frames_seen,
                "raw_frame": next_frame["raw_frame"],
                "measurement": next_frame["measurement"],
                "arrival_timestamp": next_frame["arrival_timestamp"],
                "age_s": (now - arrival).total_seconds(),
            }
        previous_seq = next_frame["seq"]
        previous_raw = next_frame["raw_frame"]

    now = datetime.now(timezone.utc)
    arrival = datetime.fromisoformat(frame["arrival_timestamp"])
    return {
        "timestamp": utc_timestamp(),
        "port": port,
        "stable": False,
        "frames_seen": frames_seen,
        "raw_frame": raw_frame,
        "measurement": parsed,
        "arrival_timestamp": frame["arrival_timestamp"],
        "age_s": (now - arrival).total_seconds(),
    }


@mcp.tool()
def bk390a_verify_connection(port: str = DEFAULT_PORT, timeout_s: float = 2.0) -> dict[str, Any]:
    """Verify meter communication by requiring a parseable BK Precision 390A frame."""
    raw_frame, parsed, frame = read_one_frame(port, timeout_s)
    port = frame["port"]
    now = datetime.now(timezone.utc)
    arrival = datetime.fromisoformat(frame["arrival_timestamp"])
    return {
        "timestamp": utc_timestamp(),
        "port": port,
        "verified": True,
        "method": "valid_bk390a_frame",
        "raw_frame": raw_frame,
        "evidence": {
            "frame_length": len(raw_frame),
            "function_code": parsed.get("function_code"),
            "function": parsed.get("function"),
            "mode": parsed.get("mode"),
            "range_code": parsed.get("range_code"),
            "range_label": parsed.get("range_label"),
            "unit": parsed.get("unit"),
            "display": parsed.get("display"),
            "summary": parsed.get("summary"),
        },
        "measurement": parsed,
        "arrival_timestamp": frame["arrival_timestamp"],
        "age_s": (now - arrival).total_seconds(),
    }


@mcp.tool()
def bk390a_read_raw_frame(port: str = DEFAULT_PORT, timeout_s: float = 2.0) -> dict[str, Any]:
    """Read one raw meter frame and decode it."""
    raw_frame, parsed, frame = read_one_frame(port, timeout_s)
    port = frame["port"]
    now = datetime.now(timezone.utc)
    arrival = datetime.fromisoformat(frame["arrival_timestamp"])
    return {
        "timestamp": utc_timestamp(),
        "port": port,
        "raw_frame": raw_frame,
        "measurement": parsed,
        "arrival_timestamp": frame["arrival_timestamp"],
        "age_s": (now - arrival).total_seconds(),
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
