"""FastAPI service for merging offline bus-timetable timing corrections.

Run locally:
    uvicorn app.main:app --host 0.0.0.0 --port 8000

Interactive API documentation: http://localhost:8000/docs
"""

from __future__ import annotations

import copy
import json
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse

from .examples import (
    CORRECTION_A,
    CORRECTION_B,
    EXPECTED_INVERSION_ERROR,
    INITIAL_TIMETABLE,
    INVERSION_EXPLANATION,
)
from .models import (
    CorrectionRequest,
    CreateResponse,
    MergeResponse,
    TableResponse,
    TableSnapshot,
    TimetableCreateRequest,
    parse_revision,
)
from .store import RevisionRecord, SqliteStore
from .timetable import (
    MergeInputError,
    canonical_trips,
    merge_tables,
    validate_timing,
)

DEFAULT_DB_PATH = os.environ.get(
    "TIMETABLE_DB_PATH", os.environ.get("DATABASE_PATH", "./data/timetable.db")
)


def _snapshot(record: RevisionRecord) -> dict:
    return {
        "revision": record.revision,
        "base_revision": record.base_revision,
        "parent_revision": record.parent_revision,
        "created_at": record.created_at,
        "trips": record.trips,
    }


def _table_response(record: RevisionRecord) -> TableResponse:
    snapshot = _snapshot(record)
    return TableResponse(
        **snapshot, timetable=TableSnapshot(**snapshot)
    )


CREATE_EXAMPLE_RESPONSE = {
    "status": "ok",
    "message": "已保存初始时刻表，修订 0。",
    "revision": 0,
    "base_revision": None,
    "parent_revision": None,
    "created_at": "2026-01-01T00:00:00.000Z",
    "created": True,
    "trips": INITIAL_TIMETABLE["trips"],
    "timetable": {
        "revision": 0,
        "base_revision": None,
        "parent_revision": None,
        "created_at": "2026-01-01T00:00:00.000Z",
        "trips": INITIAL_TIMETABLE["trips"],
    },
}

CURRENT_EXAMPLE_RESPONSE = {
    "revision": 0,
    "base_revision": None,
    "parent_revision": None,
    "created_at": "2026-01-01T00:00:00.000Z",
    "trips": INITIAL_TIMETABLE["trips"],
    "timetable": {
        "revision": 0,
        "base_revision": None,
        "parent_revision": None,
        "created_at": "2026-01-01T00:00:00.000Z",
        "trips": INITIAL_TIMETABLE["trips"],
    },
}

MERGE_TRIPS = copy.deepcopy(INITIAL_TIMETABLE["trips"])
MERGE_TRIPS[0]["stops"][0]["departure_sec"] = 180

MERGE_EXAMPLE_RESPONSE = {
    "status": "ok",
    "message": "更正已按最新修订 0 合并，提交为修订 1。",
    "revision": 1,
    "base_revision": 0,
    "parent_revision": 0,
    "merged_against_revision": 0,
    "created_at": "2026-01-01T00:00:01.000Z",
    "trips": MERGE_TRIPS,
    "timetable": {
        "revision": 1,
        "base_revision": 0,
        "parent_revision": 0,
        "created_at": "2026-01-01T00:00:01.000Z",
        "trips": MERGE_TRIPS,
    },
    "changes_applied": [
        {
            "trip_id": "T1",
            "stop_seq": 1,
            "field": "departure_sec",
            "base_value": 100,
            "current_value": 100,
            "correction_value": 180,
            "new_value": 180,
            "decision": "applied",
            "reason": "current_equals_base",
        }
    ],
    "changes_kept": [],
}

CONFLICT_EXAMPLE = {
    "detail": {
        "status": "conflict",
        "message": "更正与最新表冲突，整批未提交。",
        "base_revision": 0,
        "current_revision": 1,
        "conflicts": [
            {
                "trip_id": "T1",
                "stop_seq": 2,
                "field": "arrival_sec",
                "base_value": 200,
                "current_value": 210,
                "correction_value": 150,
                "message": (
                    "车次 T1 停站 2 的 arrival_sec 冲突：基准 200，"
                    "当前 210，更正 150"
                ),
            }
        ],
    }
}


