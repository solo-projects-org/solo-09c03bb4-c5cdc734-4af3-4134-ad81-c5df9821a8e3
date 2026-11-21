#!/usr/bin/env python3
"""Swagger 演示步骤（可复制到 /docs 各接口的「试用」中逐条执行）。

启动：
    docker compose up --build
打开：
    http://localhost:8000/docs

换版演示：
    1. POST /api/manuscripts   录入旧稿
    2. POST /api/annotations   比对前补两条标
    3. POST /api/annotations   再补一条覆盖“旧址”，换稿时会被删
    4. POST /api/versions      用版号 1 换稿 -> 第 2 版
    5. POST /api/versions      再用版号 1 -> 409，返回现行版 2
    6. GET  /api/versions/1    旧稿及其标注仍可查

外发补标演示（请求体见文件末尾常量，说明见 README）：
    1. POST /api/manuscripts                     录入含两位受访者姓名的页面
    2. POST /api/annotations x2                  把两处姓名标出来，记下 id
    3. POST /api/dispatch-packs                  遮掉姓名，取得包号与外发稿
    4. POST /api/dispatch-packs/{pack}/claims    按外发稿坐标提交地名
    5. 原样重送                                   返回同一回执
    6. 提交跨过「□」的区间                         首次回收整包退回

换版坐标按**去掉换行后**的正文计。旧稿/新稿去换行后：
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

# ---------------------------------------------------------------------------
# 外发补标演示（与 README「Swagger 外发补标演示」一致）
# ---------------------------------------------------------------------------
#
# 正文（去换行后 34 字）：
#   0 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 33
#   光 绪 《 X X 县 志 》 卷 三 ： 张 三 在 城 东 永 安 桥 见 李 四 。 张 三 说 永 安 桥 始 建 于 明 。
#
#   两处“张三” = [11, 13)、[23, 25)
#   外发时各换成一个「□」，遮盖后正文：
#     光绪《XX县志》卷三：□在城东永安桥见李四。□说永安桥始建于明。
#   外发稿坐标（含起点、不含终点）：
#     “城东”   = [13, 15)  -> 原稿 [14, 16)
#     “永安桥” = [15, 18)  -> 原稿 [16, 19)

DISPATCH_TEXT = "光绪《XX县志》卷三：张三在城东永安桥见李四。\n张三说永安桥始建于明。"

NAME_ANNOTATIONS = [
    {"version": 1, "start": 11, "end": 13, "label": "张三"},
    {"version": 1, "start": 23, "end": 25, "label": "张三"},
]

# 标注编号取决于数据库当前状态；第 1 次录入后通常为 1、2。
DISPATCH_CREATE = {"version": 1, "mask_annotation_ids": [1, 2]}

DISPATCH_CLAIM = {
    "annotations": [
        {"start": 13, "end": 15, "label": "城东"},
        {"start": 15, "end": 18, "label": "永安桥"},
    ]
}

# 首次回收时跨过遮盖处 -> 409 claim_rejected，错误项 reason=crosses_masked_span。
INVALID_CLAIM = {
    "annotations": [
        {"start": 10, "end": 13, "label": "卷三："},
    ]
}


if __name__ == "__main__":
    print(
        json.dumps(
            {
                "外发演示 / POST /api/manuscripts": {"text": DISPATCH_TEXT},
                "外发演示 / POST /api/annotations": NAME_ANNOTATIONS,
                "外发演示 / POST /api/dispatch-packs": DISPATCH_CREATE,
                "外发演示 / POST /api/dispatch-packs/{pack_no}/claims": DISPATCH_CLAIM,
                "外发演示 / 无效区间（整包退回）": INVALID_CLAIM,
                "换版演示 / POST /api/manuscripts": CREATE,
                "换版演示 / POST /api/annotations": ANNOTATIONS,
                "换版演示 / POST /api/versions": REPLACE,
                "换版演示 / POST /api/versions (stale)": STALE_REPLACE,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
