#!/usr/bin/env python3
import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

VERSION = "M-PSW-107-101-01"

HEALTH = {
    0: "Healthy",
    1: "Warning",
    2: "Error",
}

STATE = {
    0: "None",
    1: "Initialisation",
    2: "Login",
    3: "Pre-use tests",
    4: "Self-test",
    5: "CPS Operation",
    6: "USB enable",
    7: "Bootload",
    8: "Battery mode",
    9: "Error",
}

OFF_TIMEOUT_LABEL = "Off (Timeout)"

ON_STATES = {1, 2, 3, 4, 5, 7, 9}

BASE_DIR = Path(__file__).resolve().parent


def parse_timestamp(ts: str) -> Optional[datetime]:
    """Normalize the timestamp to a naive datetime object."""
    if not ts:
        return None

    cleaned = ts.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[: -1]

    if "+" in cleaned:
        cleaned = cleaned.split("+", 1)[0]
    else:
        tz_sep = cleaned.rfind("-")
        if tz_sep != -1 and tz_sep > cleaned.find("T"):
            cleaned = cleaned[:tz_sep]

    if "." in cleaned:
        main, frac = cleaned.split(".", 1)
        frac = (frac + "000000")[:6]
        cleaned = f"{main}.{frac}"
    else:
        cleaned = cleaned + ".000000"

    try:
        return datetime.strptime(cleaned, "%Y-%m-%dT%H:%M:%S.%f")
    except ValueError:
        return None


def format_duration(seconds: float) -> str:
    rounded = int(round(seconds))
    hours = rounded // 3600
    minutes = (rounded % 3600) // 60
    secs = rounded % 60
    return f"{hours}h {minutes}m {secs}s"


