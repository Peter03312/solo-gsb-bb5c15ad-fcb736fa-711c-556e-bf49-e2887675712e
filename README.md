# 场馆疏散通道编排服务

按人群组优先级，为每组（**不可拆分**）编排「截止分钟前最早到达出口」的疏散路线与出发分钟，
严格满足通道**每分钟容量**与**左闭右开封闭时段**约束；时钟推进后已出发组的路线与占用固定，
新增人群组或封闭只重排未出发组，封闭一旦触及已出发组的剩余路线则**整次拒绝**。

- 技术栈：Python 3.11 · FastAPI · SQLite（WAL）· Uvicorn · Pydantic v2
- 并发：进程内串行化 + `BEGIN IMMEDIATE` 事务，任何变更与重排在同一事务语义内完成，
  不会超用容量，也不会留下「部分重排」。

---

## 1. 一键启动（Docker Compose）

```bash
docker compose up --build -d
# 服务地址: http://127.0.0.1:8080
# Swagger 文档: http://127.0.0.1:8080/docs
curl http://127.0.0.1:8080/health
```

首次启动会自动：

1. 安装 `requirements.txt` 中的全部依赖（镜像构建阶段）；
2. 初始化 SQLite 表结构（`/data/evacuation.db`，命名卷持久化）；
3. 当数据库为空时载入演示数据（`app/seed.py`）并完成首轮编排。

停止 / 重置：

```bash
docker compose down          # 停止, 保留数据
docker compose down -v       # 停止并删除数据卷, 下次启动重新初始化+载入演示数据
```

可选配置（docker-compose.yml 中可改，或用环境变量覆盖）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `EVAC_HOST_PORT` | `8080` | 宿主机映射端口（容器内固定监听 8000） |
| `EVAC_DB_PATH` | `/data/evacuation.db` | SQLite 数据库文件路径 |
| `EVAC_PORT` | `8000` | 容器内 uvicorn 监听端口 |
| `EVAC_SEED` | `1` | 库为空时是否自动载入演示数据，`0` 表示空库启动 |

## 2. 不用 Docker，本地运行

```bash
pip install -r requirements.txt
export EVAC_DB_PATH=./evacuation.db     # 可选, 默认 /data/evacuation.db
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## 3. 调用示例

服务自带两种可直接运行的示例（针对**空库**运行；Docker 默认带演示数据，
如需跑示例的建图流程，先 `docker compose down -v` 并把 compose 里的
`EVAC_SEED` 设为 `0` 再启动）：

```bash
# curl 版
BASE_URL=http://127.0.0.1:8080 bash examples/curl-examples.sh

# Python 版 (需要 pip install httpx)
python examples/python_client.py
```

## 4. HTTP API

Base URL：`http://127.0.0.1:8080`，所有请求/响应均为 JSON。
交互式文档见 `/docs`（Swagger）与 `/redoc`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| GET | `/api/state` | 完整态势：当前分钟、全部通道/封闭/出口/组 + 全部编排 |
| GET | `/api/plans` | 全部组的编排结果（已出发组在前，未出发组按编排顺序） |
| GET | `/api/groups/{number}/plan` | 单个组的路线、出发/到达分钟或无法安排原因 |
| POST | `/api/corridors` | 新增通道，支持单对象或数组（数组为原子批次） |
| POST | `/api/exits` | 登记出口位置 |
| POST | `/api/groups` | 新增人群组（触发未出发组全量重排） |
| POST | `/api/closures` | 新增 `[start,end)` 封闭；触及已出发剩余路线返回 **409** |
| POST | `/api/clock/advance` | 当前分钟向前推进 N（N≥1） |

### 字段约定

- 通道 `POST /api/corridors`

  ```json
  {
    "corridor": 3,
    "source": 3,
    "target": 5,
    "travel_minutes": 3,
    "capacity": 25,
    "closures": [{"start": 10, "end": 15}]
  }
  ```

  通行分钟与每分钟容量均为正整数；封闭为**左闭右开** `[start, end)`（分钟），
  即占用分钟集合为 `start, start+1, …, end-1`。