def _violation_error(
    violations: list[dict],
    *,
    base_revision: Optional[int],
    current_revision: Optional[int],
    initial: bool = False,
) -> HTTPException:
    message = (
        "初始时刻表不满足到发顺序，未保存。"
        if initial
        else "合并后的时刻表不满足到发顺序，整批未提交。"
    )
    detail = {
        "status": "invalid_timetable",
        "message": message,
        "violations": violations,
    }
    if base_revision is not None:
        detail["base_revision"] = base_revision
    if current_revision is not None:
        detail["current_revision"] = current_revision
    return HTTPException(status_code=422, detail=detail)


def create_app(db_path: Optional[str] = None) -> FastAPI:
    store = SqliteStore(db_path or DEFAULT_DB_PATH)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        store.initialize()
        yield

    app = FastAPI(
        title="公交时刻表更正合并 API",
        summary="基于 SQLite 修订链的三向合并与时间顺序校验",
        description=(
            "各车队离线校时得到的更正可能基于不同时刻表。服务把初始表与每次成功"
            "合并保存为不可变修订，并在写入事务中按**最新修订**做三向合并：\n\n"
            "- 未提交字段保留当前值；\n"
            "- 当前值等于基准值时采用更正；\n"
            "- 更正值等于基准值或当前值时保留当前值；\n"
            "- 其余情况为冲突。\n\n"
            "合并后的整表必须满足每站 `到站 <= 发车 <= 下一站到站`，末站只查"
            "`到站 <= 发车`。任一冲突或时间倒置都会拒绝整批，不产生新修订。\n\n"
            "**并发与持久性：** 每个成功更正是一条完整快照记录，`BEGIN IMMEDIATE` "
            "让多个 API 进程串行合并；查询只会看到整批前后状态，旧修订永久可通过"
            "`GET /revisions/{revision}` 引用。\n\n"
            "**合并后倒置示例：** " + INVERSION_EXPLANATION + " 见 "
            "`GET /examples/inversion`。"
        ),
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        openapi_tags=[
            {"name": "timetable", "description": "初始表与当前表"},
            {"name": "corrections", "description": "三向合并更正"},
            {"name": "revisions", "description": "不可变修订查询"},
            {"name": "examples", "description": "可演示示例"},
            {"name": "system", "description": "服务状态"},
        ],
    )
    app.state.store = store

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/docs")

    @app.get("/health", tags=["system"], summary="健康检查")
    def health() -> dict:
        return {"status": "ok"}

    # ------------------------------------------------------------------
    # Initial table
    # ------------------------------------------------------------------
    @app.post(
        "/timetables",
        tags=["timetable"],
        response_model=CreateResponse,
        summary="创建初始时刻表（修订 0）",
        description=(
            "保存唯一一份初始表。最多 20 个车次，车次 ID 唯一；每车 2～12 个"
            "固定顺序停站（序号从 1 起）。初始表必须通过到发顺序校验。"
            "重复提交完全相同的初始表是幂等的；已有其他修订时返回 409。"
        ),
        responses={
            200: {
                "description": "初始表已存在或已创建。",
                "content": {
                    "application/json": {"example": CREATE_EXAMPLE_RESPONSE}
                },
            },
            409: {
                "description": "已有初始表或其他修订，且内容不同。",
            },
            422: {
                "description": "结构校验失败，或初始表时间倒置。",
            },
        },
    )
    @app.post("/init", include_in_schema=False, response_model=CreateResponse)
    @app.post("/timetable", include_in_schema=False, response_model=CreateResponse)
    @app.post(
        "/timetables/init", include_in_schema=False, response_model=CreateResponse
    )
    @app.post(
        "/timetable/init", include_in_schema=False, response_model=CreateResponse
    )
    @app.post(
        "/api/timetable/init", include_in_schema=False, response_model=CreateResponse
    )
    @app.post(
        "/api/v1/timetable/init", include_in_schema=False,
        response_model=CreateResponse,
    )
    @app.post("/api/timetable", include_in_schema=False, response_model=CreateResponse)
    @app.post(
        "/api/timetables", include_in_schema=False, response_model=CreateResponse
    )
    @app.post(
        "/api/v1/timetable", include_in_schema=False, response_model=CreateResponse
    )
    @app.post(
        "/api/v1/timetables", include_in_schema=False, response_model=CreateResponse
    )
    @app.post("/api/init", include_in_schema=False, response_model=CreateResponse)
    @app.post("/api/v1/init", include_in_schema=False, response_model=CreateResponse)
    def create_timetable(
        payload: TimetableCreateRequest = Body(
            ...,
            openapi_examples={
                "initial_timetable": {
                    "summary": "初始表：1 个车次、3 个停站",
                    "description": (
                        "停站 1 发车 100 <= 停站 2 到站 200；"
                        "停站 2 发车 300 <= 停站 3 到站 400。"
                    ),
                    "value": INITIAL_TIMETABLE,
                }
            },
        ),
    ) -> CreateResponse:
        trips = canonical_trips(payload)
        violations = validate_timing(trips)
        if violations:
            raise _violation_error(
                violations, base_revision=None, current_revision=None, initial=True
            )

        with store.write_transaction() as conn:
            current = store.latest_conn(conn)
            if current is None:
                record = store.insert_conn(
                    conn,
                    revision=0,
                    base_revision=None,
                    parent_revision=None,
                    trips=trips,
                    details={"changes": []},
                )
                created = True
                message = "已保存初始时刻表，修订 0。"
            elif (
                current.revision == 0
                and current.details.get("changes") == []
                and current.trips == trips
            ):
                record = current
                created = False
                message = "初始时刻表已存在，内容相同，返回修订 0。"
            else:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "status": "table_exists",
                        "message": (
                            "初始时刻表已存在，且当前内容不同；"
                            "请基于已有修订提交更正。"
                        ),
                        "current_revision": current.revision,
                    },
                )

        snapshot = _snapshot(record)
        return CreateResponse(
            status="ok",
            message=message,
            revision=record.revision,
            base_revision=record.base_revision,
            parent_revision=record.parent_revision,
            created_at=record.created_at,
            trips=record.trips,
            timetable=TableSnapshot(**snapshot),
            created=created,
        )

    # ------------------------------------------------------------------
    # Current table
    # ------------------------------------------------------------------
    @app.get(
        "/timetables/current",
        tags=["timetable"],
        response_model=TableResponse,
        summary="读取当前完整时刻表",
        responses={
            404: {"description": "尚未创建初始表。"},
            200: {
                "description": "按单个快照行读取，不会看到半批提交。",
                "content": {
                    "application/json": {
                        "example": CURRENT_EXAMPLE_RESPONSE
                    }
                },
            },
        },
    )
    @app.get("/current", include_in_schema=False, response_model=TableResponse)
    @app.get("/table", include_in_schema=False, response_model=TableResponse)
    @app.get("/timetable", include_in_schema=False, response_model=TableResponse)
    @app.get(
        "/timetable/current", include_in_schema=False, response_model=TableResponse
    )
    @app.get("/api/current", include_in_schema=False, response_model=TableResponse)
    @app.get("/api/table", include_in_schema=False, response_model=TableResponse)
    @app.get("/api/timetable", include_in_schema=False, response_model=TableResponse)
    @app.get(
        "/api/timetables/current",
        include_in_schema=False,
        response_model=TableResponse,
    )
    @app.get(
        "/api/v1/timetable", include_in_schema=False, response_model=TableResponse
    )
    @app.get(
        "/api/v1/timetables/current",
        include_in_schema=False,
        response_model=TableResponse,
    )
    def current_timetable() -> TableResponse:
        record = store.latest()
        if record is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "status": "no_timetable",
                    "message": "尚未创建初始时刻表，请先 POST /timetables。",
                },
            )
        return _table_response(record)

    # ------------------------------------------------------------------
    # Corrections
    # ------------------------------------------------------------------
    @app.post(
        "/corrections",
        tags=["corrections"],
        response_model=MergeResponse,
        summary="按最新表三向合并一批更正",
        description=(
            "请求携带 `base_revision`（必须已存在）和按 `trip_id` + 1 起"
            " `stop_seq` 定位的到发秒修改。整批在一个 SQLite 写事务中完成"
            "合并、全表校验与提交；任何冲突或时间倒置都会整批回滚。"
        ),
        responses={
            200: {
                "description": "合并成功，返回新修订及完整表。",
                "content": {
                    "application/json": {"example": MERGE_EXAMPLE_RESPONSE}
                },
            },
            409: {
                "description": "未知基准修订、尚无初始表，或存在字段冲突。",
                "content": {
                    "application/json": {
                        "examples": {
                            "conflict": {
                                "summary": "三向合并冲突",
                                "value": CONFLICT_EXAMPLE,
                            }
                        }
                    }
                },
            },
            422: {
                "description": "结构/字段错误，或合并后时间倒置。",
                "content": {
                    "application/json": {
                        "examples": {
                            "inversion_after_merge": {
                                "summary": "两份各自合法的更正合并后倒置",
                                "value": EXPECTED_INVERSION_ERROR,
                            }
                        }
                    }
                },
            },
        },
    )
    @app.post("/merge", include_in_schema=False, response_model=MergeResponse)
    @app.post("/correction", include_in_schema=False, response_model=MergeResponse)
    @app.post("/api/correction", include_in_schema=False, response_model=MergeResponse)
    @app.post(
        "/timetable/correction", include_in_schema=False,
        response_model=MergeResponse,
    )
    @app.post(
        "/timetables/correction", include_in_schema=False,
        response_model=MergeResponse,
    )
    @app.post(
        "/timetable/corrections", include_in_schema=False,
        response_model=MergeResponse,
    )
    @app.post(
        "/timetables/corrections", include_in_schema=False,
        response_model=MergeResponse,
    )
    @app.post("/api/corrections", include_in_schema=False, response_model=MergeResponse)
    @app.post("/api/merge", include_in_schema=False, response_model=MergeResponse)
    @app.post(
        "/api/timetable/correction", include_in_schema=False,
        response_model=MergeResponse,
    )
    @app.post(
        "/api/timetable/corrections", include_in_schema=False,
        response_model=MergeResponse,
    )
    @app.post(
        "/api/timetables/correction", include_in_schema=False,
        response_model=MergeResponse,
    )
    @app.post(
        "/api/timetables/merge", include_in_schema=False,
        response_model=MergeResponse,
    )
    @app.post(
        "/api/v1/corrections", include_in_schema=False, response_model=MergeResponse
    )
    @app.post(
        "/api/v1/timetable/corrections", include_in_schema=False,
        response_model=MergeResponse,
    )
    @app.post(
        "/api/v1/timetables/corrections", include_in_schema=False,
        response_model=MergeResponse,
    )
    @app.post("/api/v1/merge", include_in_schema=False, response_model=MergeResponse)
    def merge_correction(
        payload: CorrectionRequest = Body(
            ...,
            openapi_examples={
                "valid_departure_change": {
                    "summary": "更正 A：单独提交合法",
                    "description": (
                        "相对修订 0，把 T1 停站 1 发车改为 180 秒；"
                        "0 <= 180 <= 下一站到站 200。"
                    ),
                    "value": CORRECTION_A,
                },
                "valid_arrival_change": {
                    "summary": "更正 B：单独提交合法",
                    "description": (
                        "相对修订 0，把 T1 停站 2 到站改为 150 秒；"
                        "上一站发车 100 <= 150 <= 本站发车 300。"
                        "若修订 1 已采用更正 A（发车 180），再提交 B 会因 "
                        "180 > 150 整批被拒绝。"
                    ),
                    "value": CORRECTION_B,
                },
            },
        ),
    ) -> MergeResponse:
        with store.write_transaction() as conn:
            current = store.latest_conn(conn)
            if current is None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "status": "no_timetable",
                        "message": "尚未创建初始时刻表，请先 POST /timetables。",
                    },
                )

            base = store.get_conn(conn, payload.base_revision)
            if base is None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "status": "unknown_revision",
                        "message": f"未知基准修订 {payload.base_revision}，整批拒绝。",
                        "base_revision": payload.base_revision,
                        "known_revisions": store.known_revisions_conn(conn),
                    },
                )

            try:
                outcome = merge_tables(
                    base.trips, current.trips, payload.changes
                )
            except MergeInputError as exc:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "status": "invalid_request",
                        "message": exc.message,
                        "errors": exc.errors,
                    },
                )

            if outcome.conflicts:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "status": "conflict",
                        "message": "更正与最新表冲突，整批未提交。",
                        "base_revision": payload.base_revision,
                        "current_revision": current.revision,
                        "conflicts": outcome.conflicts,
                        "violations": outcome.violations,
                    },
                )
            if outcome.violations:
                raise _violation_error(
                    outcome.violations,
                    base_revision=payload.base_revision,
                    current_revision=current.revision,
                )

            new_revision = current.revision + 1
            record = store.insert_conn(
                conn,
                revision=new_revision,
                base_revision=payload.base_revision,
                parent_revision=current.revision,
                trips=outcome.trips,
                details={
                    "requested_base_revision": payload.base_revision,
                    "merged_against_revision": current.revision,
                    "changes_applied": outcome.applied,
                    "changes_kept": outcome.kept,
                },
            )

        snapshot = _snapshot(record)
        return MergeResponse(
            status="ok",
            message=(
                f"更正已按最新修订 {current.revision} 合并，提交为修订 "
                f"{record.revision}。"
            ),
            revision=record.revision,
            base_revision=record.base_revision,
            parent_revision=record.parent_revision,
            created_at=record.created_at,
            trips=record.trips,
            timetable=TableSnapshot(**snapshot),
            merged_against_revision=current.revision,
            changes_applied=outcome.applied,
            changes_kept=outcome.kept,
        )

    # ------------------------------------------------------------------
    # Revision history
    # ------------------------------------------------------------------
    @app.get(
        "/revisions",
        tags=["revisions"],
        summary="列出所有修订",
        description="返回修订链元数据；每个修订都是一份完整不可变快照。",
    )
    @app.get("/timetables/revisions", include_in_schema=False)
    @app.get("/api/revisions", include_in_schema=False)
    @app.get("/api/v1/revisions", include_in_schema=False)
    @app.get("/revision-history", include_in_schema=False)
    def list_revisions() -> dict:
        records = store.list_revisions()
        return {
            "current_revision": records[-1].revision if records else None,
            "revisions": [
                {
                    "revision": record.revision,
                    "base_revision": record.base_revision,
                    "parent_revision": record.parent_revision,
                    "created_at": record.created_at,
                    "trip_count": len(record.trips),
                    "stop_count": sum(
                        len(trip["stops"]) for trip in record.trips
                    ),
                }
                for record in records
            ],
        }

    @app.get(
        "/revisions/current",
        tags=["revisions"],
        response_model=TableResponse,
        summary="读取当前修订",
    )
    @app.get(
        "/revisions/latest", include_in_schema=False, response_model=TableResponse
    )
    @app.get(
        "/timetables/revisions/current",
        include_in_schema=False,
        response_model=TableResponse,
    )
    def current_revision() -> TableResponse:
        return current_timetable()

    @app.get(
        "/revisions/{revision}",
        tags=["revisions"],
        response_model=TableResponse,
        summary="按修订号读取旧表",
        description=(
            "修订号是持久化主键。服务重启后，历史修订仍可按原修订号读取。"
            "除纯数字外也接受 `rev-0` 等写法。"
        ),
        responses={
            404: {"description": "修订不存在。"},
            422: {"description": "修订号无法解析。"},
        },
    )
    @app.get(
        "/timetables/revisions/{revision}",
        include_in_schema=False,
        response_model=TableResponse,
    )
    @app.get(
        "/api/revisions/{revision}",
        include_in_schema=False,
        response_model=TableResponse,
    )
    @app.get(
        "/api/v1/revisions/{revision}",
        include_in_schema=False,
        response_model=TableResponse,
    )
    def get_revision(revision: str) -> TableResponse:
        try:
            revision_number = parse_revision(revision)
            if revision_number < 0:
                raise ValueError("negative revision")
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail={
                    "status": "invalid_revision",
                    "message": f"无法解析修订号 {revision!r}。",
                },
            )
        record = store.get(revision_number)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "status": "unknown_revision",
                    "message": f"修订 {revision_number} 不存在。",
                    "revision": revision_number,
                    "known_revisions": [
                        item.revision for item in store.list_revisions()
                    ],
                },
            )
        return _table_response(record)

    # ------------------------------------------------------------------
    # Examples
    # ------------------------------------------------------------------
    @app.get(
        "/examples",
        tags=["examples"],
        summary="示例入口",
        description="列出可直接演示的示例和 /docs 链接。",
    )
    def examples_index() -> dict:
        return {
            "inversion_after_merge": {
                "description": INVERSION_EXPLANATION,
                "endpoint": "/examples/inversion",
            },
            "initial_table": {"endpoint": "/examples/initial"},
            "docs": "/docs",
        }

    @app.get(
        "/examples/initial",
        tags=["examples"],
        summary="初始表示例",
    )
    def example_initial() -> dict:
        return {
            "request": INITIAL_TIMETABLE,
            "endpoint": "POST /timetables",
            "curl": (
                "curl -s -X POST http://localhost:8000/timetables "
                "-H 'Content-Type: application/json' "
                f"-d '{json.dumps(INITIAL_TIMETABLE, ensure_ascii=False)}'"
            ),
        }

    @app.get(
        "/examples/inversion",
        tags=["examples"],
        summary="两份各自合法、合并后时间倒置的更正",
        description=INVERSION_EXPLANATION,
    )
    def example_inversion(request: Request) -> dict:
        base_url = str(request.base_url).rstrip("/")
        return {
            "description": INVERSION_EXPLANATION,
            "initial_table": {
                "endpoint": "POST /timetables",
                "request": INITIAL_TIMETABLE,
            },
            "corrections": [
                {
                    "name": "A",
                    "individually_valid_against_revision": 0,
                    "explanation": (
                        "停站 1 发车 100 -> 180，仍 <= 停站 2 到站 200。"
                    ),
                    "request": CORRECTION_A,
                    "curl": (
                        f"curl -s -X POST {base_url}/corrections "
                        "-H 'Content-Type: application/json' "
                        f"-d '{json.dumps(CORRECTION_A, ensure_ascii=False)}'"
                    ),
                },
                {
                    "name": "B",
                    "individually_valid_against_revision": 0,
                    "explanation": (
                        "停站 2 到站 200 -> 150，仍 >= 停站 1 原发车 100。"
                    ),
                    "request": CORRECTION_B,
                    "curl": (
                        f"curl -s -X POST {base_url}/corrections "
                        "-H 'Content-Type: application/json' "
                        f"-d '{json.dumps(CORRECTION_B, ensure_ascii=False)}'"
                    ),
                },
            ],
            "recommended_sequence": [
                "POST /timetables",
                "POST /corrections with correction A  -> revision 1",
                "POST /corrections with correction B (base_revision 0) "
                "-> HTTP 422, so revision 2 is not created",
                "GET /timetables/current -> still revision 1",
                "GET /revisions/0 -> original table",
            ],
            "expected_inversion_response": EXPECTED_INVERSION_ERROR,
        }

    return app


app = create_app()
