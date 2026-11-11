import concurrent.futures
import tempfile
import unittest

from fastapi.testclient import TestClient

from app.examples import CORRECTION_A, CORRECTION_B, INITIAL_TIMETABLE
from app.main import create_app


def correction(trip_id, stop_seq, **values):
    change = {"trip_id": trip_id, "stop_seq": stop_seq}
    change.update(values)
    return {"base_revision": 0, "changes": [change]}


def make_client(db_path):
    return TestClient(create_app(db_path))


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = f"{self.tmp.name}/timetable.db"
        self.client = make_client(self.db_path)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def reset_client(self):
        self.client.__exit__(None, None, None)
        self.client = make_client(self.db_path)
        self.client.__enter__()

    # -- initial table ---------------------------------------------------

    def test_health_docs_and_openapi(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        self.assertEqual(self.client.get("/docs").status_code, 200)
        openapi = self.client.get("/openapi.json").json()
        for path in (
            "/timetables",
            "/timetables/current",
            "/corrections",
            "/revisions",
            "/revisions/{revision}",
            "/examples/inversion",
        ):
            self.assertIn(path, openapi["paths"])
        examples = openapi["paths"]["/corrections"]["post"]["requestBody"][
            "content"
        ]["application/json"]["examples"]
        self.assertIn("valid_departure_change", examples)
        self.assertIn("valid_arrival_change", examples)

    def test_create_returns_revision_zero_and_full_table(self):
        response = self.client.post("/timetables", json=INITIAL_TIMETABLE)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["revision"], 0)
        self.assertTrue(body["created"])
        self.assertEqual(body["trips"], INITIAL_TIMETABLE["trips"])
        self.assertEqual(body["timetable"]["trips"], INITIAL_TIMETABLE["trips"])
        self.assertIsNone(body["base_revision"])

    def test_identical_initial_table_is_idempotent(self):
        first = self.client.post("/timetables", json=INITIAL_TIMETABLE)
        second = self.client.post("/timetables", json=INITIAL_TIMETABLE)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["revision"], 0)
        self.assertFalse(second.json()["created"])

    def test_different_initial_table_is_rejected(self):
        self.client.post("/timetables", json=INITIAL_TIMETABLE)
        other = {
            "trips": [
                {
                    "trip_id": "T1",
                    "stops": [
                        {
                            "stop_seq": 1,
                            "station_name": "甲站",
                            "arrival_sec": 0,
                            "departure_sec": 50,
                        },
                        {
                            "stop_seq": 2,
                            "station_name": "乙站",
                            "arrival_sec": 100,
                            "departure_sec": 150,
                        },
                    ],
                }
            ]
        }
        response = self.client.post("/timetables", json=other)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["status"], "table_exists")

    def test_initial_structure_limits(self):
        # 21 trips
        too_many = {
            "trips": [
                {
                    "trip_id": f"T{i}",
                    "stops": [
                        {"station_name": "A", "arrival_sec": 0, "departure_sec": 1},
                        {"station_name": "B", "arrival_sec": 2, "departure_sec": 3},
                    ],
                }
                for i in range(21)
            ]
        }
        self.assertEqual(
            self.client.post("/timetables", json=too_many).status_code, 422
        )

        # 13 stops
        too_many_stops = {
            "trips": [
                {
                    "trip_id": "T1",
                    "stops": [
                        {
                            "station_name": f"S{i}",
                            "arrival_sec": i * 10,
                            "departure_sec": i * 10 + 5,
                        }
                        for i in range(13)
                    ],
                }
            ]
        }
        self.assertEqual(
            self.client.post("/timetables", json=too_many_stops).status_code, 422
        )

        # one stop
        one_stop = {
            "trips": [
                {
                    "trip_id": "T1",
                    "stops": [
                        {"station_name": "A", "arrival_sec": 0, "departure_sec": 1}
                    ],
                }
            ]
        }
        self.assertEqual(
            self.client.post("/timetables", json=one_stop).status_code, 422
        )

    def test_duplicate_trip_id_is_rejected(self):
        duplicate = {
            "trips": [
                {
                    "trip_id": "T1",
                    "stops": [
                        {"station_name": "A", "arrival_sec": 0, "departure_sec": 1},
                        {"station_name": "B", "arrival_sec": 2, "departure_sec": 3},
                    ],
                },
                {
                    "trip_id": "T1",
                    "stops": [
                        {"station_name": "C", "arrival_sec": 4, "departure_sec": 5},
                        {"station_name": "D", "arrival_sec": 6, "departure_sec": 7},
                    ],
                },
            ]
        }
        self.assertEqual(
            self.client.post("/timetables", json=duplicate).status_code, 422
        )

    def test_wrong_stop_sequence_is_rejected(self):
        bad_order = {
            "trips": [
                {
                    "trip_id": "T1",
                    "stops": [
                        {
                            "stop_seq": 2,
                            "station_name": "A",
                            "arrival_sec": 0,
                            "departure_sec": 1,
                        },
                        {
                            "stop_seq": 1,
                            "station_name": "B",
                            "arrival_sec": 2,
                            "departure_sec": 3,
                        },
                    ],
                }
            ]
        }
        self.assertEqual(
            self.client.post("/timetables", json=bad_order).status_code, 422
        )

    def test_initially_inverted_table_is_rejected_with_adjacent_stops(self):
        inverted = {
            "trips": [
                {
                    "trip_id": "T1",
                    "stops": [
                        {
                            "stop_seq": 1,
                            "station_name": "甲站",
                            "arrival_sec": 0,
                            "departure_sec": 250,
                        },
                        {
                            "stop_seq": 2,
                            "station_name": "乙站",
                            "arrival_sec": 200,
                            "departure_sec": 300,
                        },
                    ],
                }
            ]
        }
        response = self.client.post("/timetables", json=inverted)
        self.assertEqual(response.status_code, 422)
        detail = response.json()["detail"]
        self.assertEqual(detail["status"], "invalid_timetable")
        violation = detail["violations"][0]
        self.assertEqual(violation["code"], "departure_after_next_arrival")
        self.assertEqual(
            (violation["stop_seq"], violation["next_stop_seq"]), (1, 2)
        )

    def test_seconds_must_be_integers_and_are_on_a_common_baseline(self):
        fractional = {
            "trips": [
                {
                    "trip_id": "T1",
                    "stops": [
                        {"station_name": "A", "arrival_sec": 0.5, "departure_sec": 1},
                        {"station_name": "B", "arrival_sec": 2, "departure_sec": 3},
                    ],
                }
            ]
        }
        self.assertEqual(
            self.client.post("/timetables", json=fractional).status_code, 422
        )

        # Negative offsets are valid as long as the ordering chain holds.
        negative = {
            "trips": [
                {
                    "trip_id": "T1",
                    "stops": [
                        {"station_name": "A", "arrival_sec": -10, "departure_sec": -5},
                        {"station_name": "B", "arrival_sec": 0, "departure_sec": 5},
                    ],
                }
            ]
        }
        self.assertEqual(
            self.client.post("/timetables", json=negative).status_code, 200
        )

    def test_stop_sequence_is_assigned_from_list_order(self):
        unordered = {
            "trips": [
                {
                    "trip_id": "T1",
                    "stops": [
                        {"station_name": "A", "arrival_sec": 0, "departure_sec": 1},
                        {"station_name": "B", "arrival_sec": 2, "departure_sec": 3},
                    ],
                }
            ]
        }
        body = self.client.post("/timetables", json=unordered).json()
        self.assertEqual(
            [stop["stop_seq"] for stop in body["trips"][0]["stops"]], [1, 2]
        )

    def test_current_without_table_is_404(self):
        self.assertEqual(
            self.client.get("/timetables/current").status_code, 404
        )
        merge = self.client.post("/corrections", json=CORRECTION_A)
        self.assertEqual(merge.status_code, 409)
        self.assertEqual(merge.json()["detail"]["status"], "no_timetable")

    # -- merge behavior --------------------------------------------------

    def test_successful_merge_returns_new_revision_and_full_table(self):
        self.client.post("/timetables", json=INITIAL_TIMETABLE)
        response = self.client.post("/corrections", json=CORRECTION_A)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["revision"], 1)
        self.assertEqual(body["base_revision"], 0)
        self.assertEqual(body["parent_revision"], 0)
        self.assertEqual(body["merged_against_revision"], 0)
        stop1 = body["trips"][0]["stops"][0]
        self.assertEqual(stop1["departure_sec"], 180)
        self.assertEqual(body["timetable"]["revision"], 1)
        self.assertEqual(body["changes_applied"][0]["field"], "departure_sec")

    def test_unsubmitted_fields_keep_current_values(self):
        self.client.post("/timetables", json=INITIAL_TIMETABLE)
        self.client.post("/corrections", json=CORRECTION_A)
        response = self.client.post(
            "/corrections",
            json=correction("T1", 1, arrival_sec=10),
        )
        # current==base for arrival -> adopted; departure 180 stays.
        stops = response.json()["trips"][0]["stops"]
        self.assertEqual(
            (stops[0]["arrival_sec"], stops[0]["departure_sec"]), (10, 180)
        )

    def test_correction_equal_to_base_keeps_concurrent_current(self):
        self.client.post("/timetables", json=INITIAL_TIMETABLE)
        # revision 1: change departure 100 -> 110
        first = self.client.post(
            "/corrections", json=correction("T1", 1, departure_sec=110)
        )
        self.assertEqual(first.status_code, 200)
        # base 0 submission that still says 100 -> keep current 110
        second = self.client.post(
            "/corrections",
            json={
                "base_revision": 0,
                "changes": [
                    {"trip_id": "T1", "stop_seq": 1, "departure_sec": 100}
                ],
            },
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["revision"], 2)
        self.assertEqual(
            second.json()["trips"][0]["stops"][0]["departure_sec"], 110
        )
        self.assertEqual(
            second.json()["changes_kept"][0]["reason"],
            "correction_equals_base",
        )

    def test_both_sides_changed_is_a_conflict_and_nothing_commits(self):
        self.client.post("/timetables", json=INITIAL_TIMETABLE)
        self.client.post("/corrections", json=correction("T1", 1, departure_sec=110))
        before = self.client.get("/timetables/current").json()
        response = self.client.post(
            "/corrections", json=correction("T1", 1, departure_sec=180)
        )
        self.assertEqual(response.status_code, 409)
        detail = response.json()["detail"]
        self.assertEqual(detail["status"], "conflict")
        self.assertEqual(detail["conflicts"][0]["field"], "departure_sec")
        after = self.client.get("/timetables/current").json()
        self.assertEqual(before["revision"], after["revision"])
        self.assertEqual(before["trips"], after["trips"])

    def test_unknown_base_revision_is_rejected(self):
        self.client.post("/timetables", json=INITIAL_TIMETABLE)
        response = self.client.post(
            "/corrections",
            json={
                "base_revision": 999,
                "changes": [
                    {"trip_id": "T1", "stop_seq": 1, "arrival_sec": 1}
                ],
            },
        )
        self.assertEqual(response.status_code, 409)
        detail = response.json()["detail"]
        self.assertEqual(detail["status"], "unknown_revision")
        self.assertEqual(detail["known_revisions"], [0])

    def test_unknown_trip_and_stop_are_rejected(self):
        self.client.post("/timetables", json=INITIAL_TIMETABLE)
        unknown_trip = self.client.post(
            "/corrections",
            json={
                "base_revision": 0,
                "changes": [
                    {"trip_id": "NOPE", "stop_seq": 1, "arrival_sec": 1}
                ],
            },
        )
        self.assertEqual(unknown_trip.status_code, 422)
        self.assertEqual(
            unknown_trip.json()["detail"]["errors"][0]["code"], "unknown_trip"
        )

        unknown_stop = self.client.post(
            "/corrections",
            json={
                "base_revision": 0,
                "changes": [
                    {"trip_id": "T1", "stop_seq": 9, "arrival_sec": 1}
                ],
            },
        )
        self.assertEqual(unknown_stop.status_code, 422)
        self.assertEqual(
            unknown_stop.json()["detail"]["errors"][0]["code"], "unknown_stop"
        )

    def test_inversion_examples_are_individually_valid_but_merge_fails(self):
        self.client.post("/timetables", json=INITIAL_TIMETABLE)

        # Correction B alone is valid against revision 0.
        fresh = make_client(f"{self.tmp.name}/fresh.db")
        with fresh:
            fresh.post("/timetables", json=INITIAL_TIMETABLE)
            self.assertEqual(
                fresh.post("/corrections", json=CORRECTION_B).status_code, 200
            )

        # A alone is valid too.
        self.assertEqual(
            self.client.post("/corrections", json=CORRECTION_A).status_code, 200
        )
        self.assertEqual(self.client.get("/timetables/current").json()["revision"], 1)

        # A then B: B is valid against its base alone, but the merged table
        # has departure(1)=180 > arrival(2)=150.
        response = self.client.post("/corrections", json=CORRECTION_B)
        self.assertEqual(response.status_code, 422)
        detail = response.json()["detail"]
        self.assertEqual(detail["status"], "invalid_timetable")
        violation = detail["violations"][0]
        self.assertEqual(violation["code"], "departure_after_next_arrival")
        self.assertEqual(
            (violation["stop_seq"], violation["next_stop_seq"]), (1, 2)
        )
        self.assertEqual(
            (violation["departure_sec"], violation["next_arrival_sec"]),
            (180, 150),
        )

        # Whole batch rejected: still revision 1, no revision 2.
        current = self.client.get("/timetables/current").json()
        self.assertEqual(current["revision"], 1)
        self.assertEqual(current["trips"], self.client.get("/revisions/1").json()["trips"])
        revisions = self.client.get("/revisions").json()["revisions"]
        self.assertEqual([item["revision"] for item in revisions], [0, 1])

    def test_examples_endpoint_contains_both_corrections(self):
        response = self.client.get("/examples/inversion")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([item["name"] for item in body["corrections"]], ["A", "B"])
        self.assertEqual(
            body["corrections"][0]["request"]["base_revision"], 0
        )
        self.assertEqual(
            body["corrections"][1]["request"]["base_revision"], 0
        )
        self.assertIn("expected_inversion_response", body)

    # -- revisions, persistence, concurrency -----------------------------

    def test_old_revisions_remain_referencable(self):
        self.client.post("/timetables", json=INITIAL_TIMETABLE)
        self.client.post("/corrections", json=CORRECTION_A)
        self.client.post(
            "/corrections",
            json={
                "base_revision": 1,
                "changes": [
                    {"trip_id": "T1", "stop_seq": 2, "arrival_sec": 250}
                ],
            },
        )
        # revision 0 is untouched
        rev0 = self.client.get("/revisions/0").json()
        self.assertEqual(rev0["trips"][0]["stops"][0]["departure_sec"], 100)
        self.assertEqual(rev0["trips"][0]["stops"][1]["arrival_sec"], 200)

        # revision 1 has A but not the later arrival change
        rev1 = self.client.get("/revisions/rev-1").json()
        self.assertEqual(rev1["trips"][0]["stops"][0]["departure_sec"], 180)
        self.assertEqual(rev1["trips"][0]["stops"][1]["arrival_sec"], 200)

        # revision 2 has both
        rev2 = self.client.get("/revisions/2").json()
        self.assertEqual(rev2["trips"][0]["stops"][1]["arrival_sec"], 250)

        self.assertEqual(
            self.client.get("/revisions/999").status_code, 404
        )
        self.assertEqual(
            self.client.get("/revisions/current").json()["revision"], 2
        )

    def test_revisions_survive_app_restart(self):
        self.client.post("/timetables", json=INITIAL_TIMETABLE)
        self.client.post("/corrections", json=CORRECTION_A)
        self.reset_client()
        self.assertEqual(self.client.get("/timetables/current").json()["revision"], 1)
        self.assertEqual(
            self.client.get("/revisions/0").json()["trips"][0]["stops"][0]["departure_sec"],
            100,
        )
        self.assertEqual(
            self.client.get("/revisions/1").json()["trips"][0]["stops"][0]["departure_sec"],
            180,
        )

    def test_two_app_instances_merge_concurrent_corrections_against_latest(self):
        self.client.post("/timetables", json=INITIAL_TIMETABLE)

        # Two independent API instances sharing one SQLite file.
        client_a = make_client(self.db_path)
        client_b = make_client(self.db_path)
        with client_a, client_b:
            left = correction("T1", 1, departure_sec=150)
            right = correction("T1", 2, arrival_sec=250)

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                future_a = pool.submit(client_a.post, "/corrections", json=left)
                future_b = pool.submit(client_b.post, "/corrections", json=right)
                response_a = future_a.result(timeout=30)
                response_b = future_b.result(timeout=30)

        self.assertEqual(response_a.status_code, 200, response_a.text)
        self.assertEqual(response_b.status_code, 200, response_b.text)
        revisions = {
            response_a.json()["revision"], response_b.json()["revision"]
        }
        self.assertEqual(revisions, {1, 2})

        current = self.client.get("/timetables/current").json()
        stops = current["trips"][0]["stops"]
        self.assertEqual(stops[0]["departure_sec"], 150)
        self.assertEqual(stops[1]["arrival_sec"], 250)
        self.assertEqual(current["revision"], 2)
        listed = self.client.get("/revisions").json()["revisions"]
        self.assertEqual([item["revision"] for item in listed], [0, 1, 2])

    def test_same_field_conflict_is_detected_after_concurrent_style_change(self):
        self.client.post("/timetables", json=INITIAL_TIMETABLE)
        self.client.post("/corrections", json=correction("T1", 1, departure_sec=150))
        current = self.client.get("/timetables/current").json()
        response = self.client.post(
            "/corrections", json=correction("T1", 1, departure_sec=160)
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["status"], "conflict")
        self.assertEqual(
            self.client.get("/timetables/current").json()["revision"],
            current["revision"],
        )

    def test_alias_endpoints_are_available(self):
        self.assertEqual(
            self.client.post("/api/v1/timetables", json=INITIAL_TIMETABLE).status_code,
            200,
        )
        self.assertEqual(self.client.get("/api/timetables/current").json()["revision"], 0)
        self.assertEqual(
            self.client.post("/api/v1/corrections", json=CORRECTION_A).status_code,
            200,
        )
        self.assertEqual(self.client.get("/api/v1/revisions/1").json()["revision"], 1)

    def test_flexible_correction_shapes(self):
        self.client.post("/timetables", json=INITIAL_TIMETABLE)

        # A single change at the top level, with camelCase field names.
        single = self.client.post(
            "/corrections",
            json={
                "baseRevision": 0,
                "tripId": "T1",
                "stopSeq": 1,
                "departureSec": 150,
            },
        )
        self.assertEqual(single.status_code, 200)
        self.assertEqual(
            single.json()["trips"][0]["stops"][0]["departure_sec"], 150
        )

        # A correction shaped like the initial table, using the corrections
        # alias and nested stops.
        nested = self.client.post(
            "/corrections",
            json={
                "base_revision": 0,
                "corrections": [
                    {
                        "trip_id": "T1",
                        "stops": [
                            {
                                "stop_seq": 2,
                                "arrival_sec": 250,
                            }
                        ],
                    }
                ],
            },
        )
        self.assertEqual(nested.status_code, 200)
        self.assertEqual(
            nested.json()["trips"][0]["stops"][1]["arrival_sec"], 250
        )

    def test_initial_table_accepts_wrapped_shapes(self):
        wrapped = self.client.post(
            "/timetables", json={"timetable": INITIAL_TIMETABLE}
        )
        self.assertEqual(wrapped.status_code, 200)
        self.assertEqual(wrapped.json()["revision"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
