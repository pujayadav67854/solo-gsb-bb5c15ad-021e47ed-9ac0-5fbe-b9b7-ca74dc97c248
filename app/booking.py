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
* 批量改期（``reschedule_bookings``）：一次写事务内核对 2～10 条已有
  预约存在且 ID 不重复，将本批预约的旧占用整体排除后，再按半开区间
  检查新时段彼此及与其他预约的冲突（允许交换时段与首尾相接）；每条
  新起点重新验证唯一有效溯源链，新结束不得晚于起点证书到期；任一失败
  整体回滚，全部通过才更新时段与证书快照（仪器与备注保持不变）。
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

# 批量改期新增的冲突原因码（其余复用单条确认的四条）
CONFLICT_DUPLICATE_BOOKING_ID = "DUPLICATE_BOOKING_ID"
CONFLICT_BOOKING_NOT_FOUND = "BOOKING_NOT_FOUND"

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


# ---------------------------------------------------------------- 批量改期

def _get_booking_row(
    conn: sqlite3.Connection, booking_id: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM calibration_bookings WHERE id=?", (booking_id,)
    ).fetchone()


def _update_booking_schedule(
    conn: sqlite3.Connection,
    *,
    booking_id: str,
    start_at: str,
    end_at: str,
    duration_seconds: int,
    certificate_id: str,
    certificate_valid_until: str,
) -> dict[str, Any]:
    """更新预约时段与证书快照（供批量改期使用）；仪器、备注与创建时间不变。"""
    conn.execute(
        """
        UPDATE calibration_bookings
        SET start_at=:start_at, end_at=:end_at,
            duration_seconds=:duration_seconds,
            certificate_id=:certificate_id,
            certificate_valid_until=:certificate_valid_until
        WHERE id=:id
        """,
        {
            "id": booking_id,
            "start_at": start_at,
            "end_at": end_at,
            "duration_seconds": duration_seconds,
            "certificate_id": certificate_id,
            "certificate_valid_until": certificate_valid_until,
        },
    )
    row = _get_booking_row(conn, booking_id)
    return _booking_dict(row)


