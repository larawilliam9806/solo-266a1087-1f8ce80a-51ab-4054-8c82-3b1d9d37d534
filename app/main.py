"""FastAPI 入口：版本化发布、整批替换、同版一致性查询、外业格网对齐发布。

并发与原子性：
- 重活（几何核查、井归属重算、对齐计算）在写事务之外基于 base 版本快照完成；
- 写事务用 BEGIN IMMEDIATE 串行化，进入后先核对 meta.current_revision
  仍等于 base_revision，不等则回滚返回 409 —— 同版并发修订只成功一份；
- 版本行、宗地行、井归属行、版本指针在同一个事务里提交，失败/重启不留半版。
- 对齐副本（meta + 宗地 + 井）也在同一个事务里整体替换：对齐计算期间源版
  可照常发布；提交时源修订已变更、或校验/保存失败，原有对齐副本继续供数。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .align import (collinear_reason, compare_attributions, compare_contact_types,
                    solve_affine, transform_point_geojson, transform_polygon_geojson)
from .db import connect, init_db
from .geo import attribute_wells, parse_point, parse_polygon, validate_tiling
from .schemas import AlignRequest, InitRequest, ReplaceRequest


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="宗地边界与地下水采样井归属发布 API",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------- 工具

def _err(status: int, payload: dict) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": payload})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _current_revision(conn: sqlite3.Connection) -> Optional[int]:
    row = conn.execute("SELECT current_revision FROM meta WHERE id = 1").fetchone()
    return int(row[0]) if row is not None else None


def _load_region(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT geojson FROM regions WHERE id = 1").fetchone()
    return json.loads(row[0])


def _load_parcels(conn: sqlite3.Connection, revision: int) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT parcel_id, geojson FROM parcels WHERE revision = ? ORDER BY parcel_id",
        (revision,),
    ).fetchall()
    return {r[0]: json.loads(r[1]) for r in rows}


def _load_well_geoms(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute("SELECT well_id, geojson FROM wells ORDER BY well_id").fetchall()
    return {r[0]: json.loads(r[1]) for r in rows}


def _load_attributions(conn: sqlite3.Connection, revision: int) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT well_id, status, parcel_id, candidates FROM well_attributions "
        "WHERE revision = ? ORDER BY well_id",
        (revision,),
    ).fetchall()
    return {
        r[0]: {"status": r[1], "parcel_id": r[2], "candidates": json.loads(r[3])}
        for r in rows
    }


def _insert_version(conn: sqlite3.Connection, revision: int,
                    parcels: dict[str, dict], attributions: dict[str, dict],
                    region: Optional[dict], wells: Optional[dict[str, dict]]) -> None:
    """在已开启的事务内写入一个完整版本（不含 meta 指针）。"""
    if region is not None:
        conn.execute("INSERT INTO regions (id, geojson) VALUES (1, ?)",
                     (json.dumps(region),))
    if wells is not None:
        conn.executemany(
            "INSERT INTO wells (well_id, geojson) VALUES (?, ?)",
            [(wid, json.dumps(g)) for wid, g in wells.items()],
        )
    conn.execute(
        "INSERT INTO versions (revision, created_at, parcel_count, well_count) "
        "VALUES (?, ?, ?, ?)",
        (revision, _now(), len(parcels),
         len(wells) if wells is not None else len(attributions)),
    )
    conn.executemany(
        "INSERT INTO parcels (revision, parcel_id, geojson) VALUES (?, ?, ?)",
        [(revision, pid, json.dumps(g)) for pid, g in parcels.items()],
    )
    conn.executemany(
        "INSERT INTO well_attributions (revision, well_id, status, parcel_id, candidates) "
        "VALUES (?, ?, ?, ?, ?)",
        [(revision, wid, a["status"], a["parcel_id"], json.dumps(a["candidates"]))
         for wid, a in attributions.items()],
    )


def _validation_error(reasons: list[dict], *, gap_area: Optional[float] = None,
                      overlaps: Optional[list] = None,
                      overlap_parcel_ids: Optional[list] = None) -> JSONResponse:
    return _err(422, {
        "error": "validation_failed",
        "message": "整批新边界未通过核查，当前版本保持不变",
        "gap_area": gap_area,
        "overlaps": overlaps or [],
        "overlap_parcel_ids": overlap_parcel_ids or [],
        "reasons": reasons,
    })


def _well_summary(attributions: dict[str, dict]) -> dict:
    summary = {"assigned": 0, "pending": 0, "uncovered": 0}
    for a in attributions.values():
        summary[a["status"]] += 1
    return summary


def _parse_parcels(items, reasons: list[dict]) -> tuple[dict[str, Any], dict[str, dict]]:
    """解析一批宗地几何，把重复编号与几何错误追加到 reasons。

    返回 (id -> shapely Polygon, id -> 原始 GeoJSON)。
    """
    geoms: dict[str, Any] = {}
    geojson_by_id: dict[str, dict] = {}
    for p in items:
        if p.id in geojson_by_id:
            reasons.append({"code": "duplicate_parcel_id", "parcel_id": p.id,
                            "message": f"宗地编号 {p.id} 在请求中重复"})
            continue
        geom, errs = parse_polygon(p.geometry, parcel_id=p.id, label=f"宗地 {p.id}")
        reasons.extend(errs)
        if geom is not None:
            geoms[p.id] = geom
            geojson_by_id[p.id] = p.geometry
    return geoms, geojson_by_id


def _revision_conflict(current: int, base: int, message: str) -> JSONResponse:
    return _err(409, {
        "error": "revision_conflict",
        "message": message,
        "current_revision": current,
        "base_revision": base,
    })


# ---------------------------------------------------------------- 首次提交

@app.post("/v1/init", status_code=201)
def initialize(req: InitRequest):
    reasons: list[dict] = []

    region_geom, errs = parse_polygon(req.region, label="区域")
    reasons.extend(errs)

    parcel_geoms, parcel_gj = _parse_parcels(req.parcels, reasons)

    well_geoms: dict[str, Any] = {}
    well_gj: dict[str, dict] = {}
    for w in req.wells:
        if w.id in well_gj:
            reasons.append({"code": "duplicate_well_id", "well_id": w.id,
                            "message": f"井编号 {w.id} 在请求中重复"})
            continue
        geom, errs = parse_point(w.geometry, well_id=w.id, label=f"井 {w.id}")
        reasons.extend(errs)
        if geom is not None:
            well_geoms[w.id] = geom
            well_gj[w.id] = w.geometry

    tiling = None
    if region_geom is not None and len(parcel_geoms) == len(req.parcels):
        tiling = validate_tiling(parcel_geoms, region_geom)
        reasons.extend(tiling.reasons)
    if reasons:
        return _validation_error(
            reasons,
            gap_area=tiling.gap_area if tiling else None,
            overlaps=tiling.overlaps if tiling else None,
            overlap_parcel_ids=tiling.overlap_parcel_ids if tiling else None,
        )

    attributions = attribute_wells(well_geoms, parcel_geoms)

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if _current_revision(conn) is not None:
                conn.execute("ROLLBACK")
                return _err(409, {"error": "already_initialized",
                                  "message": "数据集已完成首次提交，不能重复初始化"})
            _insert_version(conn, 1, parcel_gj, attributions,
                            region=req.region, wells=well_gj)
            conn.execute("INSERT INTO meta (id, current_revision) VALUES (1, 1)")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    return {"revision": 1, "parcel_count": len(parcel_gj),
            "well_count": len(well_gj), "well_summary": _well_summary(attributions)}


# ---------------------------------------------------------------- 整批替换

@app.put("/v1/parcels")
def replace_parcels(req: ReplaceRequest):
    # 1) 读取 base 版本快照（读事务，不阻塞他人）
    conn = connect()
    try:
        current = _current_revision(conn)
        if current is None:
            return _err(404, {"error": "not_initialized",
                              "message": "尚未首次提交，请先调用 POST /v1/init"})
        if req.base_revision != current:
            return _err(409, {
                "error": "revision_conflict",
                "message": f"所见修订号 {req.base_revision} 已过期，当前为 {current}，"
                           "请重新读取后再提交",
                "current_revision": current,
                "base_revision": req.base_revision,
            })
        region_gj = _load_region(conn)
        old_parcel_gj = _load_parcels(conn, current)
        well_gj = _load_well_geoms(conn)
    finally:
        conn.close()

    # 2) 应用整批替换：只允许替换当前版本中存在的宗地
    reasons: list[dict] = []
    region_geom, _ = parse_polygon(region_gj, label="区域")
    old_geoms = {}
    for pid, gj in old_parcel_gj.items():
        geom, _ = parse_polygon(gj, parcel_id=pid, label=f"宗地 {pid}")
        old_geoms[pid] = geom

    new_geoms = dict(old_geoms)
    new_parcel_gj = dict(old_parcel_gj)
    seen: set[str] = set()
    for p in req.parcels:
        if p.id in seen:
            reasons.append({"code": "duplicate_parcel_id", "parcel_id": p.id,
                            "message": f"宗地编号 {p.id} 在请求中重复"})
            continue
        seen.add(p.id)
        if p.id not in old_parcel_gj:
            reasons.append({"code": "unknown_parcel_id", "parcel_id": p.id,
                            "message": f"宗地 {p.id} 在当前版本中不存在，"
                                       "只能整批替换已有宗地"})
            continue
        geom, errs = parse_polygon(p.geometry, parcel_id=p.id, label=f"宗地 {p.id}")
        reasons.extend(errs)
        if geom is not None:
            new_geoms[p.id] = geom
            new_parcel_gj[p.id] = p.geometry
    if reasons:
        return _validation_error(reasons)

    # 3) 相邻宗地联改按整批新边界核查
    tiling = validate_tiling(new_geoms, region_geom)
    if not tiling.ok:
        return _validation_error(
            tiling.reasons,
            gap_area=tiling.gap_area,
            overlaps=tiling.overlaps,
            overlap_parcel_ids=tiling.overlap_parcel_ids,
        )

    # 4) 依新版边界重算全部井归属
    well_geoms = {}
    for wid, gj in well_gj.items():
        geom, _ = parse_point(gj, well_id=wid, label=f"井 {wid}")
        well_geoms[wid] = geom
    attributions = attribute_wells(well_geoms, new_geoms)

    # 5) 乐观并发提交：同版并发修订只成功一份
    new_revision = current + 1
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if _current_revision(conn) != req.base_revision:
                conn.execute("ROLLBACK")
                return _err(409, {
                    "error": "revision_conflict",
                    "message": "提交期间已有其他修订发布，本次修订作废，请基于新版重试",
                    "current_revision": _current_revision(conn),
                    "base_revision": req.base_revision,
                })
            _insert_version(conn, new_revision, new_parcel_gj, attributions,
                            region=None, wells=None)
            cur = conn.execute(
                "UPDATE meta SET current_revision = ? WHERE id = 1 AND current_revision = ?",
                (new_revision, req.base_revision),
            )
            if cur.rowcount != 1:
                conn.execute("ROLLBACK")
                return _err(409, {"error": "revision_conflict",
                                  "message": "版本指针已被其他修订更新",
                                  "base_revision": req.base_revision})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    return {"revision": new_revision, "parcel_count": len(new_parcel_gj),
            "well_count": len(well_gj), "well_summary": _well_summary(attributions)}


# ---------------------------------------------------------------- 对齐发布（外业平面格网）

def _alignment_validation_error(reasons: list[dict], *,
                                tiling=None) -> JSONResponse:
    return _err(422, {
        "error": "alignment_validation_failed",
        "message": "对齐副本未通过核查，原有对齐副本继续供数",
        "gap_area": tiling.gap_area if tiling else None,
        "overlaps": tiling.overlaps if tiling else [],
        "overlap_parcel_ids": tiling.overlap_parcel_ids if tiling else [],
        "reasons": reasons,
    })


def _save_alignment(conn: sqlite3.Connection, req: AlignRequest,
                    control_points: list[dict], region: dict,
                    parcels: dict[str, dict], wells: dict[str, dict],
                    attributions: dict[str, dict]) -> None:
    """在已开启的事务内整体替换对齐副本（先删后插，同事务原子生效）。"""
    conn.execute("DELETE FROM alignment_wells")
    conn.execute("DELETE FROM alignment_parcels")
    conn.execute("DELETE FROM alignment_meta")
    conn.execute(
        "INSERT INTO alignment_meta "
        "(id, source_revision, control_points, grid_spacing, created_at, region) "
        "VALUES (1, ?, ?, ?, ?, ?)",
        (req.base_revision, json.dumps(control_points), req.grid_spacing,
         _now(), json.dumps(region)),
    )
    conn.executemany(
        "INSERT INTO alignment_parcels (parcel_id, geojson) VALUES (?, ?)",
        [(pid, json.dumps(g)) for pid, g in parcels.items()],
    )
    conn.executemany(
        "INSERT INTO alignment_wells (well_id, geojson, status, parcel_id, candidates) "
        "VALUES (?, ?, ?, ?, ?)",
        [(wid, json.dumps(wells[wid]), a["status"], a["parcel_id"],
          json.dumps(a["candidates"])) for wid, a in attributions.items()],
    )


@app.post("/v1/alignment")
def publish_alignment(req: AlignRequest):
    """对齐发布：把当前版本经仿射变换 + 格点吸附转到外业平面格网。

    对齐副本须通过原有边界有效性与覆盖核查，且所有宗地对接触类型、
    每口井归属状态与候选宗地均与源版一致；任一不满足即整体拒绝，
    原有对齐副本继续供数。
    """
    # 1) 控制点：源侧/目标侧三点各自不共线
    src_pts = [tuple(cp.source) for cp in req.control_points]
    dst_pts = [tuple(cp.target) for cp in req.control_points]
    cp_reasons = [r for r in (collinear_reason(src_pts, "源侧"),
                              collinear_reason(dst_pts, "目标侧")) if r]
    if cp_reasons:
        return _err(422, {
            "error": "invalid_control_points",
            "message": "控制点无法建立仿射变换，原有对齐副本继续供数",
            "reasons": cp_reasons,
        })

    # 2) 读取 base 版本快照（版本数据不可变，按修订号读取即是一致快照）
    conn = connect()
    try:
        current = _current_revision(conn)
        if current is None:
            return _err(404, {"error": "not_initialized",
                              "message": "尚未首次提交，请先调用 POST /v1/init"})
        if req.base_revision != current:
            return _revision_conflict(
                current, req.base_revision,
                f"所见修订号 {req.base_revision} 已过期，当前为 {current}，"
                "请重新读取后再提交；原有对齐副本继续供数")
        region_gj = _load_region(conn)
        parcel_gj = _load_parcels(conn, current)
        well_gj = _load_well_geoms(conn)
        src_attributions = _load_attributions(conn, current)
    finally:
        conn.close()

    # 3) 仿射变换 + 格点吸附（写事务之外，期间源版可照常发布）
    matrix = solve_affine(src_pts, dst_pts)
    spacing = req.grid_spacing
    aligned_region_gj = transform_polygon_geojson(region_gj, matrix, spacing)
    aligned_parcel_gj = {pid: transform_polygon_geojson(gj, matrix, spacing)
                         for pid, gj in parcel_gj.items()}
    aligned_well_gj = {wid: transform_point_geojson(gj, matrix, spacing)
                       for wid, gj in well_gj.items()}

    # 4) 对齐副本核查：几何有效性（复用原有规则）
    reasons: list[dict] = []
    region_geom, errs = parse_polygon(aligned_region_gj, label="对齐区域")
    reasons.extend(errs)
    aligned_geoms: dict[str, Any] = {}
    for pid in sorted(aligned_parcel_gj):
        geom, errs = parse_polygon(aligned_parcel_gj[pid], parcel_id=pid,
                                   label=f"对齐宗地 {pid}")
        reasons.extend(errs)
        if geom is not None:
            aligned_geoms[pid] = geom
    aligned_well_geoms: dict[str, Any] = {}
    for wid in sorted(aligned_well_gj):
        geom, errs = parse_point(aligned_well_gj[wid], well_id=wid,
                                 label=f"对齐井 {wid}")
        reasons.extend(errs)
        if geom is not None:
            aligned_well_geoms[wid] = geom

    # 5) 覆盖核查（复用原有规则）
    tiling = None
    if region_geom is not None and len(aligned_geoms) == len(aligned_parcel_gj):
        tiling = validate_tiling(aligned_geoms, region_geom)
        reasons.extend(tiling.reasons)

    # 6) 接触类型一致性：逐宗地对与源版比较
    if len(aligned_geoms) == len(aligned_parcel_gj):
        src_geoms = {}
        for pid, gj in parcel_gj.items():
            geom, _ = parse_polygon(gj, parcel_id=pid, label=f"宗地 {pid}")
            src_geoms[pid] = geom
        reasons.extend(compare_contact_types(src_geoms, aligned_geoms))

    # 7) 井归属一致性：对齐副本重算，逐井与源版比较
    aligned_attributions = attribute_wells(aligned_well_geoms, aligned_geoms)
    if (len(aligned_geoms) == len(aligned_parcel_gj)
            and len(aligned_well_geoms) == len(aligned_well_gj)):
        reasons.extend(compare_attributions(src_attributions, aligned_attributions))

    if reasons:
        return _alignment_validation_error(reasons, tiling=tiling)

    # 8) 乐观并发提交：源修订已变更则整体作废，原有对齐副本继续供数
    control_points = [{"source": list(cp.source), "target": list(cp.target)}
                      for cp in req.control_points]
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if _current_revision(conn) != req.base_revision:
                conn.execute("ROLLBACK")
                return _revision_conflict(
                    _current_revision(conn), req.base_revision,
                    "提交对齐结果期间源修订已变更，本次对齐作废，"
                    "原有对齐副本继续供数")
            _save_alignment(conn, req, control_points, aligned_region_gj,
                            aligned_parcel_gj, aligned_well_gj,
                            aligned_attributions)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    return {"source_revision": req.base_revision,
            "grid_spacing": spacing,
            "parcel_count": len(aligned_parcel_gj),
            "well_count": len(aligned_well_gj),
            "well_summary": _well_summary(aligned_attributions)}


@app.get("/v1/alignment")
def get_alignment():
    """当前对齐副本：完整几何 + 所用修订号、控制点、格网间距（同一读事务取出）。"""
    conn = connect()
    try:
        conn.execute("BEGIN")
        try:
            meta = conn.execute(
                "SELECT source_revision, control_points, grid_spacing, created_at, "
                "region FROM alignment_meta WHERE id = 1"
            ).fetchone()
            if meta is None:
                conn.execute("ROLLBACK")
                return _err(404, {"error": "no_alignment",
                                  "message": "尚未发布对齐副本"})
            parcel_rows = conn.execute(
                "SELECT parcel_id, geojson FROM alignment_parcels ORDER BY parcel_id"
            ).fetchall()
            well_rows = conn.execute(
                "SELECT well_id, geojson, status, parcel_id, candidates "
                "FROM alignment_wells ORDER BY well_id"
            ).fetchall()
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    return {
        "source_revision": meta["source_revision"],
        "control_points": json.loads(meta["control_points"]),
        "grid_spacing": meta["grid_spacing"],
        "created_at": meta["created_at"],
        "region": json.loads(meta["region"]),
        "parcels": [{"id": r["parcel_id"], "geometry": json.loads(r["geojson"])}
                    for r in parcel_rows],
        "wells": [{"id": r["well_id"], "geometry": json.loads(r["geojson"]),
                   "status": r["status"], "parcel_id": r["parcel_id"],
                   "candidates": json.loads(r["candidates"])}
                  for r in well_rows],
    }


# ---------------------------------------------------------------- 查询

def _wells_payload(well_gj: dict[str, dict], attributions: dict[str, dict]) -> list[dict]:
    return [
        {"id": wid, "geometry": well_gj[wid], **attributions[wid]}
        for wid in sorted(well_gj)
    ]


@app.get("/v1/current")
def get_current():
    """当前完整版本：区域 + 边界 + 井归属，同一读事务取出，保证同版一致。"""
    conn = connect()
    try:
        conn.execute("BEGIN")
        try:
            revision = _current_revision(conn)
            if revision is None:
                conn.execute("ROLLBACK")
                return _err(404, {"error": "not_initialized",
                                  "message": "尚未首次提交"})
            region = _load_region(conn)
            parcels = _load_parcels(conn, revision)
            well_gj = _load_well_geoms(conn)
            attributions = _load_attributions(conn, revision)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    return {
        "revision": revision,
        "region": region,
        "parcels": [{"id": pid, "geometry": parcels[pid]} for pid in sorted(parcels)],
        "wells": _wells_payload(well_gj, attributions),
    }


@app.get("/v1/parcels")
def get_parcels():
    """当前宗地边界（响应携带所属修订号）。"""
    conn = connect()
    try:
        revision = _current_revision(conn)
        if revision is None:
            return _err(404, {"error": "not_initialized", "message": "尚未首次提交"})
        parcels = _load_parcels(conn, revision)
    finally:
        conn.close()
    return {"revision": revision,
            "parcels": [{"id": pid, "geometry": parcels[pid]} for pid in sorted(parcels)]}


@app.get("/v1/wells")
def get_wells():
    """当前井归属（响应携带所属修订号，与 /v1/parcels 对号即可验证同版）。"""
    conn = connect()
    try:
        revision = _current_revision(conn)
        if revision is None:
            return _err(404, {"error": "not_initialized", "message": "尚未首次提交"})
        well_gj = _load_well_geoms(conn)
        attributions = _load_attributions(conn, revision)
    finally:
        conn.close()
    return {"revision": revision, "wells": _wells_payload(well_gj, attributions)}


@app.get("/v1/revisions")
def list_revisions():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT revision, created_at, parcel_count, well_count "
            "FROM versions ORDER BY revision"
        ).fetchall()
    finally:
        conn.close()
    return {"revisions": [dict(r) for r in rows]}


@app.get("/v1/revisions/{revision}")
def get_revision(revision: int):
    """读取指定历史版本（含被替换前的旧版）。"""
    conn = connect()
    try:
        conn.execute("BEGIN")
        try:
            row = conn.execute(
                "SELECT revision, created_at, parcel_count, well_count "
                "FROM versions WHERE revision = ?", (revision,),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return _err(404, {"error": "revision_not_found",
                                  "message": f"版本 {revision} 不存在"})
            region = _load_region(conn)
            parcels = _load_parcels(conn, revision)
            well_gj = _load_well_geoms(conn)
            attributions = _load_attributions(conn, revision)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    return {
        "revision": row[0],
        "created_at": row[1],
        "region": region,
        "parcels": [{"id": pid, "geometry": parcels[pid]} for pid in sorted(parcels)],
        "wells": _wells_payload(well_gj, attributions),
    }


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
