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
        "录入旧稿、补标、换稿，并查询现行版号、迁入标注与待复核清单。\n\n"
        "字符区间基于**去除换行后**的正文；两稿对齐只允许插字和删字。"
        "补标与换稿都必须携带所见版号，过期会被拒绝并返回现行版号。"
    ),
    version="1.0.0",
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
