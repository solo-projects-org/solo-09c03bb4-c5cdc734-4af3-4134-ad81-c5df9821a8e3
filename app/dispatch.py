"""外发补标包：姓名遮盖、坐标换算与回收区间校验。

遮盖模型
--------
编校员指定若干标注区间，服务端把它们投影到现行版正文上，先合并**相交**
（共享至少一个字符）的区间，再把每个合并后的区间整体换成一个 ``□``。
首尾相接但不相交的两个区间会保留两个 ``□``，以尊重“每段换成一个”的约定。

外发稿坐标与原文坐标
--------------------
遮盖后的正文由若干“未遮盖原文片段”和 ``□`` 交替组成。回收区间必须完整
落在某一个未遮盖片段内；服务端按该片段的偏移量把半开区间 ``[start, end)``
换算回原稿坐标，不依赖标注员回传原文。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

MASK_CHAR = "□"


@dataclass(frozen=True)
class Span:
    start: int
    end: int


@dataclass(frozen=True)
class MaskedSegment:
    """一段未遮盖的原文，及其在外发稿中的位置。"""

    original_start: int
    original_end: int
    masked_start: int
    masked_end: int


@dataclass(frozen=True)
class MaskedPage:
    original_text: str
    masked_text: str
    spans: tuple[Span, ...]
    segments: tuple[MaskedSegment, ...]


def merge_spans(spans: Iterable[tuple[int, int]]) -> list[Span]:
    """合并相交区间；首尾相接（end == 下一段 start）不合并。"""

    ordered = sorted(
        (Span(int(start), int(end)) for start, end in spans),
        key=lambda span: (span.start, span.end),
    )
    merged: list[Span] = []
    for span in ordered:
        if span.end <= span.start:
            continue
        if merged and span.start < merged[-1].end:
            previous = merged[-1]
            merged[-1] = Span(previous.start, max(previous.end, span.end))
        else:
            merged.append(span)
    return merged


def mask_page(text: str, spans: Iterable[tuple[int, int]]) -> MaskedPage:
    """按区间遮盖正文，并记录每段未遮盖原文的坐标映射。

    区间越界属于内部数据错误，由调用方先行校验；这里仍会拒绝越界区间。
    """

    merged = merge_spans(spans)
    for span in merged:
        if span.start < 0 or span.end > len(text):
            raise ValueError(f"遮盖区间 [{span.start}, {span.end}) 超出正文范围")

    parts: list[str] = []
    segments: list[MaskedSegment] = []
    original_pos = 0
    masked_pos = 0
    for span in merged:
        if original_pos < span.start:
            chunk = text[original_pos : span.start]
            segments.append(
                MaskedSegment(
                    original_start=original_pos,
                    original_end=span.start,
                    masked_start=masked_pos,
                    masked_end=masked_pos + len(chunk),
                )
            )
            parts.append(chunk)
            masked_pos += len(chunk)
        parts.append(MASK_CHAR)
        masked_pos += len(MASK_CHAR)
        original_pos = span.end
    if original_pos < len(text):
        chunk = text[original_pos:]
        segments.append(
            MaskedSegment(
                original_start=original_pos,
                original_end=len(text),
                masked_start=masked_pos,
                masked_end=masked_pos + len(chunk),
            )
        )
        parts.append(chunk)

    return MaskedPage(
        original_text=text,
        masked_text="".join(parts),
        spans=tuple(merged),
        segments=tuple(segments),
    )


def page_from_mask(masked_text: str, spans: Iterable[Span]) -> MaskedPage:
    """由发包时冻结的外发稿与遮盖区间重建坐标映射（不依赖现行版正文）。"""

    ordered = tuple(sorted(spans, key=lambda item: (item.start, item.end)))
    segments: list[MaskedSegment] = []
    original_pos = 0
    masked_pos = 0
    for span in ordered:
        if original_pos < span.start:
            length = span.start - original_pos
            segments.append(
                MaskedSegment(
                    original_start=original_pos,
                    original_end=span.start,
                    masked_start=masked_pos,
                    masked_end=masked_pos + length,
                )
            )
            masked_pos += length
        masked_pos += 1
        original_pos = span.end
    remaining = len(masked_text) - masked_pos
    if remaining < 0:
        raise ValueError("外发稿比遮盖区间标称的长度还短")
    if remaining > 0:
        segments.append(
            MaskedSegment(
                original_start=original_pos,
                original_end=original_pos + remaining,
                masked_start=masked_pos,
                masked_end=masked_pos + remaining,
            )
        )
    return MaskedPage(
        original_text="",
        masked_text=masked_text,
        spans=ordered,
        segments=tuple(segments),
    )


@dataclass(frozen=True)
class IntervalMapping:
    """单条回收区间的校验与换算结果。"""

    valid: bool
    original_start: int | None = None
    original_end: int | None = None
    reason: str | None = None
    message: str | None = None


def map_interval(page: MaskedPage, start: int, end: int) -> IntervalMapping:
    """把外发稿区间换算回原稿坐标；区间必须完整落在同一未遮盖片段内。"""

    if not (0 <= start < end <= len(page.masked_text)):
        return IntervalMapping(
            valid=False,
            reason="out_of_range",
            message=(
                f"区间 [{start}, {end}) 超出外发稿范围"
                f"（共 {len(page.masked_text)} 字符）"
            ),
        )

    partially_overlapped = False
    for segment in page.segments:
        if end <= segment.masked_start or start >= segment.masked_end:
            continue
        if segment.masked_start <= start and end <= segment.masked_end:
            offset = segment.original_start - segment.masked_start
            return IntervalMapping(
                valid=True,
                original_start=start + offset,
                original_end=end + offset,
            )
        partially_overlapped = True

    if partially_overlapped:
        return IntervalMapping(
            valid=False,
            reason="crosses_masked_span",
            message="区间跨越了遮盖处，须拆成落在同一段未遮盖原文内的区间",
        )
    return IntervalMapping(
        valid=False,
        reason="inside_masked_span",
        message="区间落在遮盖处，遮盖的文字不允许补标",
    )
