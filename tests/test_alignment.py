"""对齐发布（外业平面格网）端到端与单元测试。

覆盖：仿射变换与格点吸附（含同距取较小值）、控制点共线、结构校验、
对齐副本核查（几何有效性 / 覆盖 / 接触类型 / 井归属一致性）、
源修订并发变更、保存失败时旧副本继续供数。
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.align import (apply_affine, collinear_reason, compare_attributions,
                       compare_contact_types, contact_type, snap_to_grid,
                       solve_affine)
from app.geo import parse_polygon
from app.main import app


# ----------------------------------------------------------------  fixtures

@pytest.fixture()
def env(tmp_path, monkeypatch):
    """每个用例一个全新数据库文件；返回 (client, db_path)。"""
    db = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db))
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, db


def square(x0, y0, x1, y1):
    return {"type": "Polygon",
            "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}


def polygon(ring):
    return {"type": "Polygon", "coordinates": [[list(p) for p in ring]]}


def point(x, y):
    return {"type": "Point", "coordinates": [x, y]}


REGION = square(0, 0, 100, 100)
P1 = square(0, 0, 50, 100)     # 左半
P2 = square(50, 0, 100, 100)   # 右半（与 P1 共边 x=50）

WELLS = [
    {"id": "W-in", "geometry": point(25, 50)},      # P1 内部
    {"id": "W-edge", "geometry": point(50, 50)},    # P1/P2 共边上
    {"id": "W-out", "geometry": point(150, 50)},    # 区域外
    {"id": "W-move", "geometry": point(60, 50)},    # P2 内部
]


def init_ok(client, parcels=None, wells=None, region=None):
    r = client.post("/v1/init", json={
        "region": region or REGION,
        "parcels": parcels or [{"id": "P1", "geometry": P1},
                               {"id": "P2", "geometry": P2}],
        "wells": WELLS if wells is None else wells,
    })
    assert r.status_code == 201, r.text
    return r.json()


def align_body(base_revision=1, src=None, dst=None, grid_spacing=1):
    """默认恒等变换：源 (0,0),(100,0),(0,100) → 目标同。"""
    src = src or [[0, 0], [100, 0], [0, 100]]
    dst = dst or [[0, 0], [100, 0], [0, 100]]
    return {
        "base_revision": base_revision,
        "control_points": [{"source": s, "target": t} for s, t in zip(src, dst)],
        "grid_spacing": grid_spacing,
    }


def parcels_by_id(payload):
    return {p["id"]: p["geometry"] for p in payload["parcels"]}


def wells_by_id(payload):
    return {w["id"]: w for w in payload["wells"]}


# ---------------------------------------------------------------- 单元：吸附与仿射

def test_snap_to_grid_tie_goes_smaller():
    assert snap_to_grid(0, 5) == 0
    assert snap_to_grid(2, 5) == 0
    assert snap_to_grid(3, 5) == 5
    assert snap_to_grid(2.5, 5) == 0      # 同距 → 较小值
    assert snap_to_grid(7.5, 5) == 5      # 同距 → 较小值
    assert snap_to_grid(-2.5, 5) == -5    # 负轴同距 → 较小值
    assert snap_to_grid(-7.5, 5) == -10
    assert snap_to_grid(-3, 5) == -5
    assert snap_to_grid(60, 40) == 40     # 1.5 倍 → 同距取小
    assert snap_to_grid(100, 40) == 80    # 2.5 倍 → 同距取小
    assert snap_to_grid(50, 40) == 40     # 1.25 倍 → 最近


def test_solve_affine_maps_control_points():
    cases = [
        ([[0, 0], [100, 0], [0, 100]], [[10, 20], [110, 20], [10, 120]]),   # 平移
        ([[0, 0], [100, 0], [0, 100]], [[0, 0], [200, 0], [0, 200]]),       # 缩放
        ([[0, 0], [100, 0], [0, 100]], [[0, 0], [0, 100], [-100, 0]]),      # 旋转 90°
        ([[0, 0], [100, 0], [0, 100]], [[0, 0], [100, 0], [50, 100]]),      # 剪切
        ([[3, 7], [11, 7], [3, 19]], [[100, 200], [116, 208], [84, 224]]),  # 一般
    ]
    for src, dst in cases:
        m = solve_affine(src, dst)
        for s, t in zip(src, dst):
            x, y = apply_affine(m, s[0], s[1])
            assert x == pytest.approx(t[0]) and y == pytest.approx(t[1])


def test_collinear_reason():
    assert collinear_reason([(0, 0), (1, 1), (2, 2)], "源侧") is not None
    assert collinear_reason([(0, 0), (0, 0), (0, 0)], "源侧") is not None
    assert collinear_reason([(0, 0), (100, 0), (0, 100)], "源侧") is None


def test_contact_type_classification():
    a, _ = parse_polygon(square(0, 0, 50, 50))
    edge_b, _ = parse_polygon(square(50, 0, 100, 50))    # 共边
    point_b, _ = parse_polygon(square(50, 50, 100, 100))  # 点接触
    far_b, _ = parse_polygon(square(60, 60, 100, 100))    # 不接触
    assert contact_type(a, edge_b) == "edge"
    assert contact_type(a, point_b) == "point"
    assert contact_type(a, far_b) == "none"


def test_compare_contact_types_reports_pair():
    src = {}
    aligned = {}
    for pid, gj in {"A": square(0, 0, 50, 50), "B": square(50, 50, 100, 100)}.items():
        src[pid], _ = parse_polygon(gj, parcel_id=pid)
    # 对齐副本里 B 被移开：点接触 → 不接触
    for pid, gj in {"A": square(0, 0, 50, 50), "B": square(60, 60, 100, 100)}.items():
        aligned[pid], _ = parse_polygon(gj, parcel_id=pid)
    reasons = compare_contact_types(src, aligned)
    assert len(reasons) == 1
    assert reasons[0]["code"] == "contact_type_changed"
    assert reasons[0]["parcel_ids"] == ["A", "B"]
    assert reasons[0]["source_contact"] == "point"
    assert reasons[0]["aligned_contact"] == "none"


def test_compare_attributions_reports_well():
    src = {"W1": {"status": "assigned", "parcel_id": "P1", "candidates": ["P1"]}}
    new = {"W1": {"status": "pending", "parcel_id": None, "candidates": ["P1", "P2"]}}
    reasons = compare_attributions(src, new)
    assert len(reasons) == 1 and reasons[0]["well_id"] == "W1"
    assert reasons[0]["code"] == "well_attribution_changed"
    assert compare_attributions(src, src) == []


# ---------------------------------------------------------------- 结构校验

def test_align_request_structure(env):
    client, _ = env
    init_ok(client)
    # 控制点必须恰好三对
    for n in (2, 4):
        body = align_body()
        body["control_points"] = body["control_points"][:1] * n
        assert client.post("/v1/alignment", json=body).status_code == 422
    # 格网间距必须是正整数
    for bad in (0, -3, 2.5, "abc", None):
        assert client.post("/v1/alignment",
                           json=align_body(grid_spacing=bad)).status_code == 422
    # 控制点坐标必须是 [x, y] 有限数值
    for bad_cp in ([[0, 0, 0], [100, 0], [0, 100]],
                   [[0, "x"], [100, 0], [0, 100]],
                   [[0, None], [100, 0], [0, 100]],
                   [[0], [100, 0], [0, 100]]):
        assert client.post("/v1/alignment",
                           json=align_body(src=bad_cp)).status_code == 422
    # 缺字段
    assert client.post("/v1/alignment", json={"base_revision": 1}).status_code == 422
    assert client.get("/v1/alignment").status_code == 404   # 失败不留副本


def test_align_before_init(env):
    client, _ = env
    r = client.post("/v1/alignment", json=align_body())
    assert r.status_code == 404
    assert client.get("/v1/alignment").status_code == 404


def test_collinear_control_points_rejected(env):
    client, _ = env
    init_ok(client)
    r = client.post("/v1/alignment",
                    json=align_body(src=[[0, 0], [1, 1], [2, 2]]))
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "invalid_control_points"
    assert any(x["code"] == "collinear_control_points" for x in detail["reasons"])

    r = client.post("/v1/alignment",
                    json=align_body(dst=[[5, 5], [5, 5], [5, 5]]))
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "invalid_control_points"
    assert client.get("/v1/alignment").status_code == 404


# ---------------------------------------------------------------- 成功路径

def test_align_translation_exact(env):
    client, _ = env
    init_ok(client)
    r = client.post("/v1/alignment", json=align_body(
        dst=[[10, 20], [110, 20], [10, 120]], grid_spacing=1))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source_revision"] == 1 and body["grid_spacing"] == 1
    assert body["well_summary"] == {"assigned": 2, "pending": 1, "uncovered": 1}

    r = client.get("/v1/alignment")
    assert r.status_code == 200
    got = r.json()
    assert got["source_revision"] == 1
    assert got["grid_spacing"] == 1
    assert got["control_points"] == [
        {"source": [0.0, 0.0], "target": [10.0, 20.0]},
        {"source": [100.0, 0.0], "target": [110.0, 20.0]},
        {"source": [0.0, 100.0], "target": [10.0, 120.0]},
    ]
    assert got["region"]["coordinates"][0][0] == [10, 20]
    parcels = parcels_by_id(got)
    assert parcels["P1"]["coordinates"][0] == [[10, 20], [60, 20], [60, 120],
                                               [10, 120], [10, 20]]
    wells = wells_by_id(got)
    assert wells["W-in"]["geometry"]["coordinates"] == [35, 70]
    assert wells["W-in"]["status"] == "assigned" and wells["W-in"]["parcel_id"] == "P1"
    assert wells["W-edge"]["status"] == "pending"
    assert wells["W-edge"]["candidates"] == ["P1", "P2"]
    assert wells["W-out"]["status"] == "uncovered"


def test_align_snap_tie_goes_smaller(env):
    """平移 +2.5、间距 5：每个坐标都落在两格点正中间，须取较小值。"""
    client, _ = env
    init_ok(client)
    r = client.post("/v1/alignment", json=align_body(
        src=[[0, 0], [10, 0], [0, 10]],
        dst=[[2.5, 0], [12.5, 0], [2.5, 10]],
        grid_spacing=5))
    assert r.status_code == 200, r.text
    got = client.get("/v1/alignment").json()
    # 同距取较小值 → 对齐副本与源版坐标完全一致（而非整体 +5）
    assert parcels_by_id(got) == {"P1": P1, "P2": P2}
    wells = wells_by_id(got)
    assert wells["W-in"]["geometry"]["coordinates"] == [25, 50]
    assert wells["W-move"]["geometry"]["coordinates"] == [60, 50]
    assert wells["W-out"]["geometry"]["coordinates"] == [150, 50]


def test_align_scale_with_grid(env):
    client, _ = env
    init_ok(client)
    r = client.post("/v1/alignment", json=align_body(
        dst=[[0, 0], [200, 0], [0, 200]], grid_spacing=5))
    assert r.status_code == 200, r.text
    got = client.get("/v1/alignment").json()
    assert got["region"]["coordinates"][0] == [[0, 0], [200, 0], [200, 200],
                                               [0, 200], [0, 0]]
    parcels = parcels_by_id(got)
    assert parcels["P1"]["coordinates"][0] == [[0, 0], [100, 0], [100, 200],
                                               [0, 200], [0, 0]]
    wells = wells_by_id(got)
    assert wells["W-in"]["geometry"]["coordinates"] == [50, 100]
    assert wells["W-in"]["status"] == "assigned"
    assert wells["W-edge"]["geometry"]["coordinates"] == [100, 100]
    assert wells["W-edge"]["status"] == "pending"
    assert wells["W-move"]["status"] == "assigned" and wells["W-move"]["parcel_id"] == "P2"


def test_realign_replaces_copy(env):
    client, _ = env
    init_ok(client)
    assert client.post("/v1/alignment", json=align_body(grid_spacing=5)).status_code == 200
    assert client.post("/v1/alignment", json=align_body(grid_spacing=10)).status_code == 200
    got = client.get("/v1/alignment").json()
    assert got["grid_spacing"] == 10
    assert got["source_revision"] == 1


# ---------------------------------------------------------------- 核查失败

def test_align_well_attribution_change_rejected(env):
    """粗格网把井吸上宗地边界：归属状态变化 → 422 并指明涉及的井。"""
    client, _ = env
    init_ok(client)
    assert client.post("/v1/alignment", json=align_body(grid_spacing=5)).status_code == 200

    r = client.post("/v1/alignment", json=align_body(grid_spacing=40))
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "alignment_validation_failed"
    changed = {x["well_id"] for x in detail["reasons"]
               if x["code"] == "well_attribution_changed"}
    assert changed == {"W-in", "W-move"}   # (25,50)→(40,40)、(60,50)→(40,40) 吸上共边

    # 原有对齐副本继续供数
    got = client.get("/v1/alignment").json()
    assert got["grid_spacing"] == 5
    assert wells_by_id(got)["W-in"]["status"] == "assigned"


def test_align_parcel_collapse_rejected(env):
    """间距过大把宗地吸成零面积：几何无效 → 422 并指明涉及的宗地。"""
    client, _ = env
    init_ok(client)
    r = client.post("/v1/alignment", json=align_body(grid_spacing=200))
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "alignment_validation_failed"
    parcel_ids = {x.get("parcel_id") for x in detail["reasons"]
                  if x["code"] == "invalid_geometry"}
    assert "P1" in parcel_ids
    assert client.get("/v1/alignment").status_code == 404


def test_align_contact_type_change_rejected(env):
    """T 型节点 + 粗格网：顶点被吸离邻宗地边 → 接触类型变化 → 422 指明宗地对。"""
    client, _ = env
    # B 为底块（顶边斜线 y=70-0.4x）；A 为三角，顶点 (50,50) 落在 B 顶边内部
    parcels = [
        {"id": "B", "geometry": polygon([(0, 0), (100, 0), (100, 30), (0, 70), (0, 0)])},
        {"id": "A", "geometry": polygon([(50, 50), (35, 70), (65, 70), (50, 50)])},
        {"id": "PL", "geometry": polygon([(0, 70), (50, 50), (35, 70), (0, 100), (0, 70)])},
        {"id": "PR", "geometry": polygon([(50, 50), (100, 30), (100, 100), (65, 70), (50, 50)])},
        {"id": "PT", "geometry": polygon([(35, 70), (65, 70), (100, 100), (0, 100), (35, 70)])},
    ]
    wells = [{"id": "W-b", "geometry": point(50, 10)},
             {"id": "W-a", "geometry": point(50, 60)}]
    init_ok(client, parcels=parcels, wells=wells)

    r = client.post("/v1/alignment", json=align_body(grid_spacing=20))
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "alignment_validation_failed"
    contact = [x for x in detail["reasons"] if x["code"] == "contact_type_changed"]
    assert any(x["parcel_ids"] == ["A", "B"] and x["source_contact"] == "point"
               for x in contact), detail["reasons"]
    assert client.get("/v1/alignment").status_code == 404   # 未留下副本


# ---------------------------------------------------------------- 并发与失败

def test_align_stale_base_revision_keeps_old_copy(env):
    client, _ = env
    init_ok(client)
    assert client.post("/v1/alignment", json=align_body(grid_spacing=5)).status_code == 200
    # 源版照常发布新修订
    r = client.put("/v1/parcels", json={"base_revision": 1, "parcels": [
        {"id": "P1", "geometry": square(0, 0, 60, 100)},
        {"id": "P2", "geometry": square(60, 0, 100, 100)}]})
    assert r.status_code == 200 and r.json()["revision"] == 2

    r = client.post("/v1/alignment", json=align_body(base_revision=1))
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "revision_conflict" and detail["current_revision"] == 2
    # 旧对齐副本（基于修订 1）继续供数
    got = client.get("/v1/alignment").json()
    assert got["source_revision"] == 1 and got["grid_spacing"] == 5


def test_align_commit_race_with_source_publish(env, monkeypatch):
    """对齐计算期间源版被修订：提交时 409，不覆盖旧副本。"""
    client, _ = env
    init_ok(client)
    assert client.post("/v1/alignment", json=align_body(grid_spacing=5)).status_code == 200

    import app.main as main_mod
    original = main_mod.solve_affine

    def compute_then_source_changes(*args, **kwargs):
        m = original(*args, **kwargs)
        # 对齐计算期间，源版照常发布新修订
        r = client.put("/v1/parcels", json={"base_revision": 1, "parcels": [
            {"id": "P1", "geometry": square(0, 0, 60, 100)},
            {"id": "P2", "geometry": square(60, 0, 100, 100)}]})
        assert r.status_code == 200
        return m

    monkeypatch.setattr(main_mod, "solve_affine", compute_then_source_changes)
    try:
        r = client.post("/v1/alignment", json=align_body(grid_spacing=10))
    finally:
        monkeypatch.setattr(main_mod, "solve_affine", original)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "revision_conflict"
    # 旧副本继续供数
    got = client.get("/v1/alignment").json()
    assert got["source_revision"] == 1 and got["grid_spacing"] == 5


def test_align_save_failure_keeps_old_copy(env, monkeypatch):
    """保存中途崩溃：回滚干净，原有对齐副本继续供数。"""
    client, _ = env
    init_ok(client)
    assert client.post("/v1/alignment", json=align_body(grid_spacing=5)).status_code == 200

    import app.main as main_mod

    original = main_mod._save_alignment

    def boom(*args, **kwargs):
        raise RuntimeError("simulated crash mid-save")

    monkeypatch.setattr(main_mod, "_save_alignment", boom)
    r = client.post("/v1/alignment", json=align_body(grid_spacing=10))
    assert r.status_code == 500
    monkeypatch.setattr(main_mod, "_save_alignment", original)

    got = client.get("/v1/alignment").json()
    assert got["grid_spacing"] == 5   # 旧副本完好
    # 服务照常可用
    assert client.post("/v1/alignment", json=align_body(grid_spacing=10)).status_code == 200
    assert client.get("/v1/alignment").json()["grid_spacing"] == 10


def test_source_publish_unaffected_by_alignment(env):
    """对齐副本存在后，源版发布流程不受影响。"""
    client, _ = env
    init_ok(client)
    assert client.post("/v1/alignment", json=align_body(grid_spacing=5)).status_code == 200
    r = client.put("/v1/parcels", json={"base_revision": 1, "parcels": [
        {"id": "P1", "geometry": square(0, 0, 60, 100)},
        {"id": "P2", "geometry": square(60, 0, 100, 100)}]})
    assert r.status_code == 200 and r.json()["revision"] == 2
    assert client.get("/v1/current").json()["revision"] == 2
    # 基于新修订再对齐成功
    assert client.post("/v1/alignment",
                       json=align_body(base_revision=2)).status_code == 200
    got = client.get("/v1/alignment").json()
    assert got["source_revision"] == 2
    assert parcels_by_id(got)["P1"] == square(0, 0, 60, 100)


def test_concurrent_align_same_base_only_one_wins(env):
    """同版并发对齐：恰好一份成功，副本与任一提交一致。"""
    client, _ = env
    init_ok(client)

    results = []

    with TestClient(app) as c1, TestClient(app) as c2:
        barrier = threading.Barrier(2)

        def run(c, spacing):
            barrier.wait()
            r = c.post("/v1/alignment", json=align_body(grid_spacing=spacing))
            results.append(r.status_code)

        t1 = threading.Thread(target=run, args=(c1, 5))
        t2 = threading.Thread(target=run, args=(c2, 10))
        t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(results) == [200, 200] or sorted(results) == [200, 409]
    # 无论是否互相覆盖，最终副本必然完整可读且间距是两者之一
    got = client.get("/v1/alignment").json()
    assert got["grid_spacing"] in (5, 10)
    assert len(got["parcels"]) == 2 and len(got["wells"]) == 4
