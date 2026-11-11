"""Pydantic models and input aliases for the timetable correction API.

Canonical request shapes:

* initial table: ``{"trips": [{"trip_id": ..., "stops": [...]}, ...]}``
* correction:    ``{"base_revision": 0, "changes": [...]}``

Common spellings (snake_case, camelCase, short names) are accepted as input
aliases.  Responses always use the canonical names.
"""

from __future__ import annotations

from typing import Annotated, List, Literal, Optional

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

# Timetable seconds must be real integers, never bools, floats or numeric
# strings.  Negative values are allowed so an operator may use a baseline
# other than midnight; only the documented ordering chain is constrained.
Seconds = Annotated[int, Field(strict=True)]

TRIP_ID_KEYS = (
    "trip_id", "tripId", "tripID", "id", "trip", "train_id", "trainId",
    "service_id", "serviceId", "vehicle_id", "vehicleId", "trip_no",
    "tripNo", "trip_number", "tripNumber",
)
STOP_SEQ_KEYS = (
    "stop_seq", "stopSeq", "stop_sequence", "stopSequence", "seq",
    "sequence", "stop_index", "stopIndex", "stop_number", "stopNumber",
    "stop_no", "stopNo", "stop_order", "stopOrder", "order", "index",
    "stop",
)
STATION_KEYS = (
    "station_name", "stationName", "station", "name", "stop_name",
    "stopName",
)
ARRIVAL_KEYS = (
    "arrival_sec", "arrivalSec", "arrival_seconds", "arrivalSeconds",
    "arrival_time", "arrivalTime", "arrival", "arr_sec", "arrSec",
    "arr", "arr_time", "arrTime",
)
DEPARTURE_KEYS = (
    "departure_sec", "departureSec", "departure_seconds", "departureSeconds",
    "departure_time", "departureTime", "departure", "dep_sec", "depSec",
    "dep", "dep_time", "depTime",
)
TRIP_LIST_KEYS = (
    "trips", "trip_list", "tripList", "trains", "services", "vehicles",
)
CHANGE_LIST_KEYS = (
    "changes", "corrections", "modifications", "updates", "edits", "patches",
    "change_list", "changeList",
)
BASE_REVISION_KEYS = (
    "base_revision", "baseRevision", "base_rev", "baseRev",
    "based_on_revision", "basedOnRevision", "base_revision_id",
    "baseRevisionId", "base_version", "baseVersion", "base", "revision",
)

TRIP_ID_ALIASES = AliasChoices(*TRIP_ID_KEYS)
STOP_SEQ_ALIASES = AliasChoices(*STOP_SEQ_KEYS)
STATION_ALIASES = AliasChoices(*STATION_KEYS)
ARRIVAL_ALIASES = AliasChoices(*ARRIVAL_KEYS)
DEPARTURE_ALIASES = AliasChoices(*DEPARTURE_KEYS)
TRIPS_ALIASES = AliasChoices(*TRIP_LIST_KEYS)
CHANGES_ALIASES = AliasChoices(*CHANGE_LIST_KEYS)
BASE_REVISION_ALIASES = AliasChoices(*BASE_REVISION_KEYS)


def _has_any(data: dict, keys: tuple[str, ...]) -> bool:
    return any(key in data for key in keys)


def _stops_of(container: dict) -> list | None:
    for key in ("stops", "stop_list", "stopList", "stations", "stopSequence"):
        value = container.get(key)
        if isinstance(value, list):
            return value
    return None


def _expand_stop_groups(items: list) -> list:
    """Flatten ``{"trip_id": ..., "stops": [...]}`` change groups."""

    expanded: list = []
    for item in items:
        if not isinstance(item, dict):
            expanded.append(item)
            continue
        stops = _stops_of(item)
        if stops is not None and not _has_any(item, STOP_SEQ_KEYS):
            for stop in stops:
                merged = {
                    key: item[key] for key in TRIP_ID_KEYS if key in item
                }
                if isinstance(stop, dict):
                    merged.update(stop)
                expanded.append(merged)
        else:
            expanded.append(item)
    return expanded


