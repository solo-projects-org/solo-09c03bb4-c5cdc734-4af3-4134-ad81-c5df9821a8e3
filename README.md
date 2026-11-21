# 地方志单页标注迁移 API

OCR 重跑后，把编校员核过的**人名、地名标注**从旧稿迁移到新稿。FastAPI + SQLite，
单页、单文档、按版号串行演进。

## 启动

```bash
docker compose up --build
```

- Swagger：<http://localhost:8000/docs>
- 健康检查：`GET /health`
- SQLite 文件位于容器卷 `migration-data`，容器内路径 `/data/migration.db`

本机直接运行（需要 Python 3.11+）：

```bash
pip install -r requirements.txt -r requirements-dev.txt
uvicorn app.main:app --reload
pytest
```

## 规则落地

| 要求 | 实现 |
| --- | --- |
| 每稿去掉换行后最多 500 字符 | 删 `\r`、`\n` 后校验长度；所有坐标均基于去换行后的文本 |
| 标注按去换行后的字符区间填写 | `[start, end)` 半开区间，服务端只接受 `0 <= start < end <= len(text)` |
| 两稿对齐只允许插字、删字 | 最长公共子序列对齐；增删次数 = `len(old)+len(new)-2*LCS` |
| 只有所有最少增删对齐中都完整对应到同一连续区间才迁入 | 见下节 |
| 其余进待复核清单，附原标注与旧稿片段 | `review_items` 保存原区间、标注内容、旧稿全文与带「」的上下文片段 |
| 比对期间照常接收旧稿标注 | 比对在写事务之外进行；换稿取写锁后重新读取标注集合 |
| 换稿时连同原有标注一起处理 | 同一事务内写新稿、迁入标注、复核清单 |
| 新稿、迁入标注、复核清单一并生效 | `BEGIN IMMEDIATE` 单事务；任一步失败整体回滚 |
| 换版失败仍可查旧稿及其标注 | 失败不产生新版；`GET /api/versions/{version}` 可查任一世代的正文与标注 |
| 补标、换版须带所见版号 | 二者都要求 `version`；过期返回 `409 stale_version` 并附 `current_version` |

### 迁移判定

对旧稿区间 `[s, e)`：

1. 标注内每个字符都必须在**每一条**增删次数最少的对齐里被保留（不能被删）；
2. 标注首字符在每一条最优对齐中对应到唯一的新稿位置 `j0`；
3. 标注末字符在每一条最优对齐中对应到唯一的新稿位置 `j1`。

三条都满足时迁入 `[j0, j1+1)`；新稿在区间内部插入的字会自然包含在区间里。否则进入复核：

- `partially_deleted`：存在某条最优对齐会删去标注内的字；
- `ambiguous_position`：重复词导致边界在最优对齐中可落到不同位置（例如把
  `甲公司甲公司` 删成 `甲公司` 时，两处“甲公司”谁留下无法唯一确定）；
- `invalid_span`：历史数据中的越界区间。

实现上没有枚举对齐，而是用前缀 / 后缀 LCS 表判断“字符 `i` 是否可能映射到 `j`、
是否可能被删除”，枚举最优对齐的交叉验证见 `tests/test_align.py`（全量穷举
`a`/`b` 长度 1–4 的所有组合）。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/manuscripts` | 首次录入旧稿，建立第 1 版；已有旧稿返回 409 |
| `GET` | `/api/current` | 现行版正文、迁入标注、待复核清单、对齐统计 |
| `GET` | `/api/versions` | 各版摘要（字符数、标注数、复核数） |
| `GET` | `/api/versions/{version}` | 指定版正文与标注；旧版可查 |
| `POST` | `/api/annotations` | 补标：`version, start, end, label` |
| `GET` | `/api/annotations?version=` | 查标注，省略版号为现行版 |
| `POST` | `/api/versions` | 换稿：`version, text`，成功后版号 +1 |
| `GET` | `/api/reviews?version=` | 待复核清单，省略版号为现行版 |

错误统一为 `{"detail": {"code": ..., "message": ...}}`：

- `409 stale_version` → `seen_version`、`current_version`
- `422 validation_error` → 长度 / 区间 / 空标注等
- `404 not_found` → 未录入或版号不存在

标注坐标示例（去换行后）：

```
0 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16
公 司 迁 址 通 告 甲 公 司 成 立 于  甲  公 司  旧  址
```

## Swagger 换版演示

1. `POST /api/manuscripts` 录入
   `{"text": "公司迁址通告\n甲公司成立于甲公司旧址。甲公司主营地方志整理。"}`
2. `POST /api/annotations` 补两条标（同一页重复词，靠上下文区分）：
   `{"version": 1, "start": 6, "end": 9, "label": "甲公司（首处）"}`
   `{"version": 1, "start": 12, "end": 15, "label": "甲公司（次处）"}`
3. 再补一条会被删掉的：`{"version": 1, "start": 15, "end": 17, "label": "旧址"}`
4. `POST /api/versions`
   `{"version": 1, "text": "公司迁址通告\n甲公司成立于甲公司新址。甲公司主营地方志整理。"}`
   → 第 2 版；两处“甲公司”迁入（`[6,9)`、`[12,15)` 不变），“旧址”进入待复核。
5. 再用 `{"version": 1, ...}` 换稿 → `409 stale_version`，`current_version: 2`。
6. `GET /api/versions/1` → 旧稿及其三条标注仍可查。

`demo/swagger_demo.py` 存有同样请求体，可直接复制进 Swagger「试用」。

## 测试

```bash
pytest -q
```

- `tests/test_align.py`：LCS 迁移判定的单元测试 + 穷举所有最优对齐的交叉验证
  （`a`/`b` 长度 1–4 全部 900 组，另随机用例在开发中验证过 5000 组）。
- `tests/test_api.py`：录入、去换行坐标、长度上限、越界、版号过期、换版迁移、
  失败回滚、旧版可查、重复词两例、比对期间补标。
- `tests/test_concurrency.py`：线程级“比对中补标”，以及换版后旧版号补标被拒。

## 设计备注

- **比对在事务外**：500×500 的 LCS 约 0.15 s，但把它放在写锁外可以让编校员在换稿
  比对期间继续补标；加锁后会再次核对版号/正文，并重新读取全部标注。
- **版本不可变**：每次换稿新增一行 `editions`，旧版正文与旧标注原样保留，失败或
  回查都不受影响。
- **重复词歧义**：判定比“能映射到某个位置”更严格，要求所有最优对齐下边界唯一，
  否则宁可让编校员复核，避免标错到另一处同名地名。

## 未完成 / 未验证

- 本环境没有 Docker，`Dockerfile` 与 `docker-compose.yml` 只做了语法与配置检查，
  **未实际构建镜像或运行 compose**；API 本身已用等价命令 `uvicorn` 验证。
- SQLite 单文件 + 单进程 `uvicorn` 足够本单页场景；未做多副本、备份/恢复、鉴权。
- 待复核清单目前只读（换版自动生成），没有“复核后手工补标/忽略”的接口。
- 标注只支持连续区间，不支持一条标注拆成多段或合并。
- 版本号单调递增，没有回滚到旧版或删除版次的接口。
