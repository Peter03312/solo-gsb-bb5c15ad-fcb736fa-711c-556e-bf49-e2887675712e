#!/usr/bin/env bash
# 场馆疏散通道编排服务 —— curl 调用示例
#
# 用法:
#   ./examples/curl-examples.sh            # 默认 http://127.0.0.1:8080
#   BASE_URL=http://127.0.0.1:8000 ./examples/curl-examples.sh
#
# 脚本会用一块全新的空数据启动流程（先清空数据卷请见 README），
# 逐条演示: 建通道 -> 建出口 -> 建人群组 -> 查询编排 -> 推进时钟 ->
# 加封闭(被拒绝 / 被接受) -> 新增高优先级组 -> 不可安排原因。
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8080}"
C() { curl -sS -H 'Content-Type: application/json' "$@"; }

echo "### 0. 健康检查"
C "$BASE_URL/health"; echo

echo "### 1. 批量录入有向通道 (编号, 起点, 终点, 通行分钟, 每分钟容量, 封闭[start,end))"
C -X POST "$BASE_URL/api/corridors" -d '[
  {"corridor": 1, "source": 1, "target": 2, "travel_minutes": 2, "capacity": 20},
  {"corridor": 2, "source": 1, "target": 3, "travel_minutes": 1, "capacity": 30},
  {"corridor": 3, "source": 3, "target": 5, "travel_minutes": 3, "capacity": 25,
   "closures": [{"start": 10, "end": 15}]},
  {"corridor": 4, "source": 2, "target": 4, "travel_minutes": 2, "capacity": 20},
  {"corridor": 5, "source": 4, "target": 5, "travel_minutes": 1, "capacity": 40},
  {"corridor": 6, "source": 4, "target": 6, "travel_minutes": 2, "capacity": 20},
  {"corridor": 7, "source": 5, "target": 6, "travel_minutes": 1, "capacity": 40},
  {"corridor": 8, "source": 2, "target": 6, "travel_minutes": 4, "capacity": 15}
]' | python3 -c "import json,sys; print(len(json.load(sys.stdin)['corridors']), 'corridors loaded')"

echo "### 2. 登记出口位置"
C -X POST "$BASE_URL/api/exits" -d '{"node": 5}' > /dev/null
C -X POST "$BASE_URL/api/exits" -d '{"node": 6}' > /dev/null
echo "exits = 5, 6"

echo "### 3. 录入人群组 (编号, 人数, 起点, 最早出发, 优先级, 截止分钟)"
C -X POST "$BASE_URL/api/groups" -d '{"group":1,"people":25,"source":1,"earliest":0,"priority":10,"deadline":30}' > /dev/null
C -X POST "$BASE_URL/api/groups" -d '{"group":2,"people":20,"source":4,"earliest":0,"priority":5, "deadline":20}' > /dev/null
C -X POST "$BASE_URL/api/groups" -d '{"group":3,"people":15,"source":3,"earliest":2,"priority":5, "deadline":20}' > /dev/null
echo "groups 1,2,3 loaded"

echo "### 4. 查询全部编排结果 (按优先级/最早出发/组编号)"
C "$BASE_URL/api/plans" | python3 -c "
import json,sys
for p in json.load(sys.stdin)['plans']:
    if p['status'] == 'SCHEDULED':
        print(f\"  G{p['group']}: 出发 {p['departure_minute']} 到达 {p['arrival_minute']} \"
              f\"出口 {p['exit']} 通道序列 {p['route_corridors']}\")
    else:
        print(f\"  G{p['group']}: 无法安排 -- {p['reason']}\")
"

echo "### 5. 查询单个组"
C "$BASE_URL/api/groups/1/plan"; echo

echo "### 6. 推进当前分钟 0 -> 1 (已出发组路线随即固定)"
C -X POST "$BASE_URL/api/clock/advance" -d '{"minutes":1}' \
  | python3 -c "import json,sys; print('current_minute =', json.load(sys.stdin)['current_minute'])"

echo "### 7. 封闭触及已出发组 G1 的剩余路线 -> 整次拒绝 409"
C -w "\nHTTP %{http_code}\n" -X POST "$BASE_URL/api/closures" \
  -d '{"corridor":3,"start":3,"end":6}'

echo "### 8. 封闭只落在 G1 离开通道之后 (半开边界, 不触及) -> 接受, 未出发组重排"
C -X POST "$BASE_URL/api/closures" -d '{"corridor":3,"start":4,"end":7}' \
  | python3 -c "
import json,sys
for p in json.load(sys.stdin)['plans']:
    print(f\"  G{p['group']}: departed={p['departed']} 出发 {p['departure_minute']} \"
          f\"到达 {p['arrival_minute']} 通道 {p['route_corridors']}\")
"

echo "### 9. 新增更高优先级人群组 -> 未出发组重新按序编排"
C -X POST "$BASE_URL/api/groups" \
  -d '{"group":9,"people":20,"source":4,"earliest":1,"priority":9,"deadline":20}' \
  | python3 -c "
import json,sys
for p in json.load(sys.stdin)['plans']:
    print(f\"  G{p['group']}(prio {p['priority']}): {p['status']} \"
          f\"出发 {p['departure_minute']} 到达 {p['arrival_minute']}\")
"

echo "### 10. 超过所有通道容量的组 -> 给出截止前无法安排的原因"
C -X POST "$BASE_URL/api/groups" \
  -d '{"group":10,"people":999,"source":1,"earliest":1,"priority":1,"deadline":20}' > /dev/null
C "$BASE_URL/api/groups/10/plan" \
  | python3 -c "import json,sys; p=json.load(sys.stdin); print(' ', p['status'], '-', p['reason'])"

echo "### 11. 导出完整态势"
C "$BASE_URL/api/state" | python3 -m json.tool | head -40
