"""两稿对齐（只允许插字 / 删字）与标注迁移判定。

对齐模型
--------
旧稿 A、新稿 B 之间只允许删除 A 的字符、插入 B 的字符。一次对齐由一组
保持先后顺序、字符相等的配对 (i -> j) 构成，未被配对的 A 字符视为删除，
未配对的 B 字符视为插入。增删次数 = (len(A) - 配对数) + (len(B) - 配对数)，
因此“增删次数最少”等价于配对数取最长公共子序列（LCS）长度。

迁移判定
--------
对标注区间 [s, e)：

* 标注内的每个字符都必须在**每一条**最少增删对齐中保留（不能被删）；
* 标注首字符在每一条最优对齐中对应到同一个新稿位置 j0；
* 标注末字符在每一条最优对齐中对应到同一个新稿位置 j1。

满足时，标注完整落在新稿的连续区间 [j0, j1 + 1) 中。区间内部若新稿插入了
字符，这些字符会一并落在迁移后的区间内（否则区间不连续）；而标注内旧字若
可能被删，则视为不完整，进入复核。

为了同时判断“每一条最优对齐”，这里用前缀 / 后缀 LCS 表计算：对旧稿字符 i，
所有最优对齐中它可能出现的位置集合（以及是否可能被删）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

MAX_ALIGN_CELLS = 2_000_000

#: 在 possible 集合中表示“该字符可能在某条最优对齐中被删除”
DELETED = -1


class AlignmentTooLarge(ValueError):
    """文本超出可比对规模。页面 500 字上限下不会触发，作为防御保留。"""


@dataclass(frozen=True)
class AnnotationLike:
    id: int
    start: int
    end: int
    label: str


@dataclass
class MigrationResult:
    annotation: AnnotationLike
    migrated: bool
    new_start: int | None = None
    new_end: int | None = None
    reason: str | None = None
    message: str | None = None


@dataclass
class Alignment:
    old: str
    new: str
    possible: list[set[int]]
    lcs: int

    @property
    def insertions(self) -> int:
        return len(self.new) - self.lcs

    @property
    def deletions(self) -> int:
        return len(self.old) - self.lcs

    @property
    def edit_count(self) -> int:
        return self.insertions + self.deletions

    def stats(self) -> dict:
        return {
            "old_chars": len(self.old),
            "new_chars": len(self.new),
            "lcs_chars": self.lcs,
            "insertions": self.insertions,
            "deletions": self.deletions,
            "edit_count": self.edit_count,
        }

    def map_annotation(self, ann: AnnotationLike) -> MigrationResult:
        start, end = ann.start, ann.end
        if not (0 <= start < end <= len(self.old)):
            return MigrationResult(
                annotation=ann,
                migrated=False,
                reason="invalid_span",
                message="标注区间超出旧稿范围",
            )

        # 1) 标注内每个字符都必须在所有最优对齐中保留。
        lost = [i for i in range(start, end) if DELETED in self.possible[i]]
        if lost:
            return MigrationResult(
                annotation=ann,
                migrated=False,
                reason="partially_deleted",
                message=(
                    "存在增删次数最少、但会删去标注内文字的对齐，"
                    "标注无法完整保留"
                ),
            )

        # 2) 首尾字符在最优对齐中的新稿位置必须唯一，才能得到同一区间。
        head = self.possible[start]
        tail = self.possible[end - 1]
        if len(head) != 1 or len(tail) != 1:
            return MigrationResult(
                annotation=ann,
                migrated=False,
                reason="ambiguous_position",
                message=(
                    "标注边界在多种增删次数最少的对齐中对应到不同位置，"
                    "无法确定同一连续区间"
                ),
            )

        new_start = next(iter(head))
        new_end = next(iter(tail)) + 1
        return MigrationResult(
            annotation=ann,
            migrated=True,
            new_start=new_start,
            new_end=new_end,
        )


def _prefix_lcs(a: str, b: str) -> list[list[int]]:
    """pre[i][j] = LCS(a[:i], b[:j])"""

    n, m = len(a), len(b)
    pre = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        ai = a[i - 1]
        row, prev = pre[i], pre[i - 1]
        for j in range(1, m + 1):
            if ai == b[j - 1]:
                row[j] = prev[j - 1] + 1
            else:
                left = row[j - 1]
                up = prev[j]
                row[j] = left if left >= up else up
    return pre


def _suffix_lcs(a: str, b: str) -> list[list[int]]:
    """suf[i][j] = LCS(a[i:], b[j:])"""

    n, m = len(a), len(b)
    suf = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        ai = a[i]
        row, nxt = suf[i], suf[i + 1]
        for j in range(m - 1, -1, -1):
            if ai == b[j]:
                row[j] = nxt[j + 1] + 1
            else:
                right = row[j + 1]
                down = nxt[j]
                row[j] = right if right >= down else down
    return suf


def compute_alignment(old: str, new: str) -> Alignment:
    n, m = len(old), len(new)
    if (n + 1) * (m + 1) > MAX_ALIGN_CELLS:
        raise AlignmentTooLarge("文本过长，无法比对")

    pre = _prefix_lcs(old, new)
    suf = _suffix_lcs(old, new)
    best = pre[n][m]

    possible: list[set[int]] = []
    for i in range(n):
        targets: set[int] = set()
        for j in range(m):
            # 存在包含配对 (i -> j) 的最优对齐
            if old[i] == new[j] and pre[i][j] + 1 + suf[i + 1][j + 1] == best:
                targets.add(j)
        # 存在删除 old[i] 的最优对齐
        for j in range(m + 1):
            if pre[i][j] + suf[i + 1][j] == best:
                targets.add(DELETED)
                break
        possible.append(targets)

    return Alignment(old=old, new=new, possible=possible, lcs=best)


def map_annotations(
    alignment: Alignment, annotations: Iterable[AnnotationLike]
) -> list[MigrationResult]:
    return [alignment.map_annotation(ann) for ann in annotations]


def lcs_length(a: str, b: str) -> int:
    """滚动数组求 LCS 长度（列表接口的快速预算）。"""

    n, m = len(a), len(b)
    if (n + 1) * (m + 1) > MAX_ALIGN_CELLS:
        raise AlignmentTooLarge("文本过长，无法比对")
    if m > n:
        a, b = b, a
        n, m = m, n
    prev = [0] * (m + 1)
    for i in range(n):
        cur = [0] * (m + 1)
        ai = a[i]
        for j in range(m):
            if ai == b[j]:
                cur[j + 1] = prev[j] + 1
            else:
                left = cur[j]
                up = prev[j + 1]
                cur[j + 1] = left if left >= up else up
        prev = cur
    return prev[m]