def _flatten_nested_trips(trips: list) -> list:
    """Turn a correction body shaped like the initial table into changes."""

    flattened: list = []
    for trip in trips:
        if not isinstance(trip, dict):
            continue
        trip_keys = {key: trip[key] for key in TRIP_ID_KEYS if key in trip}
        stops = _stops_of(trip)
        if stops is not None:
            for stop in stops:
                merged = dict(trip_keys)
                if isinstance(stop, dict):
                    merged.update(stop)
                flattened.append(merged)
        elif _has_any(trip, STOP_SEQ_KEYS):
            flattened.append(trip)
    return flattened


def parse_revision(value: object) -> int:
    """Parse a revision identifier such as ``0``, ``"0"`` or ``"rev-0"``."""

    if isinstance(value, bool):
        raise ValueError("revision must be a non-negative integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text.isdigit():
            return int(text)
        if text.startswith("-") and text[1:].isdigit():
            return int(text)
        for prefix in ("revision-", "revision ", "revision", "rev-", "rev", "r"):
            if text.startswith(prefix):
                rest = text[len(prefix):]
                if rest.isdigit() or (rest.startswith("-") and rest[1:].isdigit()):
                    return int(rest)
                break
    raise ValueError("revision must be a non-negative integer")


def _non_blank(value: str, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    return value


class StopIn(BaseModel):
    """One stop of the initial timetable."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    stop_seq: Optional[Annotated[int, Field(strict=True, ge=1)]] = Field(
        default=None,
        validation_alias=STOP_SEQ_ALIASES,
        description="1-based fixed stop order; if omitted, list order is used.",
    )
    station_name: str = Field(
        ..., min_length=1, validation_alias=STATION_ALIASES
    )
    arrival_sec: Seconds = Field(..., validation_alias=ARRIVAL_ALIASES)
    departure_sec: Seconds = Field(..., validation_alias=DEPARTURE_ALIASES)

    @field_validator("station_name")
    @classmethod
    def _check_station_name(cls, value: str) -> str:
        return _non_blank(value, "station_name")


class TripIn(BaseModel):
    """One trip of the initial timetable."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    trip_id: str = Field(
        ..., min_length=1, validation_alias=TRIP_ID_ALIASES
    )
    stops: List[StopIn] = Field(
        ...,
        min_length=2,
        max_length=12,
        validation_alias=AliasChoices(
            "stops", "stop_list", "stopList", "stations", "stopSequence"
        ),
    )

    @field_validator("trip_id")
    @classmethod
    def _check_trip_id(cls, value: str) -> str:
        return _non_blank(value, "trip_id")

    @model_validator(mode="after")
    def _fix_stop_order(self) -> "TripIn":
        for position, stop in enumerate(self.stops, start=1):
            if stop.stop_seq is None:
                stop.stop_seq = position
            elif stop.stop_seq != position:
                raise ValueError(
                    "stop_seq must follow the fixed 1-based list order "
                    f"(expected {position}, got {stop.stop_seq})"
                )
        return self


class TimetableCreateRequest(BaseModel):
    """Payload for creating the one initial timetable."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    trips: List[TripIn] = Field(
        ..., min_length=1, max_length=20, validation_alias=TRIPS_ALIASES
    )

    @model_validator(mode="before")
    @classmethod
    def _unwrap_containers(cls, data: object) -> object:
        # Accept a bare list, or {"timetable": {"trips": [...]}}.
        if isinstance(data, list):
            return {"trips": data}
        if isinstance(data, dict):
            for key in ("timetable", "table", "payload", "data"):
                inner = data.get(key)
                if "trips" in data:
                    continue
                if isinstance(inner, list):
                    return {"trips": inner}
                if not isinstance(inner, dict):
                    continue
                for alias in (
                    "trips", "trip_list", "tripList", "trains", "services",
                    "vehicles",
                ):
                    if alias in inner:
                        merged = dict(inner)
                        merged["trips"] = inner[alias]
                        return merged
        return data

    @model_validator(mode="after")
    def _check_unique_trip_ids(self) -> "TimetableCreateRequest":
        ids = [trip.trip_id for trip in self.trips]
        if len(set(ids)) != len(ids):
            raise ValueError("trip_id values must be unique")
        return self


class CorrectionChange(BaseModel):
    """Changes for one stop, addressed by trip id + 1-based stop sequence."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    trip_id: str = Field(
        ..., min_length=1, validation_alias=TRIP_ID_ALIASES
    )
    stop_seq: Annotated[int, Field(strict=True, ge=1)] = Field(
        ..., validation_alias=STOP_SEQ_ALIASES
    )
    arrival_sec: Optional[Seconds] = Field(
        default=None, validation_alias=ARRIVAL_ALIASES
    )
    departure_sec: Optional[Seconds] = Field(
        default=None, validation_alias=DEPARTURE_ALIASES
    )

    @field_validator("trip_id")
    @classmethod
    def _check_trip_id(cls, value: str) -> str:
        return _non_blank(value, "trip_id")

    @model_validator(mode="after")
    def _require_a_field(self) -> "CorrectionChange":
        if self.arrival_sec is None and self.departure_sec is None:
            raise ValueError(
                "a change must submit arrival_sec and/or departure_sec"
            )
        return self


