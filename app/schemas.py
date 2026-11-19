"""请求体的 Pydantic 模型（结构校验；几何语义校验在 geo.py 中做）。"""
from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, Field, field_validator

MAX_PARCELS = 40
MAX_WELLS = 100


def _coerce_id(v: Any) -> Any:
    """编号允许字符串或整数，统一归一成字符串。"""
    if isinstance(v, bool):
        raise ValueError("编号必须是非空字符串或整数")
    if isinstance(v, int):
        return str(v)
    return v


class ParcelIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    geometry: dict[str, Any]

    _coerce = field_validator("id", mode="before")(_coerce_id)


class WellIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    geometry: dict[str, Any]

    _coerce = field_validator("id", mode="before")(_coerce_id)


class InitRequest(BaseModel):
    """首次提交：区域 + 全部宗地 + 全部井。"""
    region: dict[str, Any]
    parcels: list[ParcelIn] = Field(min_length=1, max_length=MAX_PARCELS)
    wells: list[WellIn] = Field(default_factory=list, max_length=MAX_WELLS)


class ReplaceRequest(BaseModel):
    """整批替换指定宗地：携带所见修订号 + 这些宗地的完整新边界。"""
    base_revision: int = Field(ge=1)
    parcels: list[ParcelIn] = Field(min_length=1, max_length=MAX_PARCELS)


def _check_position_pair(v: Any) -> Any:
    """控制点坐标：必须是 [x, y] 有限数值二元组，统一归一成浮点。"""
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        raise ValueError("控制点坐标必须是 [x, y] 二元组")
    x, y = v
    for c in (x, y):
        if isinstance(c, bool) or not isinstance(c, (int, float)) or not math.isfinite(c):
            raise ValueError("控制点坐标必须是有限数值")
    return [float(x), float(y)]


class ControlPointPair(BaseModel):
    """一对控制点：源平面坐标 + 目标平面坐标。"""
    source: list[Any]
    target: list[Any]

    _src = field_validator("source", mode="before")(_check_position_pair)
    _dst = field_validator("target", mode="before")(_check_position_pair)


class AlignRequest(BaseModel):
    """对齐发布：所见修订号 + 三对控制点 + 正整数格网间距。"""
    base_revision: int = Field(ge=1)
    control_points: list[ControlPointPair] = Field(min_length=3, max_length=3)
    grid_spacing: int = Field(gt=0)
