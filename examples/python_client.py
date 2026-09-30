#!/usr/bin/env python3
"""场馆疏散通道编排服务 —— Python (httpx) 调用示例。

演示一次完整生命周期:
  1. 录入有向通道(含左闭右开封闭时段)、出口、人群组
  2. 读取逐组编排: 路线 / 出发分钟 / 到达分钟 / 无法安排原因
  3. 推进当前分钟
  4. 触及已出发组剩余路线的封闭被 409 整次拒绝
  5. 安全封闭被接受, 未出发组自动重排
  6. 新增更高优先级组后, 未出发组重新按序编排

直接运行:  python examples/python_client.py
可选环境变量: BASE_URL (默认 http://127.0.0.1:8080)
"""
from __future__ import annotations

import os
import textwrap

import httpx

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8080")


def show(title: str, plans: list[dict]) -> None:
    print(f"\n== {title} ==")
    for p in plans:
        if p["status"] == "SCHEDULED":
            print(
                f"  G{p['group']:<2} prio={p['priority']:<2} "
                f"depart={p['departure_minute']:<3} arrive={p['arrival_minute']:<3} "
                f"exit={p['exit']} route={p['route_corridors']} "
                f"departed={p['departed']}"
            )
        else:
            print(
                f"  G{p['group']:<2} UNSCHEDULABLE reason={p['reason']}"
            )


def main() -> None:
    c = httpx.Client(base_url=BASE_URL, timeout=10)
    print("health:", c.get("/health").json())

    # 1) 网络: 有向通道 + 封闭时段 [start, end)
    corridors = [
        # c1: 1->2, 2 分钟, 20 人/分钟
        {"corridor": 1, "source": 1, "target": 2, "travel_minutes": 2, "capacity": 20},
        # c2: 1->3, 1 分钟, 30 人/分钟
        {"corridor": 2, "source": 1, "target": 3, "travel_minutes": 1, "capacity": 30},
        # c3: 3->5, 3 分钟, 25 人/分钟, 分钟 [10,15) 封闭
        {"corridor": 3, "source": 3, "target": 5, "travel_minutes": 3,
         "capacity": 25, "closures": [{"start": 10, "end": 15}]},
        {"corridor": 4, "source": 2, "target": 4, "travel_minutes": 2, "capacity": 20},
        {"corridor": 5, "source": 4, "target": 5, "travel_minutes": 1, "capacity": 40},
        {"corridor": 6, "source": 4, "target": 6, "travel_minutes": 2, "capacity": 20},
        {"corridor": 7, "source": 5, "target": 6, "travel_minutes": 1, "capacity": 40},
        {"corridor": 8, "source": 2, "target": 6, "travel_minutes": 4, "capacity": 15},
    ]
    r = c.post("/api/corridors", json=corridors)
    r.raise_for_status()

    # 2) 出口
    for node in (5, 6):
        c.post("/api/exits", json={"node": node}).raise_for_status()

    # 3) 人群组: 每组不可拆分
    groups = [
        {"group": 1, "people": 25, "source": 1, "earliest": 0, "priority": 10, "deadline": 30},
        {"group": 2, "people": 20, "source": 4, "earliest": 0, "priority": 5,  "deadline": 20},
        {"group": 3, "people": 15, "source": 3, "earliest": 2, "priority": 5,  "deadline": 20},
    ]
    for g in groups:
        c.post("/api/groups", json=g).raise_for_status()

    show("初始编排 (now=0)", c.get("/api/plans").json()["plans"])

    # 4) 推进当前分钟; 已出发组路线固定
    c.post("/api/clock/advance", json={"minutes": 1}).raise_for_status()

    # 5) 触及已出发组剩余路线的封闭 -> 整次拒绝
    bad = c.post("/api/closures", json={"corridor": 3, "start": 3, "end": 6})
    print("\n== 危险封闭 ==")
    print(f"  HTTP {bad.status_code}: {bad.json()['detail']}")

    # 6) 安全封闭 (G1 在分钟 4 离开 c3, [4,7) 不重叠) -> 接受并重排未出发组
    ok = c.post("/api/closures", json={"corridor": 3, "start": 4, "end": 7})
    ok.raise_for_status()
    show("加入安全封闭 [4,7) 后", ok.json()["plans"])

    # 7) 新增更高优先级组: 只有未出发组被重排
    r = c.post("/api/groups", json={
        "group": 9, "people": 20, "source": 4,
        "earliest": 1, "priority": 9, "deadline": 20,
    })
    r.raise_for_status()
    show("新增高优先级组 G9 后", r.json()["plans"])

    # 8) 不可安排原因
    c.post("/api/groups", json={
        "group": 10, "people": 999, "source": 1,
        "earliest": 1, "priority": 1, "deadline": 20,
    }).raise_for_status()
    p = c.get("/api/groups/10/plan").json()
    print("\n== 无法安排 ==")
    print(textwrap.fill(f"  G10: {p['status']} - {p['reason']}", 80))


if __name__ == "__main__":
    main()
