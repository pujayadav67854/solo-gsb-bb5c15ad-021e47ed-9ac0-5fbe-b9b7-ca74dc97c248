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
* ``POST /reschedule`` 批量改期：一次写事务内核对 2～10 条预约存在且
  ID 不重复，先把本批预约的旧占用整体排除，再按半开区间检查新时段彼此
  及与其他预约的冲突（允许交换时段、允许首尾相接），并对每条新起点
  重新验证唯一有效溯源链与证书截止；任一失败 409 回滚，全部通过才
  更新时段与证书快照（仪器、备注保持不变）。
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

# POST /reschedule 批量改期冲突原因码
CONFLICT_BOOKING_NOT_FOUND = "BOOKING_NOT_FOUND"
CONFLICT_BOOKING_ID_DUPLICATED = "BOOKING_ID_DUPLICATED"

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

def _reschedule_failure(
    booking_id: str, code: str, message: str, detail: Optional[dict] = None
) -> dict[str, Any]:
    """构造批量改期 409 响应中的单条预约失败项。"""
    return {
        "booking_id": booking_id,
        "code": code,
        "message": message,
        "detail": detail or {},
    }


def reschedule_bookings(
    conn: sqlite3.Connection,
    items: list[tuple[str, datetime, Optional[int]]],
) -> list[dict[str, Any]]:
    """在**同一写事务**内对 2～10 条已有预约做批量改期。

    入参 ``items`` 为 ``(booking_id, new_start, new_duration_seconds)`` 三元组，
    ``new_duration_seconds`` 为 None 时沿用该预约原时长。调用方必须提供已执行
    ``BEGIN IMMEDIATE`` 的连接；COMMIT/ROLLBACK 由调用方负责。

    检查顺序（任一阶段产生失败项即抛 :class:`BookingConflict`，码
    ``STATION_OCCUPIED``，``detail.failures`` 逐条给出预约 ID 与原因码，
    事务回滚、绝不更新）：

    1. 请求内预约 ID 不重复（写事务内核对，区别于请求体层 422 的条数校验）；
    2. 每条预约在库中存在；
    3. 对每条预约（保留原仪器、原备注）按新起点重新验证唯一有效溯源链
       （仪器仍存在 / 链唯一且通过校验 / 结束不晚于新起点证书到期）；
    4. 把**本批预约的旧占用整体排除**后，新时段按半开区间两两互不重叠，
       且均不与本批之外的其他预约重叠——因此交换两条预约的时段、以及
       新时段与旧占用首尾相接都允许。

    全部通过才在同一事务内更新 start_at/end_at/duration_seconds 及证书
    快照（certificate_id/certificate_valid_until），按请求顺序返回更新后的
    预约；instrument_code/note/created_at 保持不变。
    """
    # 0) 规整入参：新时长缺省沿用原时长，正时长在函数层再兜底校验一次
    request_ids = [bid for bid, _, _ in items]
    parsed: list[tuple[str, datetime, Optional[int]]] = []
    for bid, start, duration in items:
        if duration is not None and duration <= 0:
            raise ValidationError("改期新时长 duration_seconds 必须为正整数秒")
        parsed.append((bid, to_utc_naive(start), duration))

    failures: list[dict[str, Any]] = []

    # 1) ID 不重复（按请求顺序记录首次出现的重复项）
    seen: set[str] = set()
    duplicate_ids: set[str] = set()
    for bid in request_ids:
        if bid in seen:
            duplicate_ids.add(bid)
            failures.append(
                _reschedule_failure(
                    bid,
                    CONFLICT_BOOKING_ID_DUPLICATED,
                    f"批量改期请求中预约 ID 重复出现: {bid}，每条预约至多改期一次",
                )
            )
        seen.add(bid)

    # 2) 预约存在性（重复 ID 不重复报缺失）
    rows_by_id: dict[str, sqlite3.Row] = {}
    for bid, _, _ in parsed:
        if bid in duplicate_ids:
            continue
        row = conn.execute(
            "SELECT * FROM calibration_bookings WHERE id=?", (bid,)
        ).fetchone()
        if row is None:
            failures.append(
                _reschedule_failure(
                    bid,
                    CONFLICT_BOOKING_NOT_FOUND,
                    f"预约不存在: {bid}",
                )
            )
        else:
            rows_by_id[bid] = row

    # 3) 逐条按新起点重新验证溯源门禁与证书截止（仅对有效条目）
    plans: list[dict[str, Any]] = []
    for bid, start_naive, new_duration in parsed:
        row = rows_by_id.get(bid)
        if row is None:
            continue
        instrument_code = row["instrument_code"]
        duration_seconds = (
            new_duration if new_duration is not None else row["duration_seconds"]
        )
        end_naive = start_naive + timedelta(seconds=duration_seconds)
        start_s, end_s = fmt(start_naive), fmt(end_naive)

        if get_instrument(conn, instrument_code) is None:
            failures.append(
                _reschedule_failure(
                    bid,
                    CONFLICT_INSTRUMENT_NOT_FOUND,
                    f"预约 {bid} 所属仪器已不存在: {instrument_code}",
                    {"instrument_code": instrument_code},
                )
            )
            continue

        gate = unique_chain_gate(conn, instrument_code, start_naive)
        if not gate["ok"]:
            failures.append(
                _reschedule_failure(
                    bid,
                    CONFLICT_CHAIN_UNAVAILABLE,
                    f"改期新起点 {start_s} 仪器 {instrument_code} 的唯一有效溯源链"
                    "不可用：" + gate["reason"],
                    {
                        "instrument_code": instrument_code,
                        "reason_code": gate["reason_code"],
                        "trace_report": gate["report"],
                    },
                )
            )
            continue

        cert_id = gate["start_certificate_id"]
        deadline_s = gate["certificate_valid_until"]
        if end_s > deadline_s:
            failures.append(
                _reschedule_failure(
                    bid,
                    CONFLICT_ENDS_AFTER_CERT_EXPIRY,
                    f"改期后预约结束 {end_s} 晚于仪器 {instrument_code} 新起点证书 "
                    f"{cert_id} 的到期时刻 {deadline_s}，预约必须在证书到期前结束",
                    {
                        "instrument_code": instrument_code,
                        "certificate_id": cert_id,
                        "certificate_valid_until": deadline_s,
                        "booking_start": start_s,
                        "booking_end": end_s,
                    },
                )
            )
            continue

        plans.append(
            {
                "booking_id": bid,
                "row": row,
                "instrument_code": instrument_code,
                "start_s": start_s,
                "end_s": end_s,
                "duration_seconds": duration_seconds,
                "certificate_id": cert_id,
                "certificate_valid_until": deadline_s,
            }
        )

    # 4) 半开占用重查：本批旧占用（ID 集合）整体排除，仅看本批之外的预约
    batch_id_set = set(rows_by_id) | duplicate_ids
    plan_by_id = {p["booking_id"]: p for p in plans}
    occupied: dict[str, list[dict[str, Any]]] = {p["booking_id"]: [] for p in plans}
    for p in plans:
        # 分批 ID 数量不定（2~10），用参数占位符展开；缺失/重复 ID 一并排除
        placeholders = ",".join("?" for _ in batch_id_set) or "''"
        external_rows = conn.execute(
            f"SELECT * FROM calibration_bookings "
            f"WHERE start_at < ? AND end_at > ? AND id NOT IN ({placeholders}) "
            f"ORDER BY start_at, id",
            (p["end_s"], p["start_s"], *sorted(batch_id_set)),
        ).fetchall()
        if external_rows:
            occupied[p["booking_id"]] = [_booking_dict(r) for r in external_rows]

    # 4b) 新时段彼此之间也按半开区间检查（允许交换、允许首尾相接）
    sibling_conflicts: dict[str, list[dict[str, Any]]] = {
        p["booking_id"]: [] for p in plans
    }
    for i, pa in enumerate(plans):
        for pb in plans[i + 1 :]:
            # 半开重叠：a.start < b.end AND b.start < a.end
            if pa["start_s"] < pb["end_s"] and pb["start_s"] < pa["end_s"]:
                sibling_conflicts[pa["booking_id"]].append(
                    {
                        "booking_id": pb["booking_id"],
                        "instrument_code": pb["instrument_code"],
                        "start_at": pb["start_s"],
                        "end_at": pb["end_s"],
                    }
                )
                sibling_conflicts[pb["booking_id"]].append(
                    {
                        "booking_id": pa["booking_id"],
                        "instrument_code": pa["instrument_code"],
                        "start_at": pa["start_s"],
                        "end_at": pa["end_s"],
                    }
                )

    for p in plans:
        ext = occupied[p["booking_id"]]
        sibs = sibling_conflicts[p["booking_id"]]
        if ext or sibs:
            failures.append(
                _reschedule_failure(
                    p["booking_id"],
                    CONFLICT_STATION_OCCUPIED,
                    f"改期后时段 {p['start_s']} ~ {p['end_s']}（半开区间）与"
                    f" {len(ext)} 条本批之外预约、{len(sibs)} 条同批改期预约冲突",
                    {
                        "instrument_code": p["instrument_code"],
                        "new_start": p["start_s"],
                        "new_end": p["end_s"],
                        "conflicting_bookings": ext,
                        "conflicting_rescheduled_bookings": sibs,
                    },
                )
            )

    if failures:
        # 去重（同一预约只保留首个失败项）并按请求顺序排列
        ordered: list[dict[str, Any]] = []
        reported: set[str] = set()
        for bid in request_ids:
            if bid in reported:
                continue
            entry = next((x for x in failures if x["booking_id"] == bid), None)
            if entry is None:
                continue  # 该预约通过全部核验
            ordered.append(entry)
            reported.add(bid)
        raise BookingConflict(
            ordered[0]["code"],
            f"批量改期 {len(ordered)} 条预约未通过写事务内核验，已整体回滚",
            {"failure_count": len(ordered), "failures": ordered},
        )

    # 5) 全部通过：在同一事务内更新时段与证书快照；仪器/备注/created_at 不变
    results: list[dict[str, Any]] = []
    for bid, _, _ in parsed:
        p = plan_by_id[bid]
        conn.execute(
            """
            UPDATE calibration_bookings
               SET start_at=:start_at,
                   end_at=:end_at,
                   duration_seconds=:duration_seconds,
                   certificate_id=:certificate_id,
                   certificate_valid_until=:certificate_valid_until
             WHERE id=:id
            """,
            {
                "id": bid,
                "start_at": p["start_s"],
                "end_at": p["end_s"],
                "duration_seconds": p["duration_seconds"],
                "certificate_id": p["certificate_id"],
                "certificate_valid_until": p["certificate_valid_until"],
            },
        )
    for bid, _, _ in parsed:
        row = conn.execute(
            "SELECT * FROM calibration_bookings WHERE id=?", (bid,)
        ).fetchone()
        results.append(_booking_dict(row))
    return results
