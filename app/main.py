"""FastAPI 单页迁移 API。"""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from typing import Iterator

from fastapi import Depends, FastAPI, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, StrictStr

from . import service
from .db import connect, init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="地方志单页标注迁移 API",
    description=(
        "录入旧稿、补标、换稿，并查询现行版号、迁入标注与待复核清单；\n\n"
        "外发补标时可按标注编号遮盖受访者姓名，回收外发稿坐标后再入标注库。\n\n"
        "字符区间基于**去除换行后**的正文；两稿对齐只允许插字和删字。"
        "补标、换稿与外发都携带所见版号，过期会被拒绝并返回现行版号。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


@app.exception_handler(service.ServiceError)
async def service_error_handler(request: Request, exc: service.ServiceError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail()})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """请求体校验失败也使用统一的 detail.code，方便编校工具识别。"""

    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "code": "validation_error",
                "message": "请求参数不合法",
                "errors": jsonable_encoder(exc.errors()),
            }
        },
    )


def get_conn() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


class ManuscriptIn(BaseModel):
    text: StrictStr = Field(
        ...,
        description="旧稿全文；换行会被去掉后再计字符与坐标，去换行后最多 500 字符",
        examples=["光绪《XX县志》卷三：城东有永安桥，跨白水河。"],
    )


class AnnotationIn(BaseModel):
    version: StrictInt = Field(..., description="所见现行版号", ge=1)
    start: StrictInt = Field(..., description="标注起点（去换行后字符下标，含）", ge=0)
    end: StrictInt = Field(..., description="标注终点（去换行后字符下标，不含）", ge=1)
    label: StrictStr = Field(
        ..., min_length=1, description="标注内容，例如人名或地名", examples=["永安桥"]
    )


class ReplaceIn(BaseModel):
    version: StrictInt = Field(..., description="所见现行版号", ge=1)
    text: StrictStr = Field(
        ...,
        description="新稿全文；换行会被去掉后再计字符，去换行后最多 500 字符",
        examples=["光绪《XX县志》卷三：城东有永安桥，跨白水河。桥始建于明。"],
    )


class DispatchPackIn(BaseModel):
    version: StrictInt = Field(..., description="所见现行版号", ge=1)
    mask_annotation_ids: list[StrictInt] = Field(
        ...,
        min_length=1,
        description=(
            "要遮盖的标注编号（必须属于该版）。相交的区间会合并，"
            "每段遮盖成一个「□」"
        ),
        examples=[[1, 2]],
    )


class ReturnedIntervalIn(BaseModel):
    start: StrictInt = Field(
        ..., description="外发稿区间起点（含），基于遮盖后的正文", ge=0
    )
    end: StrictInt = Field(
        ..., description="外发稿区间终点（不含），须完整落在同一段未遮盖原文内", ge=1
    )
    label: StrictStr = Field(
        ..., min_length=1, description="标注内容，例如地名", examples=["永安桥"]
    )


class DispatchClaimIn(BaseModel):
    annotations: list[ReturnedIntervalIn] = Field(
        ...,
        description="外发稿中的标注区间；任一区间无效则整包退回",
        examples=[
            [
                {"start": 0, "end": 3, "label": "武昌府"},
                {"start": 6, "end": 8, "label": "永安桥"},
            ]
        ],
    )


@app.get("/health", tags=["运维"], summary="健康检查")
def health():
    return {"status": "ok"}


@app.post(
    "/api/manuscripts",
    tags=["稿件"],
    summary="录入旧稿（仅首次）",
    description="建立第 1 版。已存在旧稿时返回 409，请改用换稿接口。",
)
def create_manuscript(
    payload: ManuscriptIn, conn: sqlite3.Connection = Depends(get_conn)
):
    return service.create_manuscript(conn, payload.text)


@app.get(
    "/api/current", tags=["稿件"], summary="查询现行版及其标注和复核清单"
)
def get_current(conn: sqlite3.Connection = Depends(get_conn)):
    return service.get_current(conn)


@app.get("/api/versions", tags=["稿件"], summary="列出版次摘要")
def list_versions(conn: sqlite3.Connection = Depends(get_conn)):
    return {"versions": service.list_versions(conn)}


@app.get(
    "/api/versions/{version}",
    tags=["稿件"],
    summary="按版号查询稿件（换版失败后仍可查旧稿及其标注）",
)
def get_version(version: int, conn: sqlite3.Connection = Depends(get_conn)):
    return service.get_version(conn, version)


