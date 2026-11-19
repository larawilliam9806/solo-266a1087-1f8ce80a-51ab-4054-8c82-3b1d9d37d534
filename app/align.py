"""外业平面格网对齐：仿射变换、格点吸附、接触类型与归属一致性核查。

流程：
- 三对控制点（源侧三点不共线、目标侧三点不共线）唯一确定仿射变换 t = A·s + b；
- 区域、宗地、井先经仿射变换到目标平面，再把每个坐标吸附到以目标坐标
  原点为起点、grid_spacing 为间距的最近格点（每轴同距时取较小值）；
- 吸附后坐标必为 grid_spacing 的整数倍（即整数），可复用 geo.py 原有的
  整数坐标边界有效性核查与覆盖核查；
- 另加两项与源版的一致性核查：所有宗地对的两两接触类型
  （不接触 / 点接触 / 共边）一致；每口井的归属状态与候选宗地一致。
"""
from __future__ import annotations

import math
from typing import Any, Optional, Sequence

from shapely.geometry import Polygon

# 仿射变换参数：(a11, a12, b1, a21, a22, b2)，即 t = A·s + b。
Affine = tuple[float, float, float, float, float, float]

# 吸附取整时的微扰：恰好同距（x/d 为 k+0.5）时保证取较小值，
# 同时吸收仿射计算带来的浮点噪声。
_SNAP_EPS = 1e-9


# ---------------------------------------------------------------- 控制点

def collinear_reason(points: Sequence[tuple[float, float]], label: str) -> Optional[dict]:
    """三点共线则返回一条 reason，否则返回 None。"""
    (x1, y1), (x2, y2), (x3, y3) = points
    cross = (x2 - x1) * (y3 - y1) - (x3 - x1) * (y2 - y1)
    scale = max(1.0, math.hypot(x2 - x1, y2 - y1) * math.hypot(x3 - x1, y3 - y1))
    if abs(cross) <= 1e-9 * scale:
        return {"code": "collinear_control_points",
                "message": f"{label}三个控制点共线（或退化为一点），无法建立仿射变换"}
    return None


def solve_affine(src: Sequence[tuple[float, float]],
                 dst: Sequence[tuple[float, float]]) -> Affine:
    """由三对不共线控制点求仿射变换 t = A·s + b（调用前须确认两侧均不共线）。"""
    (sx1, sy1), (sx2, sy2), (sx3, sy3) = src
    (tx1, ty1), (tx2, ty2), (tx3, ty3) = dst
    # S 的列：源侧两条边向量；T 的列：目标侧对应边向量。A = T · S⁻¹。
    e1x, e1y = sx2 - sx1, sy2 - sy1
    e2x, e2y = sx3 - sx1, sy3 - sy1
    det = e1x * e2y - e2x * e1y
    f1x, f1y = tx2 - tx1, ty2 - ty1
    f2x, f2y = tx3 - tx1, ty3 - ty1
    a11 = (f1x * e2y - f2x * e1y) / det
    a12 = (f2x * e1x - f1x * e2x) / det
    a21 = (f1y * e2y - f2y * e1y) / det
    a22 = (f2y * e1x - f1y * e2x) / det
    b1 = tx1 - (a11 * sx1 + a12 * sy1)
    b2 = ty1 - (a21 * sx1 + a22 * sy1)
    return (a11, a12, b1, a21, a22, b2)


def apply_affine(m: Affine, x: float, y: float) -> tuple[float, float]:
    a11, a12, b1, a21, a22, b2 = m
    return (a11 * x + a12 * y + b1, a21 * x + a22 * y + b2)


# ---------------------------------------------------------------- 格点吸附

def snap_to_grid(value: float, spacing: int) -> int:
    """吸附到以原点为起点、spacing 为间距的最近格点；同距时取较小值。"""
    return int(math.floor(value / spacing + 0.5 - _SNAP_EPS)) * spacing


def transform_polygon_geojson(geom: dict, m: Affine, spacing: int) -> dict:
    """无洞 Polygon GeoJSON → 仿射变换 → 逐坐标吸附（结果为整数坐标）。"""
    ring = [
        [snap_to_grid(x, spacing), snap_to_grid(y, spacing)]
        for x, y in (apply_affine(m, px, py) for px, py in geom["coordinates"][0])
    ]
    return {"type": "Polygon", "coordinates": [ring]}


def transform_point_geojson(geom: dict, m: Affine, spacing: int) -> dict:
    """Point GeoJSON → 仿射变换 → 逐坐标吸附（结果为整数坐标）。"""
    x, y = apply_affine(m, geom["coordinates"][0], geom["coordinates"][1])
    return {"type": "Point",
            "coordinates": [snap_to_grid(x, spacing), snap_to_grid(y, spacing)]}


# ---------------------------------------------------------------- 接触类型

_CONTACT_LABELS = {"none": "不接触", "point": "点接触", "edge": "共边", "overlap": "内部重叠"}


def _dimension(geom: Any) -> int:
    """几何对象的拓扑维度：点 0、线 1、面 2；空为 -1。"""
    if geom.is_empty:
        return -1
    gtype = geom.geom_type
    if gtype in ("Point", "MultiPoint"):
        return 0
    if gtype in ("LineString", "LinearRing", "MultiLineString"):
        return 1
    if gtype in ("Polygon", "MultiPolygon"):
        return 2
    if gtype == "GeometryCollection":
        return max((_dimension(g) for g in geom.geoms), default=-1)
    return -1


def contact_type(a: Polygon, b: Polygon) -> str:
    """两宗地的接触类型：none（不接触）/ point（点接触）/ edge（共边）。

    有效版本中不会出现内部重叠；若出现（防御性）返回 "overlap"。
    """
    inter = a.intersection(b)
    if inter.is_empty:
        return "none"
    dim = _dimension(inter)
    if dim >= 2:
        return "overlap"
    if dim == 1:
        return "edge"
    return "point"


def compare_contact_types(source: dict[str, Polygon],
                          aligned: dict[str, Polygon]) -> list[dict]:
    """逐宗地对比较源版与对齐副本的接触类型，返回不一致的 reason 列表。"""
    reasons: list[dict] = []
    ids = sorted(source)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            src_type = contact_type(source[a], source[b])
            new_type = contact_type(aligned[a], aligned[b])
            if src_type != new_type:
                reasons.append({
                    "code": "contact_type_changed",
                    "message": f"宗地对 {a}/{b} 接触类型由"
                               f"{_CONTACT_LABELS[src_type]}变为{_CONTACT_LABELS[new_type]}",
                    "parcel_ids": [a, b],
                    "source_contact": src_type,
                    "aligned_contact": new_type,
                })
    return reasons


# ---------------------------------------------------------------- 井归属一致性

def compare_attributions(source: dict[str, dict],
                         aligned: dict[str, dict]) -> list[dict]:
    """逐井比较归属状态与候选宗地，返回不一致的 reason 列表。"""
    reasons: list[dict] = []
    for wid in sorted(source):
        src, new = source[wid], aligned[wid]
        if (src["status"] != new["status"]
                or src["parcel_id"] != new["parcel_id"]
                or list(src["candidates"]) != list(new["candidates"])):
            reasons.append({
                "code": "well_attribution_changed",
                "message": f"井 {wid} 归属由 {src['status']} 变为 {new['status']}，"
                           "与源版不一致",
                "well_id": wid,
                "source": {"status": src["status"], "parcel_id": src["parcel_id"],
                           "candidates": list(src["candidates"])},
                "aligned": {"status": new["status"], "parcel_id": new["parcel_id"],
                            "candidates": list(new["candidates"])},
            })
    return reasons
