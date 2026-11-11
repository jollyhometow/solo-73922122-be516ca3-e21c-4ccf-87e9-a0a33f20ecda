"""Timetable validation and three-way merge rules.

The merge is deliberately field-oriented:

* an unsubmitted field keeps the value from the latest revision;
* if ``current == base`` the submitted correction is adopted;
* if ``correction == base`` or ``correction == current`` the current value is
  kept (the submission made no effective change);
* otherwise the submission and concurrent edits disagree -> conflict.

The whole batch is accepted or rejected as one unit.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .models import CorrectionChange, TimetableCreateRequest

FIELDS = ("arrival_sec", "departure_sec")


class MergeInputError(Exception):
    """The batch addresses trips/stops/fields that cannot be merged at all."""

    def __init__(self, message: str, errors: Sequence[dict]):
        super().__init__(message)
        self.message = message
        self.errors = list(errors)


@dataclass
class MergeResult:
    trips: list[dict]
    applied: list[dict] = field(default_factory=list)
    kept: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    violations: list[dict] = field(default_factory=list)


def canonical_trips(request: TimetableCreateRequest) -> list[dict]:
    """Return the canonical JSON-ready table for a validated request."""

    trips: list[dict] = []
    for trip in request.trips:
        stops = [
            {
                "stop_seq": stop.stop_seq,
                "station_name": stop.station_name,
                "arrival_sec": stop.arrival_sec,
                "departure_sec": stop.departure_sec,
            }
            for stop in trip.stops
        ]
        trips.append({"trip_id": trip.trip_id, "stops": stops})
    return trips


def _trip_map(trips: Sequence[dict]) -> dict[str, dict]:
    return {trip["trip_id"]: trip for trip in trips}


def _stop_map(trip: dict) -> dict[int, dict]:
    return {stop["stop_seq"]: stop for stop in trip["stops"]}


def validate_timing(trips: Sequence[dict]) -> list[dict]:
    """Return every ordering violation in the table.

    The rule is ``arrival <= departure <= next arrival`` at every stop, and
    only ``arrival <= departure`` at the final stop.  Violations point both at
    the offending field and at the adjacent stops.
    """

    violations: list[dict] = []
    for trip in trips:
        trip_id = trip["trip_id"]
        stops = trip["stops"]
        for index, stop in enumerate(stops):
            seq = stop["stop_seq"]
            arrival = stop["arrival_sec"]
            departure = stop["departure_sec"]
            if arrival > departure:
                violations.append(
                    {
                        "code": "arrival_after_departure",
                        "trip_id": trip_id,
                        "stop_seq": seq,
                        "station_name": stop["station_name"],
                        "field": "arrival_sec",
                        "arrival_sec": arrival,
                        "departure_sec": departure,
                        "message": (
                            f"车次 {trip_id} 停站 {seq}（{stop['station_name']}）"
                            f"到站 {arrival} 秒晚于发车 {departure} 秒"
                        ),
                    }
                )
            if index + 1 < len(stops):
                next_stop = stops[index + 1]
                if departure > next_stop["arrival_sec"]:
                    violations.append(
                        {
                            "code": "departure_after_next_arrival",
                            "trip_id": trip_id,
                            "stop_seq": seq,
                            "next_stop_seq": next_stop["stop_seq"],
                            "station_name": stop["station_name"],
                            "next_station_name": next_stop["station_name"],
                            "field": "departure_sec",
                            "next_field": "arrival_sec",
                            "departure_sec": departure,
                            "next_arrival_sec": next_stop["arrival_sec"],
                            "message": (
                                f"车次 {trip_id} 停站 {seq}（{stop['station_name']}）"
                                f"发车 {departure} 秒晚于下一停站 "
                                f"{next_stop['stop_seq']}（{next_stop['station_name']}）"
                                f"到站 {next_stop['arrival_sec']} 秒"
                            ),
                        }
                    )
    return violations


def merge_tables(
    base_trips: Sequence[dict],
    current_trips: Sequence[dict],
    changes: Iterable[CorrectionChange],
) -> MergeResult:
    """Apply the documented three-way rules against latest + base tables."""

    base = _trip_map(base_trips)
    current = _trip_map(current_trips)

    changes = list(changes)
    submitted: set[tuple[str, int, str]] = set()
    errors: list[dict] = []

    # Reject ambiguous batches before doing any merge work.
    for change in changes:
        for name in FIELDS:
            value = getattr(change, name)
            if value is None:
                continue
            key = (change.trip_id, change.stop_seq, name)
            if key in submitted:
                errors.append(
                    {
                        "code": "duplicate_field",
                        "trip_id": change.trip_id,
                        "stop_seq": change.stop_seq,
                        "field": name,
                        "message": (
                            f"车次 {change.trip_id} 停站 {change.stop_seq} 的 "
                            f"{name} 在一批更正中被重复提交"
                        ),
                    }
                )
            submitted.add(key)

    for change in changes:
        trip = current.get(change.trip_id)
        if trip is None:
            errors.append(
                {
                    "code": "unknown_trip",
                    "trip_id": change.trip_id,
                    "message": f"车次 {change.trip_id} 不存在",
                }
            )
            continue
        stops = _stop_map(trip)
        if change.stop_seq not in stops:
            errors.append(
                {
                    "code": "unknown_stop",
                    "trip_id": change.trip_id,
                    "stop_seq": change.stop_seq,
                    "stop_count": len(trip["stops"]),
                    "message": (
                        f"车次 {change.trip_id} 没有序号为 "
                        f"{change.stop_seq} 的停站（共 {len(trip['stops'])} 站）"
                    ),
                }
            )
            continue
        base_trip = base.get(change.trip_id)
        base_stop = (
            _stop_map(base_trip).get(change.stop_seq)
            if base_trip is not None
            else None
        )
        if base_stop is None:
            errors.append(
                {
                    "code": "base_structure_missing",
                    "trip_id": change.trip_id,
                    "stop_seq": change.stop_seq,
                    "message": "基准修订中缺少该车次/停站",
                }
            )

    if errors:
        raise MergeInputError("更正批次包含无法处理的字段", errors)

    proposed = copy.deepcopy(list(current_trips))
    proposed_map = _trip_map(proposed)
    applied: list[dict] = []
    kept: list[dict] = []
    conflicts: list[dict] = []

    for change in changes:
        trip = proposed_map[change.trip_id]
        stop = _stop_map(trip)[change.stop_seq]
        base_stop = _stop_map(base[change.trip_id])[change.stop_seq]

        for name in FIELDS:
            value = getattr(change, name)
            if value is None:
                continue
            base_value = base_stop[name]
            current_value = stop[name]

            if current_value == base_value:
                stop[name] = value
                applied.append(
                    {
                        "trip_id": change.trip_id,
                        "stop_seq": change.stop_seq,
                        "field": name,
                        "base_value": base_value,
                        "current_value": current_value,
                        "correction_value": value,
                        "new_value": value,
                        "decision": "applied",
                        "reason": "current_equals_base",
                    }
                )
            elif value == base_value:
                kept.append(
                    {
                        "trip_id": change.trip_id,
                        "stop_seq": change.stop_seq,
                        "field": name,
                        "base_value": base_value,
                        "current_value": current_value,
                        "correction_value": value,
                        "new_value": current_value,
                        "decision": "kept_current",
                        "reason": "correction_equals_base",
                    }
                )
            elif value == current_value:
                kept.append(
                    {
                        "trip_id": change.trip_id,
                        "stop_seq": change.stop_seq,
                        "field": name,
                        "base_value": base_value,
                        "current_value": current_value,
                        "correction_value": value,
                        "new_value": current_value,
                        "decision": "kept_current",
                        "reason": "correction_equals_current",
                    }
                )
            else:
                conflicts.append(
                    {
                        "trip_id": change.trip_id,
                        "stop_seq": change.stop_seq,
                        "field": name,
                        "base_value": base_value,
                        "current_value": current_value,
                        "correction_value": value,
                        "message": (
                            f"车次 {change.trip_id} 停站 {change.stop_seq} 的 "
                            f"{name} 冲突：基准 {base_value}，当前 {current_value}，"
                            f"更正 {value}"
                        ),
                    }
                )

    violations = validate_timing(proposed)
    return MergeResult(
        trips=proposed,
        applied=applied,
        kept=kept,
        conflicts=conflicts,
        violations=violations,
    )
