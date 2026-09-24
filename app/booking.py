"""校准工位预约：单工位半开区间占用与预约确认。

业务规则
--------
* 全实验室只有**单个校准工位**：所有仪器的预约共用同一份占用表，
  占用一律按半开区间 ``[start_at, end_at)`` 计算——相接但不重叠的两段
  （如 ``[09:00,10:00)`` 与 ``[10:00,11:00)``）不冲突，可首尾相接预约。
* 预约必须在查询起点（GET 用窗口起点、POST 用预约起点）该仪器
  **唯一有效溯源链的起点证书到期前结束**，即
  ``end_at <= 起点证书 valid_until``。链不唯一 / 断裂 / 校验失败时
  复用溯源引擎给出可解释原因，绝不任取一条链。
* ``GET`` 只读：返回时间窗内（受证书截止裁剪后）最早可容纳所需时长的
  时段；``POST`` 在同一写事务内（``BEGIN IMMEDIATE``）重查占用与证书
  截止，冲突返回 409，随后原子写入 SQLite。
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from .crud import ValidationError, get_instrument
from .engine import unique_chain_gate
from .utils import fmt, to_utc_naive, utcnow_str

# 链不可用 / 无空档等原因码
REASON_CHAIN_UNAVAILABLE = "TRACEABILITY_CHAIN_UNAVAILABLE"
REASON_WINDOW_TOO_SHORT = "WINDOW_TOO_SHORT_FOR_DURATION"
REASON_NO_FREE_SLOT = "NO_FREE_SLOT_IN_WINDOW"
REASON_DEADLINE_TOO_EARLY = "CERTIFICATE_DEADLINE_TOO_EARLY"

# POST 写事务冲突原因码
CONFLICT_INSTRUMENT_NOT_FOUND = "INSTRUMENT_NOT_FOUND"
CONFLICT_CHAIN_UNAVAILABLE = "TRACEABILITY_CHAIN_UNAVAILABLE"
CONFLICT_ENDS_AFTER_CERT_EXPIRY = "BOOKING_ENDS_AFTER_CERT_EXPIRY"
CONFLICT_STATION_OCCUPIED = "STATION_OCCUPIED"

_BOOKING_FIELDS = (
    "id",
    "instrument_code",
    "start_at",
    "end_at",
    "duration_seconds",
    "certificate_id",
    "certificate_valid_until",
    "note",
    "created_at",
)


class BookingConflict(Exception):
    """POST 确认预约在写事务内重查失败（路由层映射为 HTTP 409）。"""

    def __init__(self, code: str, message: str, detail: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


# ---------------------------------------------------------------- 工具

def _parse(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")


def _booking_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in _BOOKING_FIELDS}


def insert_booking(
    conn: sqlite3.Connection,
    *,
    instrument_code: str,
    start_at: str,
    end_at: str,
    duration_seconds: int,
    certificate_id: str,
    certificate_valid_until: str,
    note: str = "",
    booking_id: Optional[str] = None,
    created_at: Optional[str] = None,
) -> dict[str, Any]:
    """底层写入（供确认事务与演示播种复用），不做任何占用/溯源校验。"""
    if booking_id is None:
        booking_id = "BK-" + datetime.utcnow().strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:12]
    conn.execute(
        """
        INSERT INTO calibration_bookings(
            id, instrument_code, start_at, end_at, duration_seconds,
            certificate_id, certificate_valid_until, note, created_at)
        VALUES (:id, :instrument_code, :start_at, :end_at, :duration_seconds,
                :certificate_id, :certificate_valid_until, :note, :created_at)
        """,
        {
            "id": booking_id,
            "instrument_code": instrument_code,
            "start_at": start_at,
            "end_at": end_at,
            "duration_seconds": duration_seconds,
            "certificate_id": certificate_id,
            "certificate_valid_until": certificate_valid_until,
            "note": note,
            "created_at": created_at or utcnow_str(),
        },
    )
    row = conn.execute(
        "SELECT * FROM calibration_bookings WHERE id=?", (booking_id,)
    ).fetchone()
    return _booking_dict(row)


def list_bookings(
    conn: sqlite3.Connection, instrument_code: Optional[str] = None
) -> list[dict[str, Any]]:
    if instrument_code is not None:
        rows = conn.execute(
            "SELECT * FROM calibration_bookings WHERE instrument_code=? "
            "ORDER BY start_at, id",
            (instrument_code,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM calibration_bookings ORDER BY start_at, id"
        ).fetchall()
    return [_booking_dict(r) for r in rows]


def _overlapping_bookings(
    conn: sqlite3.Connection, lo_s: str, hi_s: str
) -> list[sqlite3.Row]:
    """与半开查询窗 ``[lo, hi)`` 相交的既有预约（单工位，不分仪器）。

    半开重叠判据 ``b.start < hi AND b.end > lo``：恰在 ``lo`` 结束或
    恰在 ``hi`` 开始的预约不占窗口。
    """
    return conn.execute(
        "SELECT * FROM calibration_bookings WHERE start_at < ? AND end_at > ? "
        "ORDER BY start_at, id",
        (hi_s, lo_s),
    ).fetchall()


def find_clashes(
    conn: sqlite3.Connection, start_s: str, end_s: str
) -> list[dict[str, Any]]:
    """确认事务内的占用重查：与半开区间 ``[start_s, end_s)`` 重叠的预约。"""
    rows = conn.execute(
        "SELECT * FROM calibration_bookings WHERE start_at < ? AND end_at > ? "
        "ORDER BY start_at, id",
        (end_s, start_s),
    ).fetchall()
    return [_booking_dict(r) for r in rows]


# ---------------------------------------------------------------- 最早空档

def _earliest_slot(
    busy: list[tuple[datetime, datetime]],
    cursor: datetime,
    window_end: datetime,
    duration: timedelta,
) -> Optional[tuple[datetime, datetime]]:
    """在 ``[cursor, window_end]`` 内求最早能容纳 duration 的半开空档。

    贪心法：游标取窗口起点，依次与按开始时间排序的占用段比较，
    若 ``[cursor, cursor+duration]`` 不与当前段相交则即为答案，
    否则游标跳到当前段结束（半开，可与占用段首尾相接）。
    """
    for b_start, b_end in busy:
        if b_end <= cursor:
            continue  # 占用段完全在游标之前
        if cursor + duration <= b_start:
            break  # 段开始前放得下（半开：candidate_end == b_start 不冲突）
        if b_end > cursor:
            cursor = b_end  # 被该段占用，游标跳到其结束（可相接）
    candidate_end = cursor + duration
    if candidate_end <= window_end:
        return cursor, candidate_end
    return None


def find_slots(
    conn: sqlite3.Connection,
    instrument_code: str,
    start: datetime,
    end: datetime,
    duration_seconds: int,
) -> dict[str, Any]:
    """GET /api/calibration-slots 的只读计算。"""
    if duration_seconds <= 0:
        raise ValidationError("所需时长 duration_seconds 必须为正整数秒")
    start_naive = to_utc_naive(start)
    end_naive = to_utc_naive(end)
    if start_naive >= end_naive:
        raise ValidationError("查询窗起点 start 必须严格早于终点 end")
    duration = timedelta(seconds=duration_seconds)
    if duration > end_naive - start_naive:
        raise ValidationError(
            f"所需时长 {duration_seconds}s 超过查询窗长度 "
            f"{int((end_naive - start_naive).total_seconds())}s"
        )

    lo_s, hi_s = fmt(start_naive), fmt(end_naive)

    if get_instrument(conn, instrument_code) is None:
        raise KeyError(instrument_code)

    # 1) 查询起点该仪器必须存在唯一且校验通过的溯源链
    gate = unique_chain_gate(conn, instrument_code, start_naive)
    if not gate["ok"]:
        return {
            "instrument": instrument_code,
            "window_start": lo_s,
            "window_end": hi_s,
            "duration_seconds": duration_seconds,
            "traceability_at": gate["at"],
            "traceability_available": False,
            "reason_code": REASON_CHAIN_UNAVAILABLE,
            "reason": gate["reason"],
            "chain_reason_code": gate["reason_code"],
            "certificate_id": None,
            "certificate_valid_until": None,
            "search_window_start": lo_s,
            "search_window_end": hi_s,
            "slot": None,
            "busy": [],
            "trace_report": gate["report"],
        }

    cert_id = gate["start_certificate_id"]
    deadline_s = gate["certificate_valid_until"]
    deadline = _parse(deadline_s)

    # 2) 预约须在起点证书到期前（含到期瞬间）结束：裁剪搜索窗右端
    search_end = min(end_naive, deadline)
    search_end_s = fmt(search_end)
    if start_naive + duration > search_end:
        reason_code = (
            REASON_DEADLINE_TOO_EARLY
            if search_end_s < hi_s
            else REASON_WINDOW_TOO_SHORT
        )
        return {
            "instrument": instrument_code,
            "window_start": lo_s,
            "window_end": hi_s,
            "duration_seconds": duration_seconds,
            "traceability_at": gate["at"],
            "traceability_available": True,
            "reason_code": reason_code,
            "reason": (
                f"仪器 {instrument_code} 唯一有效溯源链的起点证书 {cert_id} "
                f"于 {deadline_s} 到期，预约（{duration_seconds}s）无法在到期前结束"
                if reason_code == REASON_DEADLINE_TOO_EARLY
                else f"查询窗内不存在可容纳 {duration_seconds}s 的时段"
            ),
            "chain_reason_code": None,
            "certificate_id": cert_id,
            "certificate_valid_until": deadline_s,
            "search_window_start": lo_s,
            "search_window_end": search_end_s,
            "slot": None,
            "busy": [],
            "trace_report": gate["report"],
        }

    # 3) 单工位占用（与半开搜索窗相交的既有预约），求最早空档
    rows = _overlapping_bookings(conn, lo_s, search_end_s)
    busy = [(_parse(r["start_at"]), _parse(r["end_at"])) for r in rows]
    found = _earliest_slot(busy, start_naive, search_end, duration)

    result: dict[str, Any] = {
        "instrument": instrument_code,
        "window_start": lo_s,
        "window_end": hi_s,
        "duration_seconds": duration_seconds,
        "traceability_at": gate["at"],
        "traceability_available": True,
        "chain_reason_code": None,
        "certificate_id": cert_id,
        "certificate_valid_until": deadline_s,
        "search_window_start": lo_s,
        "search_window_end": search_end_s,
        "busy": [
            {
                "booking_id": r["id"],
                "instrument_code": r["instrument_code"],
                "start_at": r["start_at"],
                "end_at": r["end_at"],
            }
            for r in rows
        ],
        "trace_report": gate["report"],
    }
    if found is None:
        result.update(
            reason_code=REASON_NO_FREE_SLOT,
            reason=(
                f"单个校准工位在 [{lo_s}, {search_end_s}) 内不存在可容纳 "
                f"{duration_seconds}s 的空档（占用按半开区间计算）"
            ),
            slot=None,
        )
    else:
        s_start, s_end = found
        result.update(
            reason_code=None,
            reason=None,
            slot={
                "start": fmt(s_start),
                "end": fmt(s_end),
                "ends_by_certificate_valid_until": fmt(s_end) <= deadline_s,
            },
        )
    return result


# ---------------------------------------------------------------- 预约确认

def confirm_booking(
    conn: sqlite3.Connection,
    instrument_code: str,
    start: datetime,
    duration_seconds: int,
    note: str = "",
) -> dict[str, Any]:
    """在**同一写事务**内重查占用与证书截止并写入预约。

    调用方必须提供已执行 ``BEGIN IMMEDIATE`` 的连接；本函数只做检查与
    插入，COMMIT/ROLLBACK 由调用方（路由层上下文）负责。任一条件不再
    成立即抛 :class:`BookingConflict`（409），事务回滚，绝不写入。
    """
    if duration_seconds <= 0:
        raise ValidationError("所需时长 duration_seconds 必须为正整数秒")
    start_naive = to_utc_naive(start)
    end_naive = start_naive + timedelta(seconds=duration_seconds)
    start_s, end_s = fmt(start_naive), fmt(end_naive)

    # 1) 仪器仍存在
    if get_instrument(conn, instrument_code) is None:
        raise BookingConflict(
            CONFLICT_INSTRUMENT_NOT_FOUND,
            f"仪器不存在: {instrument_code}",
            {"instrument_code": instrument_code},
        )

    # 2) 该预约起点仪器唯一有效链仍成立（重查链，不沿用 GET 时结果）
    gate = unique_chain_gate(conn, instrument_code, start_naive)
    if not gate["ok"]:
        raise BookingConflict(
            CONFLICT_CHAIN_UNAVAILABLE,
            f"预约起点 {start_s} 仪器 {instrument_code} 的唯一有效溯源链不可用："
            + gate["reason"],
            {
                "reason_code": gate["reason_code"],
                "trace_report": gate["report"],
            },
        )

    cert_id = gate["start_certificate_id"]
    deadline_s = gate["certificate_valid_until"]

    # 3) 预约结束不得晚于起点证书到期（半开：end == valid_until 允许）
    if end_s > deadline_s:
        raise BookingConflict(
            CONFLICT_ENDS_AFTER_CERT_EXPIRY,
            f"预约结束 {end_s} 晚于仪器 {instrument_code} 起点证书 {cert_id} "
            f"的到期时刻 {deadline_s}，预约必须在证书到期前结束",
            {
                "certificate_id": cert_id,
                "certificate_valid_until": deadline_s,
                "booking_start": start_s,
                "booking_end": end_s,
            },
        )

    # 4) 单工位占用重查：半开重叠 b.start < end AND b.end > start
    clashes = find_clashes(conn, start_s, end_s)
    if clashes:
        raise BookingConflict(
            CONFLICT_STATION_OCCUPIED,
            f"单个校准工位在 {start_s} ~ {end_s}（半开区间）已被 "
            f"{len(clashes)} 条预约占用，时段冲突",
            {"conflicting_bookings": clashes},
        )

    # 5) 全部条件成立：原子写入（与上述重查处于同一写事务）
    return insert_booking(
        conn,
        instrument_code=instrument_code,
        start_at=start_s,
        end_at=end_s,
        duration_seconds=duration_seconds,
        certificate_id=cert_id,
        certificate_valid_until=deadline_s,
        note=note,
    )
