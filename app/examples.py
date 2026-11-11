"""Example payloads used by /docs, /examples and the test suite."""

from __future__ import annotations

INITIAL_TIMETABLE = {
    "trips": [
        {
            "trip_id": "T1",
            "stops": [
                {
                    "stop_seq": 1,
                    "station_name": "甲站",
                    "arrival_sec": 0,
                    "departure_sec": 100,
                },
                {
                    "stop_seq": 2,
                    "station_name": "乙站",
                    "arrival_sec": 200,
                    "departure_sec": 300,
                },
                {
                    "stop_seq": 3,
                    "station_name": "丙站",
                    "arrival_sec": 400,
                    "departure_sec": 500,
                },
            ],
        }
    ]
}

# Individually valid against revision 0: 0 <= 180 <= 200 (next arrival).
CORRECTION_A = {
    "base_revision": 0,
    "changes": [
        {
            "trip_id": "T1",
            "stop_seq": 1,
            "departure_sec": 180,
        }
    ],
}

# Individually valid against revision 0: 100 (previous departure) <= 150 <= 300.
CORRECTION_B = {
    "base_revision": 0,
    "changes": [
        {
            "trip_id": "T1",
            "stop_seq": 2,
            "arrival_sec": 150,
        }
    ],
}

INVERSION_EXPLANATION = (
    "两份更正各自相对修订 0 都合法：更正 A 把 T1 停站 1 的发车改为 180 秒，"
    "仍不晚于停站 2 的到站 200 秒；更正 B 把 T1 停站 2 的到站改为 150 秒，"
    "仍不早于停站 1 的原发车 100 秒。先提交 A 得到修订 1 后，再以修订 0 "
    "为基准提交 B，三向合并会把 B 的 150 秒采用到最新表中，于是停站 1 "
    "发车 180 秒晚于停站 2 到站 150 秒，整批被拒绝且不产生修订 2。"
)

EXPECTED_INVERSION_ERROR = {
    "detail": {
        "status": "invalid_timetable",
        "message": "合并后的时刻表不满足到发顺序，整批未提交。",
        "base_revision": 0,
        "current_revision": 1,
        "violations": [
            {
                "code": "departure_after_next_arrival",
                "trip_id": "T1",
                "stop_seq": 1,
                "next_stop_seq": 2,
                "station_name": "甲站",
                "next_station_name": "乙站",
                "field": "departure_sec",
                "next_field": "arrival_sec",
                "departure_sec": 180,
                "next_arrival_sec": 150,
                "message": (
                    "车次 T1 停站 1（甲站）发车 180 秒晚于下一停站 "
                    "2（乙站）到站 150 秒"
                ),
            }
        ],
    }
}
