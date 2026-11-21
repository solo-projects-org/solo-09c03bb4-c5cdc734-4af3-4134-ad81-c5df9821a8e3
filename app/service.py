"""业务服务：录入、补标、换稿与查询。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .align import (
    AlignmentTooLarge,
    AnnotationLike,
    compute_alignment,
    map_annotations,
)
from .text import normalize_text

EXCERPT_CONTEXT = 12


class ServiceError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, **extra):
        super().__init__(message)
        self.message = message
        self.extra = extra

    def detail(self) -> dict:
        return {"code": self.code, "message": self.message, **self.extra}


class NotFound(ServiceError):
    status_code = 404
    code = "not_found"


class Conflict(ServiceError):
    status_code = 409
    code = "conflict"


class StaleVersion(Conflict):
    code = "stale_version"

    def __init__(self, seen_version: int, current_version: int):
        super().__init__(
            "所见版号已过期，请基于现行版重新操作",
            seen_version=seen_version,
            current_version=current_version,
        )


class ValidationFailure(ServiceError):
    status_code = 422
    code = "validation_error"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_text(raw: str) -> str:
    text = normalize_text(raw)
    if len(text) > 500:
        raise ValidationFailure(
            f"正文去除换行后最多 500 字符，当前 {len(text)} 字符"
        )
    return text


def _document_row(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM documents WHERE id = 1").fetchone()
    if row is None:
        raise NotFound("尚未录入旧稿，请先调用录入接口")
    return row


def _edition_row(conn: sqlite3.Connection, version: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM editions WHERE version = ?", (version,)
    ).fetchone()
    if row is None:
        raise NotFound(f"版次 {version} 不存在")
    return row


def _current_version(conn: sqlite3.Connection) -> int:
    return int(_document_row(conn)["current_version"])


def _annotation_rows(conn: sqlite3.Connection, version: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM annotations WHERE version = ? ORDER BY start, id",
        (version,),
    ).fetchall()


def _review_rows(conn: sqlite3.Connection, version: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM review_items WHERE version = ? ORDER BY id",
        (version,),
    ).fetchall()


def _annotation_dict(row: sqlite3.Row, text: str) -> dict:
    return {
        "id": row["id"],
        "version": row["version"],
        "start": row["start"],
        "end": row["end"],
        "label": row["label"],
        "text": text[row["start"] : row["end"]],
        "source_annotation_id": row["source_annotation_id"],
    }


def _review_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "version": row["version"],
        "source_version": row["source_version"],
        "source_annotation_id": row["source_annotation_id"],
        "start": row["start"],
        "end": row["end"],
        "label": row["label"],
        "old_text": row["old_text"],
        "old_fragment": row["excerpt"],
        "reason": row["reason"],
        "message": row["message"],
    }


def _edition_payload(
    conn: sqlite3.Connection, row: sqlite3.Row, *, is_current: bool
) -> dict:
    version = int(row["version"])
    text = row["text"]
    annotations = [_annotation_dict(r, text) for r in _annotation_rows(conn, version)]
    reviews = [_review_dict(r) for r in _review_rows(conn, version)]
    return {
        "version": version,
        "is_current": is_current,
        "text": row["text"],
        "source_version": row["source_version"],
        "created_at": row["created_at"],
        "char_count": len(row["text"]),
        "annotations": annotations,
        "review_items": reviews,
        "alignment": json.loads(row["alignment_json"])
        if row["alignment_json"]
        else None,
    }


def create_manuscript(conn: sqlite3.Connection, raw_text: str) -> dict:
    text = validate_text(raw_text)
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute("SELECT 1 FROM documents WHERE id = 1").fetchone()
        if existing:
            raise Conflict("旧稿已录入；如需换稿请调用换版接口")
        created = now_iso()
        conn.execute(
            "INSERT INTO editions (version, text, source_version, alignment_json, created_at)"
            " VALUES (1, ?, NULL, NULL, ?)",
            (text, created),
        )
        conn.execute(
            "INSERT INTO documents (id, current_version, updated_at) VALUES (1, 1, ?)",
            (created,),
        )
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
    row = _edition_row(conn, 1)
    return _edition_payload(conn, row, is_current=True)


def get_current(conn: sqlite3.Connection) -> dict:
    current = _current_version(conn)
    return _edition_payload(conn, _edition_row(conn, current), is_current=True)


def get_version(conn: sqlite3.Connection, version: int) -> dict:
    row = _edition_row(conn, version)
    current = _current_version(conn)
    return _edition_payload(conn, row, is_current=version == current)


def list_versions(conn: sqlite3.Connection) -> list[dict]:
    try:
        current = _current_version(conn)
    except NotFound:
        return []
    rows = conn.execute("SELECT * FROM editions ORDER BY version").fetchall()
    output = []
    for row in rows:
        version = int(row["version"])
        ann = conn.execute(
            "SELECT COUNT(*) AS c FROM annotations WHERE version = ?", (version,)
        ).fetchone()["c"]
        rev = conn.execute(
            "SELECT COUNT(*) AS c FROM review_items WHERE version = ?", (version,)
        ).fetchone()["c"]
        output.append(
            {
                "version": version,
                "is_current": version == current,
                "source_version": row["source_version"],
                "char_count": len(row["text"]),
                "annotation_count": ann,
                "review_count": rev,
                "created_at": row["created_at"],
            }
        )
    return output


def add_annotation(
    conn: sqlite3.Connection,
    *,
    seen_version: int,
    start: int,
    end: int,
    label: str,
) -> dict:
    label = label.strip()
    if not label:
        raise ValidationFailure("标注内容不能为空")
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = _current_version(conn)
        if seen_version != current:
            raise StaleVersion(seen_version, current)
        edition = _edition_row(conn, current)
        text = edition["text"]
        if not (0 <= start < end <= len(text)):
            raise ValidationFailure(
                f"标注区间 [{start}, {end}) 超出旧稿范围 "
                f"（去除换行后共 {len(text)} 字符）"
            )
        cursor = conn.execute(
            "INSERT INTO annotations"
            " (version, start, end, label, source_annotation_id, created_at)"
            " VALUES (?, ?, ?, ?, NULL, ?)",
            (current, start, end, label, now_iso()),
        )
        annotation_id = cursor.lastrowid
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
    row = conn.execute(
        "SELECT * FROM annotations WHERE id = ?", (annotation_id,)
    ).fetchone()
    return _annotation_dict(row, text)


def _excerpt(text: str, start: int, end: int) -> str:
    left = max(0, start - EXCERPT_CONTEXT)
    right = min(len(text), end + EXCERPT_CONTEXT)
    prefix = "…" if left > 0 else ""
    suffix = "…" if right < len(text) else ""
    return f"{prefix}{text[left:start]}「{text[start:end]}」{text[end:right]}{suffix}"


REASON_MESSAGES = {
    "partially_deleted": (
        "存在增删次数最少、但会删去标注内文字的对齐，标注无法完整保留"
    ),
    "ambiguous_position": (
        "标注边界在多种增删次数最少的对齐中对应到不同位置，"
        "无法确定同一连续区间"
    ),
    "invalid_span": "标注区间超出旧稿范围",
}


def _review_message(result) -> str:
    return REASON_MESSAGES.get(result.reason or "", result.message or result.reason or "")


def replace_manuscript(
    conn: sqlite3.Connection, *, seen_version: int, raw_text: str
) -> dict:
    new_text = validate_text(raw_text)

    # 先在事务外完成文本比对，避免长时间的写锁阻塞比对期间的新增标注。
    # 比对只依赖两稿正文；换稿成功才会改变现行版，因此下面取得写锁后
    # 会再核对版号并重新读取最新标注集合。
    snapshot = _current_version(conn)
    if snapshot != seen_version:
        raise StaleVersion(seen_version, snapshot)
    old_text = _edition_row(conn, snapshot)["text"]
    try:
        alignment = compute_alignment(old_text, new_text)
    except AlignmentTooLarge as exc:
        raise ValidationFailure(str(exc)) from exc

    conn.execute("BEGIN IMMEDIATE")
    try:
        current = _current_version(conn)
        if seen_version != current:
            raise StaleVersion(seen_version, current)
        # 仍为同一版，正文不会变化；再次核对以防万一。
        if _edition_row(conn, current)["text"] != old_text:
            raise Conflict("旧稿正文在比对期间发生变化，请重试")

        # 重新读取标注：比对期间新增的旧稿标注也一并迁移或进入复核。
        annotation_rows = _annotation_rows(conn, current)
        annotations = [
            AnnotationLike(
                id=int(r["id"]),
                start=int(r["start"]),
                end=int(r["end"]),
                label=r["label"],
            )
            for r in annotation_rows
        ]
        results = map_annotations(alignment, annotations)
        migrated = sum(1 for r in results if r.migrated)
        summary = {
            **alignment.stats(),
            "migrated_count": migrated,
            "review_count": len(results) - migrated,
        }

        next_version = current + 1
        created = now_iso()
        conn.execute(
            "INSERT INTO editions (version, text, source_version, alignment_json, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                next_version,
                new_text,
                current,
                json.dumps(summary, ensure_ascii=False),
                created,
            ),
        )

        for result in results:
            ann = result.annotation
            if result.migrated:
                conn.execute(
                    "INSERT INTO annotations"
                    " (version, start, end, label, source_annotation_id, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        next_version,
                        result.new_start,
                        result.new_end,
                        ann.label,
                        ann.id,
                        created,
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO review_items"
                    " (version, source_annotation_id, source_version, start, end,"
                    "  label, old_text, excerpt, reason, message, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        next_version,
                        ann.id,
                        current,
                        ann.start,
                        ann.end,
                        ann.label,
                        old_text,
                        _excerpt(old_text, ann.start, ann.end),
                        result.reason or "unmapped",
                        _review_message(result),
                        created,
                    ),
                )

        conn.execute(
            "UPDATE documents SET current_version = ?, updated_at = ? WHERE id = 1",
            (next_version, created),
        )
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")

    row = _edition_row(conn, next_version)
    return _edition_payload(conn, row, is_current=True)


def get_reviews(conn: sqlite3.Connection, version: int | None) -> dict:
    current = _current_version(conn)
    target = current if version is None else version
    _edition_row(conn, target)
    return {
        "version": target,
        "is_current": target == current,
        "review_items": [_review_dict(r) for r in _review_rows(conn, target)],
    }


def list_annotations(conn: sqlite3.Connection, version: int | None) -> dict:
    current = _current_version(conn)
    target = current if version is None else version
    row = _edition_row(conn, target)
    return {
        "version": target,
        "is_current": target == current,
        "annotations": [
            _annotation_dict(r, row["text"])
            for r in _annotation_rows(conn, target)
        ],
    }