def reschedule_bookings(
    conn: sqlite3.Connection,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """批量改期：在**同一写事务**内核验并更新 2～10 条已有预约的时段。

    ``items`` 按请求顺序给出
    ``{"booking_id": str, "start": datetime,
      "duration_seconds": Optional[int]}``；
    ``duration_seconds`` 为 ``None`` 时沿用该预约原时长。调用方必须提供
    已执行 ``BEGIN IMMEDIATE`` 的连接；本函数只做检查与更新，
    COMMIT/ROLLBACK 由调用方（路由层上下文）负责。

    核验顺序（任一失败即抛 :class:`BookingConflict`，事务整体回滚）：

    1. 批次核验：预约 ID 不重复、每条预约均存在；
    2. 逐条核验：仪器仍存在、**新起点**该仪器唯一有效溯源链仍成立
       （重新验证，不沿用旧证书快照）、新结束不晚于起点证书
       ``valid_until``；
    3. 占用核验：本批预约的旧占用**整体排除**后，按半开区间检查
       新时段（a）与其他预约、（b）彼此两两的冲突——因此允许本批
       预约交换时段，也允许首尾相接；
    4. 全部通过才逐条 UPDATE 时段与证书快照（仪器/备注/创建时间不变），
       按请求顺序返回更新后的预约。
    """
    # 1) 批次核验：ID 不重复、预约存在
    seen: set[str] = set()
    originals: dict[str, sqlite3.Row] = {}
    for item in items:
        bid = item["booking_id"]
        if bid in seen:
            raise BookingConflict(
                CONFLICT_DUPLICATE_BOOKING_ID,
                f"预约 ID 重复: {bid}（批量改期要求每条预约 ID 互不重复）",
                {"booking_id": bid},
            )
        seen.add(bid)
        row = _get_booking_row(conn, bid)
        if row is None:
            raise BookingConflict(
                CONFLICT_BOOKING_NOT_FOUND,
                f"预约不存在: {bid}",
                {"booking_id": bid},
            )
        originals[bid] = row

    # 2) 逐条计算新时段，重查仪器、溯源链与证书截止
    targets: list[dict[str, Any]] = []
    for item in items:
        bid = item["booking_id"]
        row = originals[bid]
        instrument_code = row["instrument_code"]
        duration = item["duration_seconds"]
        if duration is None:
            duration = row["duration_seconds"]
        start_naive = to_utc_naive(item["start"])
        end_naive = start_naive + timedelta(seconds=duration)
        start_s, end_s = fmt(start_naive), fmt(end_naive)

        # 仪器仍存在（所属仪器以库内现值为准，改期不更换仪器）
        if get_instrument(conn, instrument_code) is None:
            raise BookingConflict(
                CONFLICT_INSTRUMENT_NOT_FOUND,
                f"预约 {bid} 所属仪器不存在: {instrument_code}",
                {"booking_id": bid, "instrument_code": instrument_code},
            )

        # 新起点该仪器唯一有效链仍成立（重新验证，不沿用旧快照）
        gate = unique_chain_gate(conn, instrument_code, start_naive)
        if not gate["ok"]:
            raise BookingConflict(
                CONFLICT_CHAIN_UNAVAILABLE,
                f"预约 {bid} 新起点 {start_s} 仪器 {instrument_code} 的"
                "唯一有效溯源链不可用：" + gate["reason"],
                {
                    "booking_id": bid,
                    "reason_code": gate["reason_code"],
                    "trace_report": gate["report"],
                },
            )

        cert_id = gate["start_certificate_id"]
        deadline_s = gate["certificate_valid_until"]

        # 新结束不得晚于起点证书到期（半开：end == valid_until 允许）
        if end_s > deadline_s:
            raise BookingConflict(
                CONFLICT_ENDS_AFTER_CERT_EXPIRY,
                f"预约 {bid} 新结束 {end_s} 晚于仪器 {instrument_code} "
                f"起点证书 {cert_id} 的到期时刻 {deadline_s}，"
                "预约必须在证书到期前结束",
                {
                    "booking_id": bid,
                    "certificate_id": cert_id,
                    "certificate_valid_until": deadline_s,
                    "booking_start": start_s,
                    "booking_end": end_s,
                },
            )
        targets.append(
            {
                "booking_id": bid,
                "start_s": start_s,
                "end_s": end_s,
                "duration": duration,
                "certificate_id": cert_id,
                "certificate_valid_until": deadline_s,
            }
        )

    # 3) 占用核验：本批旧占用整体排除后，检查新时段与其他预约及彼此
    batch_ids = set(originals)
    for t in targets:
        clashes = [
            b for b in find_clashes(conn, t["start_s"], t["end_s"])
            if b["id"] not in batch_ids
        ]
        if clashes:
            raise BookingConflict(
                CONFLICT_STATION_OCCUPIED,
                f"预约 {t['booking_id']} 新时段 {t['start_s']} ~ {t['end_s']}"
                f"（半开区间）与单工位上 {len(clashes)} 条其他预约冲突",
                {"booking_id": t["booking_id"], "conflicting_bookings": clashes},
            )
    for i in range(len(targets)):
        for j in range(i + 1, len(targets)):
            a, b = targets[i], targets[j]
            # 半开重叠判据：a.start < b.end AND b.start < a.end（首尾相接不冲突）
            if a["start_s"] < b["end_s"] and b["start_s"] < a["end_s"]:
                raise BookingConflict(
                    CONFLICT_STATION_OCCUPIED,
                    f"批量改期后的新时段彼此冲突：预约 {b['booking_id']} "
                    f"[{b['start_s']}, {b['end_s']}) 与预约 {a['booking_id']} "
                    f"[{a['start_s']}, {a['end_s']}) 在半开区间下重叠",
                    {
                        "booking_id": b["booking_id"],
                        "conflicting_batch_bookings": [
                            {
                                "booking_id": a["booking_id"],
                                "start_at": a["start_s"],
                                "end_at": a["end_s"],
                            },
                            {
                                "booking_id": b["booking_id"],
                                "start_at": b["start_s"],
                                "end_at": b["end_s"],
                            },
                        ],
                    },
                )

    # 4) 全部通过：逐条更新时段与证书快照（仪器/备注/创建时间不变）
    return [
        _update_booking_schedule(
            conn,
            booking_id=t["booking_id"],
            start_at=t["start_s"],
            end_at=t["end_s"],
            duration_seconds=t["duration"],
            certificate_id=t["certificate_id"],
            certificate_valid_until=t["certificate_valid_until"],
        )
        for t in targets
    ]
