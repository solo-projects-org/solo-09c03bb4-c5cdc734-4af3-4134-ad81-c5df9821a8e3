"""外发补标包：遮盖、回收换算、幂等回执与换稿交错。"""

from contextlib import closing

import pytest
from conftest import annotate, post_manuscript, replace

from app.db import connect


def make_pack(client, version, ids):
    return client.post(
        "/api/dispatch-packs",
        json={"version": version, "mask_annotation_ids": ids},
    )


def claim(client, pack_no, intervals):
    return client.post(
        f"/api/dispatch-packs/{pack_no}/claims",
        json={
            "annotations": [
                {"start": start, "end": end, "label": label}
                for start, end, label in intervals
            ]
        },
    )


TEXT = "张三在武昌府见到李四。\n张三又说永安桥在城东。"


def annotation_ids(client, version=1):
    resp = client.get(f"/api/annotations?version={version}")
    return {a["label"]: a["id"] for a in resp.json()["annotations"]}


def test_pack_masks_only_returns_pack_no_and_masked_text(client):
    post_manuscript(client, TEXT)
    annotate(client, 1, 0, 2, "张三")
    annotate(client, 1, 3, 6, "武昌府")
    ids = annotation_ids(client)
    resp = make_pack(client, 1, [ids["张三"], ids["武昌府"]])
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"pack_no", "masked_text"}
    assert body["masked_text"].startswith("□在□见到李四。")
    assert "武昌府" not in body["masked_text"]
    # 只遮盖指定的标注区间；后文另一处“张三”不在本次遮盖范围内。
    assert body["masked_text"].count("□") == 2


def test_overlapping_masks_merge_and_adjacent_masks_stay_separate(client):
    post_manuscript(client, "前文甲乙丙丁后文")
    # 0前 1文 2甲 3乙 4丙 5丁 6后 7文
    # [1,3) 与 [2,4) 相交 -> 合并为 [1,4)，只留一个「□」。
    annotate(client, 1, 1, 3, "文甲")
    annotate(client, 1, 2, 4, "甲乙")
    ids = annotation_ids(client)
    body = make_pack(client, 1, [ids["文甲"], ids["甲乙"]]).json()
    assert body["masked_text"] == "前□丙丁后文"

    # 再加上首尾相接的 [4,5)：与 [1,4) 不相交，保留两个「□」。
    annotate(client, 1, 4, 5, "丙")
    ids = annotation_ids(client)
    body = make_pack(client, 1, [ids["文甲"], ids["甲乙"], ids["丙"]]).json()
    assert body["masked_text"] == "前□□丁后文"


def test_same_pack_accepts_multiple_named_places(client):
    post_manuscript(client, "永安桥在东，永安桥在西。")
    # 0永 1安 2桥 3在 4东 5， 6永 7安 8桥 9在 10西 11。
    annotate(client, 1, 0, 3, "永安桥")
    annotate(client, 1, 6, 9, "永安桥")
    ids = [a["id"] for a in client.get("/api/annotations").json()["annotations"]]
    pack_no = make_pack(client, 1, ids).json()["pack_no"]
    # 遮盖后：□在东，□在西。同一包里两条标注允许同名。
    resp = claim(client, pack_no, [(1, 3, "在东"), (5, 7, "在西")])
    assert resp.status_code == 200
    labels = [(a["start"], a["end"], a["label"]) for a in resp.json()["annotations"]]
    assert labels == [(3, 5, "在东"), (9, 11, "在西")]


