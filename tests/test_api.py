"""录入、补标、换稿、版号校验与查询的端到端测试。"""

import pytest
from conftest import annotate, post_manuscript, replace


OLD = "光绪《XX县志》卷三：城东有永安桥，跨白水河。\n桥始建于明万历年间，县人李守仁募修。"


def test_create_and_query_current(client):
    resp = post_manuscript(client, OLD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == 1
    assert body["is_current"] is True
    # 坐标基于去掉换行后的正文
    assert "\n" not in body["text"]
    assert body["char_count"] == len(OLD.replace("\n", ""))
    assert client.get("/api/current").json()["version"] == 1

    second = post_manuscript(client, OLD)
    assert second.status_code == 409


def test_newlines_removed_before_counting_and_offsets(client):
    text = "甲乙丙\n丁戊己\r\n庚辛"
    body = post_manuscript(client, text).json()
    assert body["text"] == "甲乙丙丁戊己庚辛"
    # 带换行的原始串下标 6 落在“庚”上；去换行后应是 6
    resp = annotate(client, 1, 6, 8, "庚辛")
    assert resp.status_code == 200
    ann = resp.json()
    assert ann["start"] == 6 and ann["end"] == 8


def test_text_limit_after_stripping_newlines(client):
    ok = "甲" * 500
    assert len(ok.replace("\n", "")) == 500
    assert post_manuscript(client, ok).status_code == 200

    too_long = "甲" * 300 + "\n" * 300 + "乙" * 201
    resp = replace(client, 1, too_long)
    assert resp.status_code == 422


def test_annotation_rejects_out_of_range(client):
    post_manuscript(client, "甲乙丙")
    assert annotate(client, 1, 0, 4, "越界").status_code == 422
    assert annotate(client, 1, 2, 2, "空区间").status_code == 422


def test_stale_annotation_version_returns_current(client):
    post_manuscript(client, "甲乙丙丁")
    replace(client, 1, "甲乙丙丁戊")
    resp = annotate(client, 1, 0, 2, "甲乙")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "stale_version"
    assert detail["seen_version"] == 1
    assert detail["current_version"] == 2


def test_replace_migrates_annotation_and_advances_version(client):
    post_manuscript(client, "甲乙丙丁")
    annotate(client, 1, 1, 3, "乙丙")
    resp = replace(client, 1, "甲X乙丙丁")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == 2
    assert body["is_current"] is True
    assert body["source_version"] == 1
    (ann,) = body["annotations"]
    assert (ann["start"], ann["end"], ann["label"]) == (2, 4, "乙丙")
    assert ann["source_annotation_id"] == 1
    assert body["review_items"] == []
    assert body["alignment"]["edit_count"] == 1


def test_replace_with_stale_version_is_rejected_and_old_edition_remains(client):
    post_manuscript(client, "甲乙丙丁")
    annotate(client, 1, 0, 2, "甲乙")
    replace(client, 1, "甲乙丙丁戊")
    resp = replace(client, 1, "甲乙丙")
    assert resp.status_code == 409
    assert resp.json()["detail"]["current_version"] == 2
    # 旧稿及其标注仍可查
    current = client.get("/api/current").json()
    assert current["version"] == 2
    assert current["text"] == "甲乙丙丁戊"
    old = client.get("/api/versions/1").json()
    assert old["text"] == "甲乙丙丁"
    assert [(a["start"], a["end"], a["label"]) for a in old["annotations"]] == [
        (0, 2, "甲乙")
    ]
    # 失败的换版没有产生第 3 版
    versions = [v["version"] for v in client.get("/api/versions").json()["versions"]]
    assert versions == [1, 2]


def test_failed_validation_leaves_everything_untouched(client):
    post_manuscript(client, "甲乙丙丁")
    annotate(client, 1, 0, 2, "甲乙")
    resp = replace(client, 1, "甲" * 501)
    assert resp.status_code == 422
    current = client.get("/api/current").json()
    assert current["version"] == 1
    assert current["text"] == "甲乙丙丁"
    assert len(current["annotations"]) == 1


def test_mid_transaction_failure_rolls_back_and_old_edition_stays_queryable(
    client, monkeypatch
):
    """模拟写库中途出错：新稿、迁入标注、复核清单必须全部回滚。"""

    from app import service

    post_manuscript(client, "甲乙丙丁")
    annotate(client, 1, 0, 2, "甲乙")
    annotate(client, 1, 2, 3, "丙")  # 会被删除，触发复核写入

    calls = {"n": 0}
    original = service._review_message

    def fail_once(result):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("模拟写复核清单时故障")
        return original(result)

    monkeypatch.setattr(service, "_review_message", fail_once)

    with pytest.raises(RuntimeError):
        replace(client, 1, "甲乙丁")

    current = client.get("/api/current").json()
    assert current["version"] == 1
    assert current["text"] == "甲乙丙丁"
    assert len(current["annotations"]) == 2
    assert current["review_items"] == []
    versions = [v["version"] for v in client.get("/api/versions").json()["versions"]]
    assert versions == [1]

    # 故障排除后可正常换版
    monkeypatch.setattr(service, "_review_message", original)
    body = replace(client, 1, "甲乙丁").json()
    assert body["version"] == 2
    assert [a["label"] for a in body["annotations"]] == ["甲乙"]
    assert [r["label"] for r in body["review_items"]] == ["丙"]


def test_annotation_added_during_comparison_is_processed(client, monkeypatch):
    """比对期间照常接收旧稿标注，换稿时一并处理。

    直接对服务层打桩：在 compute_alignment 返回后、写事务开始前插入一条新标注，
    模拟“比对进行中补标”。
    """

    from app import service

    post_manuscript(client, "甲乙丙丁")
    annotate(client, 1, 0, 1, "甲")

    original = service.compute_alignment
    calls = {"done": False}

    def slow_alignment(old, new):
        result = original(old, new)
        if not calls["done"]:
            calls["done"] = True
            # 比对期间另一请求写入的新标注（换行应被规范化）
            resp = client.post(
                "/api/annotations",
                json={"version": 1, "start": 2, "end": 4, "label": "丙丁"},
            )
            assert resp.status_code == 200
        return result

    monkeypatch.setattr(service, "compute_alignment", slow_alignment)

    body = replace(client, 1, "甲X乙丙丁").json()
    assert body["version"] == 2
    labels = {a["label"]: (a["start"], a["end"]) for a in body["annotations"]}
    assert labels == {"甲": (0, 1), "丙丁": (3, 5)}


def test_review_items_carry_original_annotation_and_fragment(client):
    post_manuscript(client, "前文甲乙丙丁后文")
    annotate(client, 1, 3, 5, "乙丙")  # 将被删去的字符
    body = replace(client, 1, "前文甲丁后文").json()
    assert body["annotations"] == []
    (item,) = body["review_items"]
    assert item["label"] == "乙丙"
    assert (item["start"], item["end"]) == (3, 5)
    assert item["source_version"] == 1
    assert "「乙丙」" in item["old_fragment"]
    assert item["old_text"] == "前文甲乙丙丁后文"
    # 默认查询现行版清单
    reviews = client.get("/api/reviews").json()
    assert reviews["version"] == 2
    assert [r["id"] for r in reviews["review_items"]] == [item["id"]]
    # 旧版清单为空
    assert client.get("/api/reviews?version=1").json()["review_items"] == []


def test_repeated_words_disambiguated_by_context(client):
    old = "甲公司成立于甲公司旧址，后迁至城西。"
    new = "甲公司成立于甲公司新址，后迁至城西。"
    post_manuscript(client, old)
    assert annotate(client, 1, 0, 3, "甲公司A").status_code == 200
    assert annotate(client, 1, 6, 9, "甲公司B").status_code == 200
    body = replace(client, 1, new).json()
    assert len(body["review_items"]) == 0
    positions = {a["label"]: (a["start"], a["end"]) for a in body["annotations"]}
    assert positions == {"甲公司A": (0, 3), "甲公司B": (6, 9)}
    assert body["text"][0:3] == "甲公司"
    assert body["text"][6:9] == "甲公司"


def test_repeated_words_fully_ambiguous_go_to_review(client):
    post_manuscript(client, "甲公司甲公司")
    annotate(client, 1, 0, 3, "甲公司A")
    annotate(client, 1, 3, 6, "甲公司B")
    body = replace(client, 1, "甲公司").json()
    assert body["annotations"] == []
    reasons = {r["label"]: r["reason"] for r in body["review_items"]}
    assert reasons == {"甲公司A": "partially_deleted", "甲公司B": "partially_deleted"}


def test_get_unknown_version_and_missing_manuscript(client):
    assert client.get("/api/current").status_code == 404
    assert client.get("/api/versions").json()["versions"] == []
    assert client.get("/api/versions/99").status_code == 404
    assert replace(client, 1, "甲乙").status_code == 404


def test_annotations_listing_defaults_to_current(client):
    post_manuscript(client, "甲乙丙")
    annotate(client, 1, 0, 1, "甲")
    replace(client, 1, "甲X乙丙")
    resp = client.get("/api/annotations").json()
    assert resp["version"] == 2
    assert [a["label"] for a in resp["annotations"]] == ["甲"]
    old = client.get("/api/annotations?version=1").json()
    assert [a["label"] for a in old["annotations"]] == ["甲"]
    assert [a["start"] for a in old["annotations"]] == [0]