def format_span(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    return format_duration(seconds)


def state_label(state: Optional[int]) -> str:
    if state is None:
        return "Unknown"
    return STATE.get(state, f"State({state})")


def health_label(health: Optional[int]) -> str:
    if health is None:
        return "Unknown"
    return HEALTH.get(health, f"Unknown({health})")


def describe_state(state: Optional[int], health: Optional[int], fallback: str = "Unknown (Unknown)") -> str:
    if state is None or health is None:
        return fallback
    return f"{state_label(state)} ({health_label(health)})"


def normalize_mac(mac: str) -> str:
    return "".join(ch for ch in mac if ch.isalnum()).upper()


def mac_with_colons(mac: str) -> str:
    segments = [mac[i : i + 2] for i in range(0, len(mac), 2)]
    return ":".join(segments)


def date_range(start_date: date, end_date: date) -> Iterable[date]:
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


def extract_health_state(raw: Optional[str]) -> Optional[Tuple[int, int, int, int, int]]:
    if not raw or not raw.startswith("060000"):
        return None

    payload = raw[6:16]
    if len(payload) < 10:
        return None

    info_cnt = int(payload[0:2], 16)
    warning_cnt = int(payload[2:4], 16)
    error_cnt = int(payload[4:6], 16)
    state = int(payload[6:8], 16)
    health = int(payload[8:10], 16)

    return health, state, info_cnt, warning_cnt, error_cnt


def gather_health_events(
    data_dir: str,
    mac_folder: str,
    target_mac: str,
    lookup_start: date,
    lookup_end: date,
) -> List[Tuple[datetime, int, int]]:
    events: List[Tuple[datetime, int, int]] = []

    for lookup_date in date_range(lookup_start, lookup_end):
        folder_name = f"{mac_folder}_{lookup_date:%Y_%m_%d}"
        folder_path = os.path.join(data_dir, folder_name)
        if not os.path.isdir(folder_path):
            continue

        for entry in sorted(os.listdir(folder_path)):
            if not entry.lower().endswith(".jsonl"):
                continue

            file_path = os.path.join(folder_path, entry)
            try:
                with open(file_path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue

                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        device_mac = data.get("DeviceMac")
                        if not device_mac or normalize_mac(device_mac) != target_mac:
                            continue

                        if data.get("RegisterGroup") != 6:
                            continue

                        raw = data.get("RawData")
                        parsed = extract_health_state(raw)
                        if parsed is None:
                            continue

                        ts = parse_timestamp(data.get("TimestampUtc", ""))
                        if ts is None:
                            continue

                        health, state, _, _, _ = parsed
                        events.append((ts, state, health))
            except (OSError, IOError):
                continue

    events.sort(key=lambda item: item[0])
    return events


def accumulate_durations(
    events: List[Tuple[datetime, int, int]],
    start_time: datetime,
    end_time: datetime,
    max_gap: timedelta,
    trace: bool = False,
) -> Tuple[float, float, float, float, float, List[str], List[str]]:
    total_on = total_off = healthy_on = error_on = other_on = 0.0
    last_event_time = start_time
    current_state: Optional[int] = None
    current_health: Optional[int] = None

    max_gap_seconds = max_gap.total_seconds()
    traces: List[str] = []
    state_changes: List[str] = []
    current_label = describe_state(None, None)
    label_start_time = start_time

    def record(message: str) -> None:
        if trace:
            traces.append(message)

    def log_state_transition(change_time: datetime, next_label: str, duration_seconds: float) -> None:
        nonlocal current_label, label_start_time
        duration_seconds = max(0.0, duration_seconds)
        entry = (
            f"{change_time.isoformat()}: {current_label:<30} -> {next_label:<30}  ({format_duration(duration_seconds)})"
        )
        state_changes.append(entry)
        current_label = next_label
        label_start_time = change_time

    best_previous: Optional[Tuple[datetime, int, int]] = None
    for ts, state, health in events:
        if ts <= start_time:
            if best_previous is None or ts > best_previous[0]:
                best_previous = (ts, state, health)
        else:
            break

    if best_previous and (start_time - best_previous[0]).total_seconds() <= max_gap_seconds:
        _, state, health = best_previous
        current_state = state
        current_health = health
        current_label = describe_state(state, health)
        label_start_time = start_time
        record(
            f"Carried {state_label(current_state)} (health {health_label(current_health)}) "
            f"from {best_previous[0].isoformat()} into the {start_time.isoformat()} window"
        )
    else:
        record("No prior state inside the threshold; starting window as OFF.")

    def consume_gap(gap_seconds: float) -> None:
        nonlocal total_on, total_off, healthy_on, error_on, other_on
        nonlocal current_state, current_health

        if gap_seconds <= 0:
            return

        continuation = min(gap_seconds, max_gap_seconds)
        off_extension = max(0.0, gap_seconds - max_gap_seconds)
        gap_start = last_event_time
        gap_end = gap_start + timedelta(seconds=gap_seconds)
        base_msg = (
            f"Gap {gap_start.isoformat()} -> {gap_end.isoformat()} "
            f"({format_span(gap_seconds)})"
        )

        if current_state in ON_STATES:
            total_on += continuation
            if current_health in (0, 1):
                healthy_on += continuation
            elif current_health == 2:
                error_on += continuation
            else:
                other_on += continuation
            on_end = gap_start + timedelta(seconds=continuation)
            msg = (
                f"{base_msg}: holding {state_label(current_state)} (health {health_label(current_health)}) "
                f"until {on_end.isoformat()} ({format_span(continuation)})"
            )
            if off_extension > 0:
                msg += f", then assumed OFF for {format_span(off_extension)}"
                timeout_time = gap_start + timedelta(seconds=max_gap_seconds)
                if current_label != OFF_TIMEOUT_LABEL:
                    log_state_transition(timeout_time, OFF_TIMEOUT_LABEL, max_gap_seconds)
                current_state = None
                current_health = None
            total_off += off_extension
        else:
            total_off += continuation
            msg = f"{base_msg}: assumed OFF for {format_span(continuation)}"
            if gap_seconds > max_gap_seconds and current_label != OFF_TIMEOUT_LABEL:
                timeout_time = gap_start + timedelta(seconds=max_gap_seconds)
                log_state_transition(timeout_time, OFF_TIMEOUT_LABEL, max_gap_seconds)
                current_state = None
                current_health = None
        record(msg)

    def record_event(ts: datetime, state: int, health: int, gap_seconds: float) -> None:
        if not trace:
            return
        msg = (
            f"Event at {ts.isoformat()}: {state_label(state)}, health {health_label(health)}"
        )
        if gap_seconds > 0:
            msg += f" after gap {format_span(gap_seconds)}"
        record(msg)

    for ts, state, health in events:
        if ts < start_time:
            continue
        if ts >= end_time:
            break

        gap = (ts - last_event_time).total_seconds()
        if gap > 0:
            consume_gap(gap)

        last_event_time = ts
        next_label = describe_state(state, health)
        if next_label != current_label:
            duration_seconds = (ts - label_start_time).total_seconds()
            log_state_transition(ts, next_label, duration_seconds)
        current_state = state
        current_health = health

        record_event(ts, state, health, gap)

    final_gap = (end_time - last_event_time).total_seconds()
    consume_gap(final_gap)

    return total_on, total_off, healthy_on, error_on, other_on, traces, state_changes


def prompt_if_needed(value: Optional[str], prompt_text: str) -> str:
    candidate = (value or "").strip()
    if candidate:
        return candidate

    prompt_label = prompt_text if prompt_text.endswith(": ") else prompt_text + ": "
    while True:
        try:
            entry = input(prompt_label).strip()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit(f"Input required for {prompt_label}")
        if entry:
            return entry
        print("Please provide a value.")


def prompt_yes_no(question: str, default: str = "N") -> bool:
    normalized_default = default.strip().lower()
    if normalized_default not in {"y", "n"}:
        normalized_default = "n"
    while True:
        try:
            answer = input(question).strip().lower()
        except (EOFError, KeyboardInterrupt):
            raise
        if not answer:
            answer = normalized_default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Please answer Y or N.")


def prompt_threshold(default: int) -> int:
    prompt_label = f"Gap threshold minutes (default {default}): "
    while True:
        try:
            entry = input(prompt_label).strip()
        except (EOFError, KeyboardInterrupt):
            raise
        if not entry:
            return default
        try:
            value = int(entry)
        except ValueError:
            print("Please enter a whole number.")
            continue
        if value <= 0:
            print("Value must be a positive integer.")
            continue
        return value
def print_summary(
    total_on: float,
    total_off: float,
    healthy_on: float,
    error_on: float,
    other_on: float,
    start_time: datetime,
    end_time: datetime,
    threshold_minutes: int,
    event_count: int,
    data_dir: str,
    mac_display: str,
    trace_log: Optional[List[str]] = None,
    state_changes: Optional[List[str]] = None,
) -> None:
    tracking_window = end_time - start_time
    used_seconds = total_on + total_off

    if total_on > 0:
        error_pct = (error_on / total_on) * 100
        healthy_pct = (healthy_on / total_on) * 100
    else:
        error_pct = healthy_pct = 0.0

    print(f"Data directory : {data_dir}")
    print(f"MAC            : {mac_display}")
    print(
        f"Time window    : {start_time.isoformat()} -> "
        f"{(end_time - timedelta(microseconds=1)).isoformat()}"
    )
    print(f"Gap threshold  : {threshold_minutes} minutes")
    print(f"Records parsed : {event_count}")
    print()
    print("Totals")
    print("------")
    print(f"Tracked window:  {format_duration(tracking_window.total_seconds()):>16}")
    print(f"Used window   :   {format_duration(used_seconds):>16}")
    print(f"  => On       : {format_duration(total_on):>17}")
    print(f"  => Off      : {format_duration(total_off):>17}")
    print()
    if healthy_on > 0 or error_on > 0 or other_on > 0:
        print(f"     * Healthy: {format_duration(healthy_on):>17}")
        print(f"     * Error  : {format_duration(error_on):>17}")
        if other_on > 0:
            print(f"     * Other  : {format_duration(other_on):>17}")
        print()
    else:
        print()
    print(f"     * Error %  : {error_pct:>10.1f}%")
    print(f"     * Healthy %: {healthy_pct:>10.1f}%")

    if state_changes:
        print("\nState change log")
        print("----------------")
        for entry in state_changes:
            print(entry)

    if trace_log:
        print("\nTrace details")
        print("-------------")
        for entry in trace_log:
            print(entry)


def execute_report(args: argparse.Namespace, interactive: bool) -> None:
    print(f"\n----------------------\n Software version : {VERSION} \n----------------------\n")

    mac_input = prompt_if_needed(
        args.mac, "MAC address (e.g. 54:F8:2A:FF:4E:05)"
    )
    start_input = prompt_if_needed(
        args.start, "Start date (YYYY_MM_DD or YYYY-MM-DD or ISO timestamp)"
    )
    end_input = prompt_if_needed(
        args.end, "End date (inclusive, same format as --start)"
    )

    norm_mac = normalize_mac(mac_input)
    if len(norm_mac) != 12:
        raise SystemExit("MAC address must resolve to 12 hexadecimal characters")

    start_dt, start_has_time = parse_datetime_input(start_input)
    end_dt, end_has_time = parse_datetime_input(end_input)
    if end_dt < start_dt:
        raise SystemExit("--end must be the same day or after --start")

    start_time = (
        start_dt
        if start_has_time
        else start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    )
    end_time = (
        end_dt + timedelta(microseconds=1)
        if end_has_time
        else end_dt + timedelta(days=1)
    )

    threshold_minutes = args.threshold_minutes
    if interactive:
        threshold_minutes = prompt_threshold(threshold_minutes)

    lookup_start_date = (start_time - timedelta(minutes=threshold_minutes)).date()
    lookup_end_date = end_dt.date()

    mac_folder = norm_mac
    display_mac = mac_with_colons(norm_mac)

    events = gather_health_events(
        data_dir=args.data_dir,
        mac_folder=mac_folder,
        target_mac=norm_mac,
        lookup_start=lookup_start_date,
        lookup_end=lookup_end_date,
    )

    (
        total_on,
        total_off,
        healthy_on,
        error_on,
        other_on,
        trace_log,
        state_changes,
    ) = accumulate_durations(
        events,
        start_time,
        end_time,
        timedelta(minutes=threshold_minutes),
        trace=args.trace,
    )

    print_summary(
        total_on,
        total_off,
        healthy_on,
        error_on,
        other_on,
        start_time,
        end_time,
        threshold_minutes,
        len(events),
        args.data_dir,
        display_mac,
        trace_log=trace_log if args.trace else None,
        state_changes=state_changes,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Summarize how much time a MAC spent on healthy/error/off states."
    )
    parser.add_argument(
        "--mac",
        help="Vehicle MAC address (colons/dashes optional).",
    )
    parser.add_argument(
        "--start",
        help="Start date/time (YYYY_MM_DD, YYYY-MM-DD, or ISO timestamp like 2026-03-18T13:00:00).",
    )
    parser.add_argument(
        "--end",
        help="End date/time (inclusive; same formats as --start).",
    )
    parser.add_argument(
        "--data-dir",
        default=str(BASE_DIR),
        help="Path to the folder containing MAC_YYYY_MM_DD subfolders.",
    )
    parser.add_argument(
        "--threshold-minutes",
        type=int,
        default=120,
        help="Minutes allowed between health reports before assuming the device went off.",
    )
    parser.add_argument(
        "--trace",
        action="store_true",
        help="Print each state change/gap so you can debug the timeline.",
    )
    return parser.parse_args()


def parse_datetime_input(value: str) -> Tuple[datetime, bool]:
    candidate = (value or "").strip()
    if not candidate:
        raise ValueError("date/time value must not be empty")

    for fmt in ("%Y_%m_%d", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(candidate, fmt)
            return dt, False
        except ValueError:
            continue

    normalized = candidate.replace("_", "-")
    normalized = normalized.replace(" ", "T", 1)
    dt = parse_timestamp(normalized)
    if dt is None:
        raise ValueError(
            "date/time must be YYYY_MM_DD, YYYY-MM-DD or ISO timestamp (e.g. 2026-03-18T13:00:00)"
        )

    has_time = ":" in normalized
    return dt, has_time

def main() -> None:
    args = parse_args()

    if args.threshold_minutes <= 0:
        raise SystemExit("threshold-minutes must be a positive integer")

    interactive = sys.stdin.isatty() and sys.stdout.isatty()

    while True:
        try:
            execute_report(args, interactive)
        except SystemExit as exc:
            print(f"\nTest failed: {exc}")
            if not interactive:
                raise
        except Exception as exc:
            print(f"\nUnexpected error: {exc}")
            if not interactive:
                raise
        else:
            print()

        if not interactive:
            return

        try:
            if not prompt_yes_no("Run another test? (Y/N): "):
                break
        except (EOFError, KeyboardInterrupt):
            break


if __name__ == "__main__":
    main()
