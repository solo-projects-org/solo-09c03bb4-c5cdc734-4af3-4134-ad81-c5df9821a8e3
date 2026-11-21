"""业务服务：录入、补标、换稿、外发补标与查询。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from .align import (
    AlignmentTooLarge,
    AnnotationLike,
    compute_alignment,
    map_annotations,
)
from .dispatch import Span, map_interval, mask_page, page_from_mask
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


class ClaimRejected(Conflict):
    """首次回收失败：整包退回，不写入任何标注或回执。"""

    code = "claim_rejected"

    def __init__(self, pack_no: str, errors: list[dict]):
        super().__init__(
            "回收包存在无效区间或基于已换版的旧稿，整包退回",
            pack_no=pack_no,
            errors=errors,
        )


class ClaimPayloadMismatch(Conflict):
    code = "claim_mismatch"


class UnknownPack(NotFound):
    code = "unknown_pack"


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


# ---------------------------------------------------------------------------
# 外发补标包
# ---------------------------------------------------------------------------


def _pack_row(conn: sqlite3.Connection, pack_no: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM dispatch_packs WHERE pack_no = ?", (pack_no,)
    ).fetchone()
    if row is None:
        raise UnknownPack(f"外发补标包 {pack_no} 不存在")
    return row


def _claim_row(conn: sqlite3.Connection, pack_no: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM dispatch_claims WHERE pack_no = ?", (pack_no,)
    ).fetchone()


def _pack_spans(row: sqlite3.Row) -> list[dict]:
    return json.loads(row["masked_spans_json"])


def _dispatch_pack_payload(row: sqlite3.Row) -> dict:
    """发包响应只暴露包号与遮盖后的正文，不回传原文或遮盖位置。"""

    return {
        "pack_no": row["pack_no"],
        "masked_text": row["masked_text"],
    }


def _receipt_payload(conn: sqlite3.Connection, claim: sqlite3.Row) -> dict:
    receipt = json.loads(claim["receipt_json"])
    receipt["annotations"] = [
        {
            "position": int(r["position"]),
            "annotation_id": int(r["annotation_id"]),
            "masked_start": int(r["masked_start"]),
            "masked_end": int(r["masked_end"]),
            "start": int(r["start"]),
            "end": int(r["end"]),
            "label": r["label"],
        }
        for r in conn.execute(
            "SELECT * FROM dispatch_claim_annotations WHERE pack_no = ?"
            " ORDER BY position",
            (claim["pack_no"],),
        ).fetchall()
    ]
    return receipt


def create_dispatch_pack(
    conn: sqlite3.Connection,
    *,
    seen_version: int,
    mask_annotation_ids: list[int],
) -> dict:
    """冻结现行版正文，按指定标注编号遮盖姓名后打包。

    多个遮盖区间相交（共享至少一个字符）时合并为一段，每段只留一个「□」；
    首尾相接但不相交的区间保留为两个「□」。
    """

    ids = [int(value) for value in mask_annotation_ids]
    if not ids:
        raise ValidationFailure("至少需要指定一条要遮盖的标注")
    if len(set(ids)) != len(ids):
        raise ValidationFailure("要遮盖的标注编号不能重复")

    conn.execute("BEGIN IMMEDIATE")
    try:
        current = _current_version(conn)
        if seen_version != current:
            raise StaleVersion(seen_version, current)
        text = _edition_row(conn, current)["text"]

        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            "SELECT * FROM annotations WHERE version = ?"
            f" AND id IN ({placeholders}) ORDER BY start, id",
            (current, *ids),
        ).fetchall()
        found = {int(r["id"]) for r in rows}
        missing = [value for value in ids if value not in found]
        if missing:
            raise ValidationFailure(
                "以下标注编号不在现行版中，无法遮盖",
                missing_annotation_ids=missing,
            )

        page = mask_page(text, [(int(r["start"]), int(r["end"])) for r in rows])
        masked_spans = [
            {"start": span.start, "end": span.end} for span in page.spans
        ]
        source_ids = [int(r["id"]) for r in rows]

        created = now_iso()
        pack_no = f"PK-{created[:10].replace('-', '')}-{uuid.uuid4().hex[:12]}"
        conn.execute(
            "INSERT INTO dispatch_packs"
            " (pack_no, version, masked_text, masked_spans_json,"
            "  source_annotation_ids_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                pack_no,
                current,
                page.masked_text,
                json.dumps(masked_spans, ensure_ascii=False),
                json.dumps(source_ids, ensure_ascii=False),
                created,
            ),
        )
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")

    return _dispatch_pack_payload(_pack_row(conn, pack_no))


def _validate_claim_payload(intervals: list[tuple[int, int, str]]) -> list[dict]:
    if not intervals:
        return [
            {"index": None, "reason": "empty", "message": "回收包至少包含一条标注"}
        ]
    errors = []
    seen: set[tuple[int, int, str]] = set()
    for index, (start, end, raw_label) in enumerate(intervals):
        label = raw_label.strip()
        if start < 0 or end <= start:
            errors.append(
                {
                    "index": index,
                    "start": start,
                    "end": end,
                    "label": raw_label,
                    "reason": "invalid_range",
                    "message": "区间须满足 start >= 0 且 end > start",
                }
            )
        if not label:
            errors.append(
                {
                    "index": index,
                    "start": start,
                    "end": end,
                    "label": raw_label,
                    "reason": "empty_label",
                    "message": "标注内容不能为空",
                }
            )
        elif (start, end, label) in seen:
            errors.append(
                {
                    "index": index,
                    "start": start,
                    "end": end,
                    "label": raw_label,
                    "reason": "duplicate_interval",
                    "message": "同一回收包内不能重复提交完全相同的区间与标签",
                }
            )
        else:
            seen.add((start, end, label))
    return errors


def _claim_key(
    pack_no: str, intervals: list[tuple[int, int, str]]
) -> str:
    """回执去重键：外发稿区间与标签的有序排列（与提交顺序无关）。"""

    canonical = [
        {"start": start, "end": end, "label": label.strip()}
        for start, end, label in sorted(
            intervals, key=lambda item: (item[0], item[1], item[2].strip())
        )
    ]
    return json.dumps(
        {"pack_no": pack_no, "intervals": canonical},
        ensure_ascii=False,
        sort_keys=True,
    )


def claim_dispatch_pack(
    conn: sqlite3.Connection,
    *,
    pack_no: str,
    intervals: list[tuple[int, int, str]],
) -> dict:
    """回收外发稿区间，换算成原稿坐标后与回执一起入库。

    * 首次回收：任一区间无效，或包所基于的版次已不是现行版，整包退回；
    * 已收包原样重送：直接返回首次回执（换版、进程重启后仍一致）；
    * 已收包但内容不同：``409 claim_mismatch``，不覆盖原回执。
    """

    normalized = [
        (start, end, label.strip()) for start, end, label in intervals
    ]
    claim_key = _claim_key(pack_no, normalized)

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = _claim_row(conn, pack_no)
        if existing is not None:
            if existing["payload_json"] == claim_key:
                conn.execute("COMMIT")
                return _receipt_payload(conn, existing)
            raise ClaimPayloadMismatch(
                "该包已回收过，且本次提交的标注与首次回执不同",
                pack_no=pack_no,
            )

        # 先确认包存在（404），再校验区间（422），最后才判断是否换版。
        pack = _pack_row(conn, pack_no)
        payload_errors = _validate_claim_payload(intervals)
        if payload_errors:
            raise ValidationFailure("回收区间不合法", errors=payload_errors)
        current = _current_version(conn)
        if int(pack["version"]) != current:
            raise ClaimRejected(
                pack_no,
                [
                    {
                        "reason": "stale_edition",
                        "message": (
                            f"包基于第 {pack['version']} 版，现行版已是第 {current} 版，"
                            "请重新发包"
                        ),
                    }
                ],
            )

        spans = [
            Span(int(item["start"]), int(item["end"]))
            for item in _pack_spans(pack)
        ]
        page = page_from_mask(pack["masked_text"], spans)

        errors: list[dict] = []
        mapped: list[dict] = []
        for index, (start, end, label) in enumerate(normalized):
            mapping = map_interval(page, start, end)
            if not mapping.valid:
                errors.append(
                    {
                        "index": index,
                        "start": start,
                        "end": end,
                        "label": label,
                        "reason": mapping.reason,
                        "message": mapping.message,
                    }
                )
            else:
                mapped.append(
                    {
                        "masked_start": start,
                        "masked_end": end,
                        "start": mapping.original_start,
                        "end": mapping.original_end,
                        "label": label,
                    }
                )
        if errors:
            raise ClaimRejected(pack_no, errors)

        created = now_iso()
        for item in mapped:
            cursor = conn.execute(
                "INSERT INTO annotations"
                " (version, start, end, label, source_annotation_id, created_at)"
                " VALUES (?, ?, ?, ?, NULL, ?)",
                (current, item["start"], item["end"], item["label"], created),
            )
            item["annotation_id"] = int(cursor.lastrowid)

        receipt = {
            "pack_no": pack_no,
            "pack_version": int(pack["version"]),
            "claimed_version": current,
            "status": "accepted",
            "claimed_at": created,
            "annotation_count": len(mapped),
        }
        conn.execute(
            "INSERT INTO dispatch_claims"
            " (pack_no, version, payload_json, receipt_json, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                pack_no,
                current,
                claim_key,
                json.dumps(receipt, ensure_ascii=False, sort_keys=True),
                created,
            ),
        )
        for position, item in enumerate(mapped):
            conn.execute(
                "INSERT INTO dispatch_claim_annotations"
                " (pack_no, position, annotation_id, version, masked_start,"
                "  masked_end, start, end, label)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    pack_no,
                    position,
                    item["annotation_id"],
                    current,
                    item["masked_start"],
                    item["masked_end"],
                    item["start"],
                    item["end"],
                    item["label"],
                ),
            )
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")

    return _receipt_payload(conn, _claim_row(conn, pack_no))


def get_dispatch_pack(conn: sqlite3.Connection, pack_no: str) -> dict:
    return _dispatch_pack_payload(_pack_row(conn, pack_no))


def get_dispatch_receipt(conn: sqlite3.Connection, pack_no: str) -> dict:
    _pack_row(conn, pack_no)
    claim = _claim_row(conn, pack_no)
    if claim is None:
        raise NotFound(f"外发补标包 {pack_no} 尚未回收")
    return _receipt_payload(conn, claim)
