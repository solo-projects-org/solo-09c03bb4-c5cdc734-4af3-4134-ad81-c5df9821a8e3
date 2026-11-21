"""对齐算法单元测试，含穷举最优对齐的交叉验证。"""

from functools import lru_cache
from itertools import product

import pytest

from app.align import AlignmentTooLarge, AnnotationLike, compute_alignment


def brute_possible(a: str, b: str) -> list[set[int]]:
    """穷举所有最优对齐，返回每个旧字符可能的新位置（-1 表示可被删除）。"""

    n, m = len(a), len(b)

    @lru_cache(None)
    def lcs(i, j):
        if i == n or j == m:
            return 0
        if a[i] == b[j]:
            return 1 + lcs(i + 1, j + 1)
        return max(lcs(i + 1, j), lcs(i, j + 1))

    possible = [set() for _ in range(n)]

    def walk(i, j, path):
        if i == n and j == m:
            mapped = dict(path)
            for idx in range(n):
                possible[idx].add(mapped.get(idx, -1))
            return
        if i == n:
            walk(i, j + 1, path)
            return
        if j == m:
            walk(i + 1, j, path)
            return
        if a[i] == b[j] and lcs(i, j) == lcs(i + 1, j + 1) + 1:
            walk(i + 1, j + 1, path + [(i, j)])
        if lcs(i, j) == lcs(i + 1, j):
            walk(i + 1, j, path)
        if lcs(i, j) == lcs(i, j + 1):
            walk(i, j + 1, path)

    walk(0, 0, [])
    return possible


@pytest.mark.parametrize(
    "a,b",
    [
        (a, b)
        for a in ("".join(p) for n in range(1, 5) for p in product("ab", repeat=n))
        for b in ("".join(p) for n in range(1, 5) for p in product("ab", repeat=n))
    ],
)
def test_possible_sets_match_bruteforce(a, b):
    assert compute_alignment(a, b).possible == brute_possible(a, b)


@pytest.mark.parametrize(
    "old,new,span,expected",
    [
        # 无改动
        ("甲乙丙丁", "甲乙丙丁", (1, 3), (1, 3)),
        # 标注前插入
        ("甲乙丙丁", "X甲乙丙丁", (1, 3), (2, 4)),
        # 标注后删除
        ("甲乙丙丁", "甲乙丙", (0, 2), (0, 2)),
        # 标注内部插入：迁移区间会包含插入的字，仍为连续区间
        ("甲乙丙丁", "甲X乙丙丁", (0, 4), (0, 5)),
        ("甲乙丙丁", "甲X乙丙丁", (1, 3), (2, 4)),
        # 标注内部删除：不能完整保留
        ("甲乙丙丁", "甲丙丁", (1, 2), None),
        # 标注前部删除
        ("甲乙丙丁", "乙丙丁", (0, 2), None),
    ],
)
def test_map_annotation_cases(old, new, span, expected):
    alignment = compute_alignment(old, new)
    result = alignment.map_annotation(AnnotationLike(1, span[0], span[1], "x"))
    if expected is None:
        assert result.migrated is False
    else:
        assert result.migrated is True
        assert (result.new_start, result.new_end) == expected


def test_repeated_word_boundaries_are_unique_by_context():
    old = "甲公司成立于甲公司旧址"
    new = "甲公司成立于甲公司新址"
    alignment = compute_alignment(old, new)
    first = alignment.map_annotation(AnnotationLike(1, 0, 3, "A"))
    second = alignment.map_annotation(AnnotationLike(1, 6, 9, "B"))
    assert (first.new_start, first.new_end) == (0, 3)
    assert (second.new_start, second.new_end) == (6, 9)


def test_wholly_repeated_text_is_ambiguous():
    alignment = compute_alignment("甲公司甲公司", "甲公司")
    assert alignment.map_annotation(AnnotationLike(1, 0, 3, "A")).migrated is False
    assert alignment.map_annotation(AnnotationLike(1, 3, 6, "B")).migrated is False


def test_insertion_only_shift():
    alignment = compute_alignment("abcdef", "abcXdef")
    assert alignment.insertions == 1
    assert alignment.deletions == 0
    result = alignment.map_annotation(AnnotationLike(1, 3, 6, "def"))
    assert (result.new_start, result.new_end) == (4, 7)


def test_overlarge_input_guard():
    a = "甲" * 1500
    b = "乙" * 1500
    with pytest.raises(AlignmentTooLarge):
        compute_alignment(a, b)