- 人群组 `POST /api/groups`

  ```json
  {"group": 1, "people": 25, "source": 1,
   "earliest": 0, "priority": 10, "deadline": 30}
  ```

  - `people` 正整数，每组**不可拆分**；
  - `priority` 数值越大优先级越高；
  - `earliest` 最早出发分钟；`deadline` 规划截止分钟（到达分钟须 `≤ deadline`）。

- 封闭 `POST /api/closures`：`{"corridor": 3, "start": 4, "end": 7}`

- 时钟 `POST /api/clock/advance`：`{"minutes": 1}`

### 响应示例（`GET /api/plans` 节选）

```json
{
  "current_minute": 0,
  "plans": [
    {
      "group": 1,
      "schedule_order": 0,
      "priority": 10,
      "people": 25,
      "status": "SCHEDULED",
      "departed": false,
      "departure_minute": 0,
      "arrival_minute": 4,
      "exit": 5,
      "route_corridors": [2, 3],
      "reason": null,
      "legs": [
        {"corridor": 2, "from": 1, "to": 3,
         "enter_minute": 0, "leave_minute": 1, "occupies_minutes": [0]},
        {"corridor": 3, "from": 3, "to": 5,
         "enter_minute": 1, "leave_minute": 4, "occupies_minutes": [1, 2, 3]}
      ]
    },
    {"group": 10, "status": "UNSCHEDULABLE", "departure_minute": null,
     "arrival_minute": null, "exit": null, "route_corridors": [],
     "reason": "no directed route from source to any exit using corridors with capacity >= 999"}
  ]
}
```

无法安排的 `reason` 会区分：最早出发已晚于截止、静态不可达（含所有通道容量都小于组人数）、
截止前容量/封闭阻塞全部可行出发、截止已过等情况。

## 5. 编排规则（与实现的对应）

1. **顺序**：优先级从高到低；同级按最早出发分钟升序；再同级按组编号升序。
   逐组在「已固定占用 + 更高顺序组已占容量」之上安排。
2. **择优**：在截止分钟内选择**到达分钟最早**的（出发时间，路线）；
   到达分钟相同时，先比出口编号，再比通道编号序列（字典序）。
3. **容量**：通道在某分钟上，所有占用该分钟的组人数之和 `≤ capacity`；
   组不可拆分，不能把人分摊到两条通道。
4. **封闭**：通行占用窗口 `[enter, enter + travel)` 与任何封闭 `[start, end)`
   有重叠即不可使用；端点相接（一个的结束 = 另一个的开始）不算重叠。
5. **时钟**：当前分钟从 0 起。`advance N` 后，凡出发分钟严格小于当前分钟的组视为已出发，
   其路线与逐分钟占用写死；之后的新增组 / 封闭只对未出发组全量重排。
6. **拒绝**：新封闭若与任一已出发组**剩余路线**（尚未完全走到的通道分钟）重叠，
   该封闭整体回滚并返回 `409 {"error":"conflict", ...}`，已有编排不变。
7. **搜索**：对单组做「分钟分层」图搜索——状态为（节点，分钟，已访问节点集），
   边为等待 1 分钟（仅起点出发前）或进入一条窗口内容量充足且不封闭的通道；
   同层标签按（通道序列，已访问集）做 Pareto 支配剪枝，首个含出口的分钟层即最早到达。

## 6. 测试

```bash
pip install -r requirements.txt pytest httpx
python -m pytest -q
```

覆盖：优先级/最早出发/组编号排序、到达并列时出口与通道序列择优、逐分钟容量不超限、
左闭右开边界、推进后已出发组固定、危险封闭 409、未出发组重排、新组抢占、
不可达/截止原因、批次原子性，以及 8 线程并发下容量不超用、变更不产生部分重排。

## 7. 目录结构

```
app/
  main.py        # FastAPI 路由、串行化、响应组装
  scheduler.py   # 时间分层搜索、占用、重排、封闭冲突判定、原子事务
  models.py      # Pydantic 请求模型与校验
  database.py    # SQLite 连接与建表
  seed.py        # 首次启动的演示数据
examples/
  curl-examples.sh
  python_client.py
tests/           # pytest + FastAPI TestClient
Dockerfile
docker-compose.yml
requirements.txt
```