def test_claim_maps_intervals_back_across_masks(client):
    post_manuscript(client, TEXT)
    annotate(client, 1, 0, 2, "张三")
    annotate(client, 1, 8, 10, "李四")
    ids = annotation_ids(client)
    pack_no = make_pack(client, 1, [ids["张三"], ids["李四"]]).json()["pack_no"]

    # 遮盖后：□在武昌府见到□。张三又说永安桥在城东。
    # 下标：0□ 1在 2武 3昌 4府 5见 6到 7□ 8。 9张 10三 11又
    #       12说 13永 14安 15桥 16在 17城 18东 19。
    resp = claim(
        client,
        pack_no,
        [(2, 5, "武昌府"), (8, 9, "。"), (9, 11, "张三"), (13, 16, "永安桥")],
    )
    assert resp.status_code == 200, resp.json()
    receipt = resp.json()
    assert receipt["status"] == "accepted"
    assert receipt["annotation_count"] == 4
    positions = {
        (a["start"], a["end"], a["label"]) for a in receipt["annotations"]
    }
    # 每个区间都按它所在的未遮盖片段平移回原文；跨过“□”后偏移量正确。
    assert positions == {
        (3, 6, "武昌府"),
        (10, 11, "。"),
        (11, 13, "张三"),
        (15, 18, "永安桥"),
    }
    current = client.get("/api/current").json()
    stored = {(a["start"], a["end"], a["label"]) for a in current["annotations"]}
    assert positions <= stored


def test_claim_receipt_records_masked_and_original_coordinates(client):
    post_manuscript(client, TEXT)
    annotate(client, 1, 0, 2, "张三")
    ids = annotation_ids(client)
    pack_no = make_pack(client, 1, [ids["张三"]]).json()["pack_no"]
    receipt = claim(client, pack_no, [(1, 3, "在武")]).json()
    (item,) = receipt["annotations"]
    assert (item["masked_start"], item["masked_end"]) == (1, 3)
    assert (item["start"], item["end"]) == (2, 4)
    assert item["annotation_id"]
    fetched = client.get(f"/api/dispatch-packs/{pack_no}/receipt").json()
    assert fetched == receipt


@pytest.mark.parametrize(
    "interval,reason",
    [
        # 从「□」伸进其后的未遮盖片段
        ((0, 2), "crosses_masked_span"),
        # 整个落在「□」上
        ((0, 1), "inside_masked_span"),
        # 越界
        ((0, 999), "out_of_range"),
    ],
)
def test_invalid_interval_rejects_whole_pack(client, interval, reason):
    post_manuscript(client, TEXT)
    annotate(client, 1, 0, 2, "张三")
    ids = annotation_ids(client)
    pack_no = make_pack(client, 1, [ids["张三"]]).json()["pack_no"]

    resp = claim(client, pack_no, [(1, 3, "在武"), (interval[0], interval[1], "坏区间")])
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "claim_rejected"
    assert detail["errors"][0]["reason"] == reason

    # 整包退回：没有任何标注落库，也没有回执。
    assert [
        a["label"] for a in client.get("/api/annotations").json()["annotations"]
    ] == ["张三"]
    assert client.get(f"/api/dispatch-packs/{pack_no}/receipt").status_code == 404

    # 修正后仍可正常回收（首次回收才算数）。
    assert claim(client, pack_no, [(1, 3, "在武")]).status_code == 200


def test_pack_creation_rejects_stale_version_and_foreign_annotation(client):
    post_manuscript(client, "甲乙丙丁")
    annotate(client, 1, 0, 2, "甲乙")
    ids = annotation_ids(client)
    replace(client, 1, "甲乙丙丁戊")
    stale = make_pack(client, 1, [ids["甲乙"]])
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "stale_version"
    assert stale.json()["detail"]["current_version"] == 2

    # 旧版标注不能拿来遮盖现行版。
    missing = make_pack(client, 2, [ids["甲乙"]])
    assert missing.status_code == 422
    assert missing.json()["detail"]["missing_annotation_ids"] == [ids["甲乙"]]


def test_claim_rejected_after_edition_changed(client):
    post_manuscript(client, "甲乙丙丁")
    annotate(client, 1, 0, 2, "甲乙")
    ids = annotation_ids(client)
    pack_no = make_pack(client, 1, [ids["甲乙"]]).json()["pack_no"]
    replace(client, 1, "甲乙丙丁戊")

    resp = claim(client, pack_no, [(1, 3, "丙丁")])
    assert resp.status_code == 409
    assert resp.json()["detail"]["errors"][0]["reason"] == "stale_edition"
    assert client.get(f"/api/dispatch-packs/{pack_no}/receipt").status_code == 404
    assert [
        a["label"] for a in client.get("/api/annotations").json()["annotations"]
    ] == ["甲乙"]


