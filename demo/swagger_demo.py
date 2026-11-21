#!/usr/bin/env python3
"""Swagger 换版演示步骤（可复制到 /docs 各接口的「试用」中逐条执行）。

启动：
    docker compose up --build
打开：
    http://localhost:8000/docs

演示脚本：
    1. POST /api/manuscripts   录入旧稿
    2. POST /api/annotations   比对前补两条标
    3. POST /api/annotations   再补一条覆盖“旧址”，换稿时会被删
    4. POST /api/versions      用版号 1 换稿 -> 第 2 版
    5. POST /api/versions      再用版号 1 -> 409，返回现行版 2
    6. GET  /api/versions/1    旧稿及其标注仍可查

坐标按**去掉换行后**的正文计。旧稿/新稿去换行后：
    0 1 2 3 4 5 6 7 8 9 ...
    公 司 迁 址 通 告 甲 公 司 成 立 于 甲 公 司 旧 址 。 ...
    第一处“甲公司”= [6, 9)
    第二处“甲公司”= [12, 15)
    “旧址”      = [15, 17)
"""

import json

OLD = "公司迁址通告\n甲公司成立于甲公司旧址。甲公司主营地方志整理。"
NEW = "公司迁址通告\n甲公司成立于甲公司新址。甲公司主营地方志整理。"

CREATE = {"text": OLD}

ANNOTATIONS = [
    {"version": 1, "start": 6, "end": 9, "label": "甲公司（首处）"},
    {"version": 1, "start": 12, "end": 15, "label": "甲公司（次处）"},
    # 覆盖“旧址”；新稿只把“旧”改成“新”，按最短增删对齐该字被删除，
    # 标注无法完整保留，进入待复核清单。
    {"version": 1, "start": 15, "end": 17, "label": "旧址"},
]

REPLACE = {"version": 1, "text": NEW}

STALE_REPLACE = {"version": 1, "text": "任意新稿"}

if __name__ == "__main__":
    print(
        json.dumps(
            {
                "POST /api/manuscripts": CREATE,
                "POST /api/annotations": ANNOTATIONS,
                "POST /api/versions": REPLACE,
                "POST /api/versions (stale)": STALE_REPLACE,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
