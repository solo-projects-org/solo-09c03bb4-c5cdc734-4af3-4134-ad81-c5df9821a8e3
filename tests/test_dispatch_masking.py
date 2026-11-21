"""遮盖与区间换算的纯函数测试。"""

import pytest

from app.dispatch import (
    MASK_CHAR,
    Span,
    map_interval,
    mask_page,
    merge_spans,
    page_from_mask,
)


def test_merge_spans_merges_only_intersections():
    assert merge_spans([(1, 3), (2, 5), (5, 6), (8, 9)]) == [
        Span(1, 5),
        Span(5, 6),
        Span(8, 9),
    ]


def test_merge_spans_is_order_independent_and_drops_empty():
    assert merge_spans([(8, 9), (2, 5), (1, 3), (4, 4)]) == [
        Span(1, 5),
        Span(8, 9),
    ]


def test_mask_page_replaces_each_merged_span_with_one_box():
    page = mask_page("abcdefgh", [(1, 3), (2, 4), (4, 5)])
    assert page.masked_text == "a" + MASK_CHAR + MASK_CHAR + "fgh"
    assert page.spans == (Span(1, 4), Span(4, 5))


def test_mask_page_segments_map_both_directions():
    page = mask_page("0123456789", [(2, 4), (7, 8)])
    assert page.masked_text == "01" + MASK_CHAR + "456" + MASK_CHAR + "89"
    assert [
        (s.masked_start, s.masked_end, s.original_start, s.original_end)
        for s in page.segments
    ] == [(0, 2, 0, 2), (3, 6, 4, 7), (7, 9, 8, 10)]


def test_mask_page_rejects_out_of_range_spans():
    with pytest.raises(ValueError):
        mask_page("abc", [(2, 4)])


def test_map_interval_inside_single_segment():
    page = mask_page("0123456789", [(2, 4), (7, 8)])
    assert map_interval(page, 3, 6).original_start == 4
    assert map_interval(page, 3, 6).original_end == 7
    assert map_interval(page, 7, 9).original_start == 8
    assert map_interval(page, 7, 9).original_end == 10


def test_map_interval_rejects_masked_and_crossing():
    page = mask_page("0123456789", [(2, 4), (7, 8)])
    assert map_interval(page, 2, 3).reason == "inside_masked_span"
    assert map_interval(page, 0, 3).reason == "crosses_masked_span"
    assert map_interval(page, 6, 8).reason == "crosses_masked_span"
    assert map_interval(page, 0, 99).reason == "out_of_range"
    assert map_interval(page, 3, 3).reason == "out_of_range"


def test_page_from_mask_reconstructs_same_segments_as_mask_page():
    original = "甲乙丙丁戊己庚"
    spans = [(1, 3), (3, 4), (6, 7)]
    page = mask_page(original, spans)
    rebuilt = page_from_mask(page.masked_text, page.spans)
    assert rebuilt.segments == page.segments
    for masked_start, masked_end, _, _ in (
        (s.masked_start, s.masked_end, s.original_start, s.original_end)
        for s in page.segments
    ):
        left = map_interval(rebuilt, masked_start, masked_end)
        right = map_interval(page, masked_start, masked_end)
        assert left == right


def test_masking_entire_text_leaves_only_boxes():
    page = mask_page("甲乙丙", [(0, 3)])
    assert page.masked_text == MASK_CHAR
    assert page.segments == ()
    assert map_interval(page, 0, 1).reason == "inside_masked_span"


def test_random_subintervals_map_to_same_substring():
    import random

    rng = random.Random(20261121)
    alphabet = "甲乙丙丁戊己庚辛壬癸"
    for _ in range(300):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 30)))
        spans = []
        for _ in range(rng.randint(1, 4)):
            start = rng.randrange(len(text))
            end = rng.randint(start + 1, len(text))
            spans.append((start, end))
        page = mask_page(text, spans)
        for segment in page.segments:
            for masked_start in range(segment.masked_start, segment.masked_end):
                for masked_end in range(masked_start + 1, segment.masked_end + 1):
                    mapped = map_interval(page, masked_start, masked_end)
                    assert mapped.valid
                    assert (
                        text[mapped.original_start : mapped.original_end]
                        == page.masked_text[masked_start:masked_end]
                    )