def test_identical_resend_returns_first_receipt_after_replace_and_restart(client):
    post_manuscript(client, "甲乙丙丁")
    annotate(client, 1, 0, 2, "甲乙")
    ids = annotation_ids(client)
    pack_no = make_pack(client, 1, [ids["甲乙"]]).json()["pack_no"]
    # 遮盖后：□丙丁 -> [1,2) 丙，[2,3) 丁
    first = claim(client, pack_no, [(1, 2, "丙"), (2, 3, "丁")]).json()
    assert first["annotation_count"] == 2

    # 换版后原样重送仍返回原回执，且不重复写入标注。
    assert replace(client, 1, "甲X乙丙丁").status_code == 200
    before = client.get("/api/annotations").json()["annotations"]
    again = claim(client, pack_no, [(2, 3, "丁"), (1, 2, "丙")])  # 顺序无关
    assert again.status_code == 200
    assert again.json() == first
    after = client.get("/api/annotations").json()["annotations"]
    assert after == before

    # 模拟进程重启：新开一条数据库连接，回执仍一致。
    with closing(connect()) as conn:
        from app import service

        assert (
            service.claim_dispatch_pack(
                conn,
                pack_no=pack_no,
                intervals=[(1, 2, "丙"), (2, 3, "丁")],
            )
            == first
        )


def test_identical_resend_returns_receipt_after_app_restart(tmp_path, monkeypatch):
    """真正的应用重启：同一个 SQLite 文件，新建 TestClient 与 lifespan。"""

    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "restart.db"))
    with TestClient(app) as first_client:
        post_manuscript(first_client, "甲乙丙丁")
        annotate(first_client, 1, 0, 2, "甲乙")
        ids = annotation_ids(first_client)
        pack_no = make_pack(first_client, 1, [ids["甲乙"]]).json()["pack_no"]
        first = claim(first_client, pack_no, [(1, 2, "丙")]).json()

    with TestClient(app) as second_client:
        again = claim(second_client, pack_no, [(1, 2, "丙")])
        assert again.status_code == 200
        assert again.json() == first
        # 标注不重复写入。
        assert [
            a["label"] for a in second_client.get("/api/annotations").json()["annotations"]
        ] == ["甲乙", "丙"]


def test_same_pack_different_content_is_rejected(client):
    post_manuscript(client, "甲乙丙丁")
    annotate(client, 1, 0, 2, "甲乙")
    ids = annotation_ids(client)
    pack_no = make_pack(client, 1, [ids["甲乙"]]).json()["pack_no"]
    first = claim(client, pack_no, [(1, 2, "丙")]).json()

    resp = claim(client, pack_no, [(1, 2, "丙"), (2, 3, "丁")])
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "claim_mismatch"
    # 原回执与已入库标注都不变。
    assert client.get(f"/api/dispatch-packs/{pack_no}/receipt").json() == first
    assert len(client.get("/api/annotations").json()["annotations"]) == 2


def test_claim_before_replace_is_included_in_migration(client):
    post_manuscript(client, "前文甲乙丙丁后文")
    annotate(client, 1, 2, 4, "甲乙")  # 原有标注，换稿时会被删去“乙”
    ids = annotation_ids(client)
    pack_no = make_pack(client, 1, [ids["甲乙"]]).json()["pack_no"]

    # 遮盖后：前文□丙丁后文 -> “前文”原文 [0,2)，“丙丁”原文 [4,6)。
    receipt = claim(client, pack_no, [(0, 2, "前文"), (3, 5, "丙丁")]).json()
    assert receipt["annotation_count"] == 2

    # 换稿删除“丙”：“丙丁”是回收来的标注，也必须参与迁移判定并进入复核。
    body = replace(client, 1, "前文甲乙丁后文").json()
    assert body["version"] == 2
    labels = {a["label"]: (a["start"], a["end"]) for a in body["annotations"]}
    assert labels == {"前文": (0, 2), "甲乙": (2, 4)}
    reasons = {r["label"]: r["reason"] for r in body["review_items"]}
    assert reasons == {"丙丁": "partially_deleted"}


