"""校准工位预约路由。

* ``GET  /api/calibration-slots``               只读：单工位最早可用时段；
* ``POST /api/calibration-bookings``            写事务内重查占用与证书截止后确认（409 冲突）；
* ``POST /api/calibration-bookings/reschedule`` 2～10 条预约批量改期（同一写事务，冲突 409）；
* ``GET  /api/calibration-bookings``            按仪器（可选）查询已确认预约。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlite3 import Connection

from .. import booking, crud
from ..database import get_conn_ctx, get_db
from ..schemas import (
    CalibrationBookingIn,
    CalibrationBookingOut,
    CalibrationBookingRescheduleIn,
    CalibrationSlotsOut,
)

router = APIRouter(prefix="/api", tags=["calibration-booking"])


@router.get(
    "/calibration-slots",
    response_model=CalibrationSlotsOut,
    summary="查询单个校准工位时间窗内最早可用时段",
)
def calibration_slots(
    instrument: str = Query(
        ..., description="受校准仪器编码（溯源链按查询窗起点判定）"
    ),
    start: datetime = Query(
        ...,
        alias="start",
        description="UTC 查询窗起点（含），如 2026-06-20T00:00:00Z",
    ),
    end: datetime = Query(
        ...,
        alias="end",
        description="UTC 查询窗终点，如 2026-06-21T00:00:00Z",
    ),
    duration_seconds: int = Query(
        ...,
        alias="duration_seconds",
        gt=0,
        description="所需校准时长（正整数秒）",
    ),
    db: Connection = Depends(get_db),
):
    """输入仪器、UTC 起止时间与所需时长，返回时间窗内**最早可用时段**。

    计算规则：

    1. 全实验室仅**单个校准工位**，所有仪器共用一份占用；占用按
       **半开区间** ``[start_at, end_at)`` 计算，相接但不重叠的预约
       （如 ``[09,10)`` 与 ``[10,11)``）互不冲突；
    2. 在**查询起点**要求该仪器存在**唯一且校验通过**的溯源链，预约
       必须在其**起点证书到期前结束**（``end <= valid_until``），
       搜索窗右端据此裁剪；链不可用（无有效证书/起点不唯一/多候选/
       断裂/校验失败）时 ``slot=null`` 并在 ``reason`` 中说明原因，
       完整证据见 ``trace_report``；
    3. 在裁剪后的搜索窗内按占用段贪心求最早可容纳所需时长的空档。
    """
    try:
        return booking.find_slots(db, instrument, start, end, duration_seconds)
    except KeyError:
        raise HTTPException(404, f"仪器不存在: {instrument}")
    except crud.ValidationError as exc:
        raise HTTPException(422, str(exc))


@router.post(
    "/calibration-bookings",
    response_model=CalibrationBookingOut,
    status_code=201,
    summary="确认校准工位预约（同一写事务内重查，冲突 409）",
)
def create_calibration_booking(body: CalibrationBookingIn):
    """确认预约：在**同一写事务**（``BEGIN IMMEDIATE``）内重新检查：

    1. 仪器存在；
    2. 预约起点该仪器的唯一有效溯源链仍成立（重查，不沿用 GET 结果）；
    3. 预约结束（``start + duration_seconds``，半开）不晚于起点证书
       ``valid_until``；
    4. 单个工位在该半开区间内无其他预约（``b.start < end AND b.end > start``）。

    任一条件不成立返回 **409**，响应体带机器可读 ``code``、中文 ``message``
    与结构化 ``detail``（占用冲突时给出全部冲突预约），事务回滚、绝不写入。
    全部成立则在同一事务内原子写入 SQLite 并返回已确认预约。
    """
    try:
        with get_conn_ctx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return booking.confirm_booking(
                conn,
                body.instrument_code,
                body.start,
                body.duration_seconds,
                body.note,
            )
    except booking.BookingConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message, "detail": exc.detail},
        )
    except crud.ValidationError as exc:
        raise HTTPException(422, str(exc))


@router.get(
    "/calibration-bookings",
    response_model=list[CalibrationBookingOut],
    summary="查询校准工位预约（可按仪器筛选）",
)
def list_calibration_bookings(
    instrument: Optional[str] = Query(
        None, description="仅返回该仪器的预约；留空返回全部预约（按起点升序）"
    ),
    db: Connection = Depends(get_db),
):
    if instrument is not None and crud.get_instrument(db, instrument) is None:
        raise HTTPException(404, f"仪器不存在: {instrument}")
    return booking.list_bookings(db, instrument)


@router.post(
    "/calibration-bookings/reschedule",
    response_model=list[CalibrationBookingOut],
    status_code=200,
    summary="批量改期（2～10 条；同一写事务内核验，冲突 409，整体回滚）",
)
def reschedule_calibration_bookings(body: CalibrationBookingRescheduleIn):
    """对 2～10 条**已有预约**批量改期：每条给新起点与可选新时长
    （留空沿用原时长），结束时间由服务端按 ``start + duration_seconds``
    计算；**仪器与备注沿用原预约，不随请求修改**。

    在**同一写事务**（``BEGIN IMMEDIATE``）内完成全部核验：

    1. 请求中的预约 ID 互不重复、且每条预约在库中存在；
    2. 对每条预约的**新起点**重新验证该仪器唯一有效溯源链（不沿用旧结果），
       且改期后结束不晚于新起点证书 ``valid_until``；
    3. 先把**本批预约的旧占用整体排除**，再按半开区间检查新时段：新时段
       **彼此之间**及与**本批之外其他预约**均不得重叠
       （``b.start < end AND b.end > start``）。因此两条预约**交换时段**、
       以及新时段与其他占用**首尾相接**（如 ``10:00`` 紧接 ``[09,10)``）
       都允许。

    任一条件不成立返回 **409**：顶层 ``detail.code`` 取请求顺序中第一条
    失败预约的原因码，``detail.failures`` 按请求顺序逐条给出
    ``booking_id`` / ``code``（``BOOKING_NOT_FOUND`` /
    ``BOOKING_ID_DUPLICATED`` / ``INSTRUMENT_NOT_FOUND`` /
    ``TRACEABILITY_CHAIN_UNAVAILABLE`` / ``BOOKING_ENDS_AFTER_CERT_EXPIRY`` /
    ``STATION_OCCUPIED``）/ 中文 ``message`` / 结构化 ``detail``，
    事务回滚、绝无部分更新。全部通过才在同一事务内更新时段与证书快照，
    并按请求顺序返回改期后的预约。
    """
    items = [
        (b.booking_id, b.start, b.duration_seconds) for b in body.bookings
    ]
    try:
        with get_conn_ctx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return booking.reschedule_bookings(conn, items)
    except booking.BookingConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message, "detail": exc.detail},
        )
    except crud.ValidationError as exc:
        raise HTTPException(422, str(exc))
