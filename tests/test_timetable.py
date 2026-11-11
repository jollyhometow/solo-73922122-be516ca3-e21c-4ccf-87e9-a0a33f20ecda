import unittest

from app.models import CorrectionRequest
from app.timetable import (
    MergeInputError,
    merge_tables,
    validate_timing,
)


def table(*stops):
    return {
        "trips": [
            {
                "trip_id": "T1",
                "stops": [
                    {
                        "stop_seq": index,
                        "station_name": f"S{index}",
                        "arrival_sec": arrival,
                        "departure_sec": departure,
                    }
                    for index, (arrival, departure) in enumerate(stops, start=1)
                ],
            }
        ]
    }["trips"]


def changes(payload):
    return CorrectionRequest.model_validate(payload).changes


BASE = table((0, 100), (200, 300))
CURRENT_CONCURRENT_DEP = table((0, 110), (200, 300))


class MergeRuleTests(unittest.TestCase):
    def test_current_equals_base_adopts_and_keeps_unsubmitted_fields(self):
        outcome = merge_tables(
            BASE,
            CURRENT_CONCURRENT_DEP,
            changes(
                {
                    "base_revision": 0,
                    "changes": [
                        {"trip_id": "T1", "stop_seq": 1, "arrival_sec": 5}
                    ],
                }
            ),
        )
        self.assertEqual(outcome.conflicts, [])
        self.assertEqual(
            [(s["arrival_sec"], s["departure_sec"]) for s in outcome.trips[0]["stops"]],
            [(5, 110), (200, 300)],
        )
        self.assertEqual(outcome.applied[0]["decision"], "applied")
        self.assertEqual(outcome.applied[0]["reason"], "current_equals_base")

    def test_correction_equal_to_base_keeps_concurrent_current(self):
        outcome = merge_tables(
            BASE,
            CURRENT_CONCURRENT_DEP,
            changes(
                {
                    "base_revision": 0,
                    "changes": [
                        {
                            "trip_id": "T1",
                            "stop_seq": 1,
                            "departure_sec": 100,
                        }
                    ],
                }
            ),
        )
        self.assertEqual(outcome.conflicts, [])
        self.assertEqual(outcome.trips[0]["stops"][0]["departure_sec"], 110)
        self.assertEqual(outcome.kept[0]["reason"], "correction_equals_base")

    def test_correction_equal_to_current_is_idempotent(self):
        outcome = merge_tables(
            BASE,
            CURRENT_CONCURRENT_DEP,
            changes(
                {
                    "base_revision": 0,
                    "changes": [
                        {
                            "trip_id": "T1",
                            "stop_seq": 1,
                            "departure_sec": 110,
                        }
                    ],
                }
            ),
        )
        self.assertEqual(outcome.conflicts, [])
        self.assertEqual(outcome.trips[0]["stops"][0]["departure_sec"], 110)
        self.assertEqual(outcome.kept[0]["reason"], "correction_equals_current")

    def test_both_sides_changed_differently_is_a_conflict(self):
        outcome = merge_tables(
            BASE,
            CURRENT_CONCURRENT_DEP,
            changes(
                {
                    "base_revision": 0,
                    "changes": [
                        {
                            "trip_id": "T1",
                            "stop_seq": 1,
                            "departure_sec": 180,
                        }
                    ],
                }
            ),
        )
        self.assertEqual(len(outcome.conflicts), 1)
        conflict = outcome.conflicts[0]
        self.assertEqual(conflict["field"], "departure_sec")
        self.assertEqual(
            (conflict["base_value"], conflict["current_value"],
             conflict["correction_value"]),
            (100, 110, 180),
        )

    def test_adoption_that_creates_inversion_is_detected(self):
        outcome = merge_tables(
            BASE,
            BASE,
            changes(
                {
                    "base_revision": 0,
                    "changes": [
                        {"trip_id": "T1", "stop_seq": 2, "arrival_sec": 50}
                    ],
                }
            ),
        )
        self.assertEqual(outcome.conflicts, [])
        self.assertEqual(len(outcome.violations), 1)
        violation = outcome.violations[0]
        self.assertEqual(violation["code"], "departure_after_next_arrival")
        self.assertEqual(
            (violation["stop_seq"], violation["next_stop_seq"]), (1, 2)
        )
        self.assertEqual(violation["field"], "departure_sec")

    def test_last_stop_only_checks_arrival_before_departure(self):
        bad_last = table((0, 100), (200, 250), (400, 350))
        violations = validate_timing(bad_last)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0]["code"], "arrival_after_departure")
        self.assertEqual(violations[0]["stop_seq"], 3)

    def test_valid_table_has_no_violations(self):
        self.assertEqual(validate_timing(BASE), [])

    def test_unknown_trip_and_stop_are_rejected(self):
        with self.assertRaises(MergeInputError) as raised:
            merge_tables(
                BASE,
                BASE,
                changes(
                    {
                        "base_revision": 0,
                        "changes": [
                            {"trip_id": "NOPE", "stop_seq": 1, "arrival_sec": 1}
                        ],
                    }
                ),
            )
        self.assertEqual(raised.exception.errors[0]["code"], "unknown_trip")

        with self.assertRaises(MergeInputError) as raised:
            merge_tables(
                BASE,
                BASE,
                changes(
                    {
                        "base_revision": 0,
                        "changes": [
                            {"trip_id": "T1", "stop_seq": 9, "arrival_sec": 1}
                        ],
                    }
                ),
            )
        self.assertEqual(raised.exception.errors[0]["code"], "unknown_stop")

    def test_duplicate_field_in_one_batch_is_rejected(self):
        with self.assertRaises(MergeInputError) as raised:
            merge_tables(
                BASE,
                BASE,
                changes(
                    {
                        "base_revision": 0,
                        "changes": [
                            {
                                "trip_id": "T1",
                                "stop_seq": 1,
                                "arrival_sec": 1,
                            },
                            {
                                "trip_id": "T1",
                                "stop_seq": 1,
                                "arrival_sec": 2,
                            },
                        ],
                    }
                ),
            )
        self.assertEqual(raised.exception.errors[0]["code"], "duplicate_field")


if __name__ == "__main__":
    unittest.main(verbosity=2)