@app.post(
    "/api/annotations",
    tags=["标注"],
    summary="补标（须带所见版号）",
    description=(
        "比对期间仍可正常补标。版号与现行版不一致时返回 409，"
        "detail.current_version 为现行版号。"
    ),
)
def add_annotation(
    payload: AnnotationIn, conn: sqlite3.Connection = Depends(get_conn)
):
    return service.add_annotation(
        conn,
        seen_version=payload.version,
        start=payload.start,
        end=payload.end,
        label=payload.label,
    )


@app.post(
    "/api/versions",
    tags=["稿件"],
    summary="换稿并迁移标注（须带所见版号）",
    description=(
        "新稿、迁入的标注与待复核清单在同一事务中生效。"
        "两稿对齐只允许插字和删字；只有一条标注在所有增删次数最少的对齐中"
        "都完整对应到新稿同一连续区间时才会迁入。比对期间新增的旧稿标注"
        "会在换稿时一并处理。失败时保留旧稿及其标注可查。"
    ),
)
def replace_manuscript(
    payload: ReplaceIn, conn: sqlite3.Connection = Depends(get_conn)
):
    return service.replace_manuscript(
        conn, seen_version=payload.version, raw_text=payload.text
    )


@app.get(
    "/api/annotations",
    tags=["标注"],
    summary="查询某版标注（默认现行版）",
)
def list_annotations(
    version: int | None = Query(None, ge=1, description="版号，默认现行版"),
    conn: sqlite3.Connection = Depends(get_conn),
):
    return service.list_annotations(conn, version)


@app.get(
    "/api/reviews",
    tags=["复核"],
    summary="查询待复核清单",
    description="不传 version 时查询现行版；每项附原标注与旧稿片段。",
)
def get_reviews(
    version: int | None = Query(None, ge=1, description="版号，默认现行版"),
    conn: sqlite3.Connection = Depends(get_conn),
):
    return service.get_reviews(conn, version)


@app.post(
    "/api/dispatch-packs",
    tags=["外发补标"],
    summary="创建外发补标包（遮盖受访者姓名）",
    description=(
        "按编校员给定的现行版号与标注编号，把正文中对应区间遮盖成「□」后打包。"
        "相交的遮盖区间合并为一段，每段只保留一个「□」。"
        "响应只返回包号与遮盖后的正文，供外发标注使用。"
    ),
)
def create_dispatch_pack(
    payload: DispatchPackIn, conn: sqlite3.Connection = Depends(get_conn)
):
    return service.create_dispatch_pack(
        conn,
        seen_version=payload.version,
        mask_annotation_ids=payload.mask_annotation_ids,
    )


@app.get(
    "/api/dispatch-packs/{pack_no}",
    tags=["外发补标"],
    summary="查询外发补标包（包号与遮盖后正文）",
)
def get_dispatch_pack(pack_no: str, conn: sqlite3.Connection = Depends(get_conn)):
    return service.get_dispatch_pack(conn, pack_no)


@app.post(
    "/api/dispatch-packs/{pack_no}/claims",
    tags=["外发补标"],
    summary="回收外发补标（区间基于外发稿，含起点不含终点）",
    description=(
        "每条区间必须完整落在同一段未遮盖原文内，服务端按发包时的映射换算回原稿坐标。"
        "首次回收时只要有无效区间或原稿已换版，整包退回且不落库。"
        "已收包原样重送返回首次回执（换版、重启后亦同）；同号异文返回 409 claim_mismatch。"
    ),
)
def claim_dispatch_pack(
    pack_no: str,
    payload: DispatchClaimIn,
    conn: sqlite3.Connection = Depends(get_conn),
):
    intervals = [
        (item.start, item.end, item.label) for item in payload.annotations
    ]
    return service.claim_dispatch_pack(conn, pack_no=pack_no, intervals=intervals)


@app.get(
    "/api/dispatch-packs/{pack_no}/receipt",
    tags=["外发补标"],
    summary="查询回收回执",
    description="包已回收时返回首次回执；尚未回收返回 404。",
)
def get_dispatch_receipt(
    pack_no: str, conn: sqlite3.Connection = Depends(get_conn)
):
    return service.get_dispatch_receipt(conn, pack_no)
