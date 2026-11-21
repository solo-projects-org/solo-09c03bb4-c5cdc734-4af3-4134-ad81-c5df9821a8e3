"""并发行为：比对进行中仍可补标，换稿时一并处理。"""

import threading

import pytest

from app import service
from app.db import connect, init_db


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "concurrent.db"))
    init_db()
    return str(tmp_path / "concurrent.db")


def _conn():
    return connect()


def test_annotation_during_comparison_is_included(db, monkeypatch):
    conn = _conn()
    service.create_manuscript(conn, "甲乙丙丁")
    conn.close()

    started = threading.Event()
    release = threading.Event()
    original = service.compute_alignment

    def blocking_alignment(old, new):
        result = original(old, new)
        started.set()
        assert release.wait(timeout=10), "换稿线程未被释放"
        return result

    monkeypatch.setattr(service, "compute_alignment", blocking_alignment)

    outcome = {}

    def do_replace():
        c = _conn()
        try:
            outcome["body"] = service.replace_manuscript(
                c, seen_version=1, raw_text="甲X乙丙丁"
            )
        except Exception as exc:  # pragma: no cover - 失败时便于定位
            outcome["error"] = exc
        finally:
            c.close()

    thread = threading.Thread(target=do_replace)
    thread.start()
    assert started.wait(timeout=10), "比对未开始"

    # 比对尚未结束，此时补标应被正常接收
    add_conn = _conn()
    ann = service.add_annotation(
        add_conn, seen_version=1, start=2, end=4, label="丙丁"
    )
    add_conn.close()
    assert ann["version"] == 1

    release.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert "error" not in outcome, outcome.get("error")

    body = outcome["body"]
    assert body["version"] == 2
    labels = {a["label"]: (a["start"], a["end"]) for a in body["annotations"]}
    assert labels == {"丙丁": (3, 5)}
    assert body["review_items"] == []


def test_annotation_after_replace_commit_is_rejected_as_stale(db):
    conn = _conn()
    service.create_manuscript(conn, "甲乙丙丁")
    service.replace_manuscript(conn, seen_version=1, raw_text="甲乙丙丁戊")
    with pytest.raises(service.StaleVersion) as info:
        service.add_annotation(conn, seen_version=1, start=0, end=1, label="甲")
    assert info.value.extra["current_version"] == 2
    conn.close()


def test_claim_during_replace_comparison_is_migrated(db, monkeypatch):
    """回收与换稿交错：比对进行中回收的标注，必须在换稿时一起迁移。"""

    conn = _conn()
    service.create_manuscript(conn, "前文甲乙丙丁后文")
    service.add_annotation(conn, seen_version=1, start=2, end=4, label="甲乙")
    ids = [
        row["id"]
        for row in conn.execute("SELECT id FROM annotations WHERE version = 1")
    ]
    pack_no = service.create_dispatch_pack(
        conn, seen_version=1, mask_annotation_ids=ids
    )["pack_no"]
    conn.close()

    started = threading.Event()
    release = threading.Event()
    original = service.compute_alignment

    def blocking_alignment(old, new):
        result = original(old, new)
        started.set()
        assert release.wait(timeout=10), "换稿线程未被释放"
        return result

    monkeypatch.setattr(service, "compute_alignment", blocking_alignment)

    outcome = {}

    def do_replace():
        c = _conn()
        try:
            outcome["body"] = service.replace_manuscript(
                c, seen_version=1, raw_text="前文甲X乙丙丁后文"
            )
        except Exception as exc:  # pragma: no cover - 失败时便于定位
            outcome["error"] = exc
        finally:
            c.close()

    thread = threading.Thread(target=do_replace)
    thread.start()
    assert started.wait(timeout=10), "比对未开始"

    # 比对尚未结束，此时回收外发包；标注写入第 1 版。
    claim_conn = _conn()
    receipt = service.claim_dispatch_pack(
        claim_conn, pack_no=pack_no, intervals=[(3, 5, "丙丁")]
    )
    claim_conn.close()
    assert receipt["annotation_count"] == 1

    release.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert "error" not in outcome, outcome.get("error")

    body = outcome["body"]
    assert body["version"] == 2
    labels = {a["label"]: (a["start"], a["end"]) for a in body["annotations"]}
    assert labels == {"甲乙": (2, 5), "丙丁": (5, 7)}
    assert body["review_items"] == []