def test_claim_before_replace_can_migrate(client):
    post_manuscript(client, "前文甲乙丙丁后文")
    annotate(client, 1, 2, 4, "甲乙")
    ids = annotation_ids(client)
    pack_no = make_pack(client, 1, [ids["甲乙"]]).json()["pack_no"]
    claim(client, pack_no, [(3, 5, "丙丁")])

    body = replace(client, 1, "前文甲X乙丙丁后文").json()
    labels = {a["label"]: (a["start"], a["end"]) for a in body["annotations"]}
    assert labels == {"甲乙": (2, 5), "丙丁": (5, 7)}


def test_unknown_pack_and_unclaimed_receipt(client):
    post_manuscript(client, "甲乙")
    assert claim(client, "PK-nope", [(0, 1, "甲")]).status_code == 404
    assert client.get("/api/dispatch-packs/PK-nope").status_code == 404
    assert client.get("/api/dispatch-packs/PK-nope/receipt").status_code == 404


def test_claim_rejects_duplicate_identical_intervals(client):
    post_manuscript(client, "甲乙丙丁")
    annotate(client, 1, 0, 2, "甲乙")
    ids = annotation_ids(client)
    pack_no = make_pack(client, 1, [ids["甲乙"]]).json()["pack_no"]
    resp = claim(client, pack_no, [(1, 2, "丙"), (1, 2, "丙")])
    assert resp.status_code == 422
    assert resp.json()["detail"]["errors"][0]["reason"] == "duplicate_interval"


def test_empty_claim_payload_is_rejected(client):
    post_manuscript(client, "甲乙丙丁")
    annotate(client, 1, 0, 2, "甲乙")
    ids = annotation_ids(client)
    pack_no = make_pack(client, 1, [ids["甲乙"]]).json()["pack_no"]
    # Pydantic 只要一个空列表即可通过模型，由服务层拒绝。
    resp = client.post(f"/api/dispatch-packs/{pack_no}/claims", json={"annotations": []})
    assert resp.status_code == 422
    assert resp.json()["detail"]["errors"][0]["reason"] == "empty"


def test_claim_is_atomic_when_receipt_write_fails(client, monkeypatch):
    """回收标注与回执同一事务：写回执失败时已插入的标注必须回滚。"""

    from app import service

    post_manuscript(client, "甲乙丙丁")
    annotate(client, 1, 0, 2, "甲乙")
    ids = annotation_ids(client)
    pack_no = make_pack(client, 1, [ids["甲乙"]]).json()["pack_no"]

    original_dumps = service.json.dumps

    def failing_dumps(value, *args, **kwargs):
        if isinstance(value, dict) and value.get("status") == "accepted":
            raise RuntimeError("模拟写回执故障")
        return original_dumps(value, *args, **kwargs)

    monkeypatch.setattr(service.json, "dumps", failing_dumps)
    with pytest.raises(RuntimeError):
        claim(client, pack_no, [(1, 2, "丙")])

    monkeypatch.setattr(service.json, "dumps", original_dumps)
    # 事务整体回滚：没有新增标注，也没有回执。
    assert [
        a["label"] for a in client.get("/api/annotations").json()["annotations"]
    ] == ["甲乙"]
    assert client.get(f"/api/dispatch-packs/{pack_no}/receipt").status_code == 404

    # 故障排除后可以正常首次回收。
    resp = claim(client, pack_no, [(1, 2, "丙")])
    assert resp.status_code == 200
    assert resp.json()["annotation_count"] == 1
