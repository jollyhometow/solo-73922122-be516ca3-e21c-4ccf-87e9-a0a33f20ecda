# 公交时刻表更正合并 API

各车队离线校时后会提交基于某份时刻表的到发秒更正。服务把初始表与每次成功合并保存为不可变修订，并以**最新修订**为当前值做三向合并，避免后一份更正直接覆盖先前修改。

- 修订存储：SQLite（初始表 + 每次完整快照）
- 合并规则：未提交字段保留当前值；提交字段按基准 / 当前 / 更正三方比较
- 整表校验：每个停站 `到站 <= 发车 <= 下一站到站`，末站只查 `到站 <= 发车`
- 整批语义：任一冲突或时间倒置都拒绝整批，不产生新修订
- 并发安全：`BEGIN IMMEDIATE` 写事务内读取最新表、合并、校验、单行快照提交
- 可演示：`/docs`、`/examples/inversion`，以及两份单独合法但合并后倒置的更正文件

## 快速启动（Docker Compose）

```bash
docker compose up --build -d
```

启动**两个共享同一 SQLite 卷的 API 进程**：

| 进程 | Swagger UI | OpenAPI |
| --- | --- | --- |
| api1 | <http://localhost:8000/docs> | <http://localhost:8000/openapi.json> |
| api2 | <http://localhost:8001/docs> | <http://localhost:8001/openapi.json> |

健康检查：`GET /health`。停止：`docker compose down`（保留数据卷 `timetable-merge-data`，重启后旧修订仍可读取）。

```bash
docker compose down -v   # 连数据卷一起删除，回到无初始表状态
```

端口可改：

```bash
API1_PORT=18080 API2_PORT=18081 docker compose up --build -d
```

镜像基于 `python:3.11-slim`，以非 root 用户运行。Compose 会先运行一次性的 `init-data` 容器（不提供 API），把共享命名卷 `timetable-merge-data` 的权限交给非 root 用户；随后启动两个 API 进程，SQLite 数据库位于 `/data/timetable.db`。

## 数据模型

初始表最多 **20 个车次**，车次 ID 唯一；每个车次 **2～12 个固定顺序停站**：

```json
{
  "trips": [
    {
      "trip_id": "T1",
      "stops": [
        {"stop_seq": 1, "station_name": "甲站", "arrival_sec": 0,   "departure_sec": 100},
        {"stop_seq": 2, "station_name": "乙站", "arrival_sec": 200, "departure_sec": 300},
        {"stop_seq": 3, "station_name": "丙站", "arrival_sec": 400, "departure_sec": 500}
      ]
    }
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `trip_id` | 车次 ID，非空且全表唯一 |
| `stops` | 2～12 个停站，固定顺序 |
| `stop_seq` | 停站序号，从 1 起；省略时按列表顺序自动赋值，若提交则必须与顺序一致 |
| `station_name` | 站名，非空 |
| `arrival_sec` / `departure_sec` | 同一基准下的整数秒（严格整数，可为负，但必须满足顺序约束） |

初始表与合并后的表都必须满足：

```text
第 k 站：arrival_sec[k] <= departure_sec[k]
非末站：departure_sec[k] <= arrival_sec[k+1]
末站：  只检查 arrival_sec[last] <= departure_sec[last]
```

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/timetables` | 创建初始表，返回修订 `0`；同一内容重复提交幂等 |
| `GET` | `/timetables/current` | 读取当前修订的完整表 |
| `POST` | `/corrections` | 按最新表三向合并一批更正 |
| `GET` | `/revisions` | 列出修订链元数据 |
| `GET` | `/revisions/{revision}` | 按修订号读取旧表；重启后仍可用 |
| `GET` | `/revisions/current` | 当前修订 |
| `GET` | `/examples/inversion` | 返回两份“单独合法、合并后倒置”的示例 |
| `GET` | `/health` | 健康检查 |

也提供兼容别名（不出现在 `/docs` 主列表）：`POST /init`、`POST /merge`、`GET /current`、`GET /table`、`GET /api/v1/revisions/{revision}` 等。

### 更正请求

```json
{
  "base_revision": 0,
  "changes": [
    {"trip_id": "T1", "stop_seq": 1, "arrival_sec": 0, "departure_sec": 180}
  ]
}
```

- `base_revision` 必须已存在，否则整批拒绝（HTTP `409`，`status=unknown_revision`）。
- `changes` 按 `trip_id` + 从 1 起的 `stop_seq` 定位停站。
- 每个 change 至少提交 `arrival_sec` 或 `departure_sec` 之一；同一批内同一字段不能重复提交。
- 未提交字段保留当前值。

### 三向合并规则

设某提交字段：基准修订值 `b`、最新当前值 `c`、更正提交值 `x`。

| 条件 | 结果 |
| --- | --- |
| `c == b` | 采用更正 `x` |
| `c != b` 且 `x == b` | 保留当前值 `c`（更正未改变基准值） |
| `c != b` 且 `x == c` | 保留当前值 `c`（更正与当前一致） |
| `c != b` 且 `x != b` 且 `x != c` | **冲突**，整批拒绝（HTTP `409`） |
| 未提交字段 | 保留当前值 `c` |