class CorrectionRequest(BaseModel):
    """A three-way merge request against a known base revision."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    base_revision: int = Field(
        ..., ge=0, validation_alias=BASE_REVISION_ALIASES
    )
    changes: List[CorrectionChange] = Field(
        ..., min_length=1, max_length=2000, validation_alias=CHANGES_ALIASES
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_change_shapes(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data

        if _has_any(data, CHANGE_LIST_KEYS):
            key = next(key for key in CHANGE_LIST_KEYS if key in data)
            items = data[key]
            if isinstance(items, list):
                expanded = _expand_stop_groups(items)
                if expanded != items:
                    return {**data, "changes": expanded}
            return data

        # Single change sent at the top level:
        # {"base_revision": 0, "trip_id": "T1", "stop_seq": 1, ...}
        if _has_any(data, TRIP_ID_KEYS) and _has_any(data, STOP_SEQ_KEYS):
            change = {
                key: value
                for key, value in data.items()
                if key not in BASE_REVISION_KEYS
            }
            return {**data, "changes": [change]}

        # Correction body shaped like the initial table:
        # {"base_revision": 0, "trips": [{"trip_id": ..., "stops": [...]}]}
        for key in TRIP_LIST_KEYS:
            trips = data.get(key)
            if isinstance(trips, list):
                return {**data, "changes": _flatten_nested_trips(trips)}
        return data

    @field_validator("base_revision", mode="before")
    @classmethod
    def _parse_base_revision(cls, value: object) -> int:
        return parse_revision(value)


class StopOut(BaseModel):
    stop_seq: int
    station_name: str
    arrival_sec: int
    departure_sec: int


class TripOut(BaseModel):
    trip_id: str
    stops: List[StopOut]


class TableSnapshot(BaseModel):
    """A complete immutable revision."""

    revision: int
    base_revision: Optional[int] = None
    parent_revision: Optional[int] = None
    created_at: str
    trips: List[TripOut]


class TableResponse(TableSnapshot):
    """A snapshot plus a nested copy for clients that expect ``timetable``."""

    timetable: TableSnapshot


class CreateResponse(TableResponse):
    status: Literal["ok"] = "ok"
    message: str
    created: bool


class FieldDecision(BaseModel):
    trip_id: str
    stop_seq: int
    field: Literal["arrival_sec", "departure_sec"]
    base_value: int
    current_value: int
    correction_value: int
    new_value: int
    decision: Literal["applied", "kept_current"]
    reason: str


class MergeResponse(TableResponse):
    status: Literal["ok"] = "ok"
    message: str
    merged_against_revision: int
    changes_applied: List[FieldDecision]
    changes_kept: List[FieldDecision]