成功时返回新修订号、完整表、实际采用的字段（`changes_applied`）和保留当前值的字段（`changes_kept`）。完整表同时在 `trips` 和 `timetable` 两个键中返回，便于直接使用。

## 合并后时间倒置示例（两份更正单独均合法）

示例文件：

- `examples/initial_timetable.json`
- `examples/correction_a_valid_alone.json`
- `examples/correction_b_valid_alone.json`
- `examples/inversion_after_merge_error.json`

初始表：`T1` 停站 1 `(到站 0, 发车 100)`，停站 2 `(到站 200, 发车 300)`。

| 更正 | 单独相对修订 0 校验 | 结果 |
| --- | --- | --- |
| A：停站 1 发车 `100 -> 180` | `0 <= 180 <= 下一站到站 200` | 合法 |
| B：停站 2 到站 `200 -> 150` | `上一站发车 100 <= 150 <= 本站发车 300` | 合法 |
| A 后（修订 1）再合并 B | 最新表停站 1 发车已为 180 | `180 > 150`，整批拒绝 |

```bash
# 1. 创建初始表（api1，修订 0）
curl -s -X POST http://localhost:8000/timetables \
  -H 'Content-Type: application/json' \
  -d @examples/initial_timetable.json

# 2. 提交更正 A（api1，得到修订 1）
curl -s -X POST http://localhost:8000/corrections \
  -H 'Content-Type: application/json' \
  -d @examples/correction_a_valid_alone.json

# 3. 通过另一个 API 进程提交更正 B（base_revision 仍为 0）
#    它按共享 SQLite 中的最新修订 1 合并，得到 180 > 150，返回 422
curl -s -i -X POST http://localhost:8001/corrections \
  -H 'Content-Type: application/json' \
  -d @examples/correction_b_valid_alone.json

# 4. 当前仍为修订 1；修订 2 从未产生
curl -s http://localhost:8001/timetables/current
curl -s http://localhost:8001/revisions
curl -s http://localhost:8001/revisions/0
```

第 3 步的响应（HTTP `422`）：

```json
{
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
        "message": "车次 T1 停站 1（甲站）发车 180 秒晚于下一停站 2（乙站）到站 150 秒"
      }
    ]
  }
}
```

也可在浏览器打开两个进程的 `/docs`，在 `POST /corrections` 的示例下拉框中直接选择更正 A、B 进行试调用。

## 错误响应

| HTTP | `detail.status` | 场景 |
| --- | --- | --- |
| `409` | `no_timetable` | 尚未创建初始表 |
| `409` | `table_exists` | 已有不同内容的初始表/修订 |
| `409` | `unknown_revision` | 基准修订不存在 |
| `409` | `conflict` | 三向合并冲突，返回 `conflicts[]`（含 `trip_id`、`stop_seq`、`field`、三方值） |
| `422` | `invalid_timetable` | 初始表或合并结果时间倒置，返回 `violations[]`（含字段及相邻停站） |
| `422` | `invalid_request` | 未知车次、停站越界、批内重复字段等 |
| `422` | 标准校验列表 | 请求结构、车次/停站数量、ID 唯一性、整数秒等不合法 |

冲突和倒置都会导致整批回滚；查询当前表或旧修订不会看到被拒绝的中间状态。

## 并发、一致性与重启

- 每个修订是 `revisions` 表中的**一行完整 JSON 快照**。查询只读该行，因此不会读到半批数据。
- 更正写入在 `BEGIN IMMEDIATE` 事务中完成：先锁写、再读取最新修订、按最新表三向合并、全表校验、插入新修订、提交。两个 API 进程因此串行完成合并，后到的更正不会覆盖先到的修改。
- SQLite 使用 WAL、`synchronous=FULL` 和忙等待重试；`api1` 与 `api2` 挂载同一命名卷。
- 修订号从 `0` 开始单调递增。旧修订行永不修改，服务重启、容器重建后仍可通过 `GET /revisions/{revision}` 引用。

## 本地开发与测试

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt

TIMETABLE_DB_PATH=./data/timetable.db uvicorn app.main:app --reload
python -m unittest discover -s tests -v
```

测试覆盖：三向合并四种分支、整批冲突/倒置回滚、未知修订、结构限制、旧修订持久化、两个独立 app 实例共享 SQLite 的并发合并。

## 目录

```text
app/
  main.py       FastAPI 路由、/docs 示例、错误映射
  models.py     请求/响应模型与字段别名
  timetable.py  三向合并、到发顺序校验
  store.py      SQLite 修订存储、写事务
  examples.py   示例常量（/examples 与 /docs 共用）
tests/
  test_timetable.py  合并规则与校验
  test_api.py        API、并发、持久化
examples/
  initial_timetable.json
  correction_a_valid_alone.json
  correction_b_valid_alone.json
  inversion_after_merge_error.json
Dockerfile
docker-compose.yml
```
