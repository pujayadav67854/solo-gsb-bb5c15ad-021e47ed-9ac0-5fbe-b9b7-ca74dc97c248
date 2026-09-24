import os
import tempfile

import pytest
from fastapi.testclient import TestClient

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["TRACE_DB_PATH"] = _tmp.name
os.environ["SEED_DEMO"] = "0"

from app.database import get_conn_ctx, init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()


def _chain(client, *, parent_until="2030-01-01T00:00:00",
           leaf_until="2027-01-01T00:00:00",
           leaf_from="2026-01-01T00:00:00",
           issued_leaf="2026-01-02T00:00:00",
           code="W"):
    """提交 基准 G -> 参考 R -> 工作仪器 W 的唯一有效链。

    默认令上级证书有效期长于起点证书，从而在起点证书到期后链本身仍成立，
    可专门验证“预约必须在起点证书到期前结束”的截止规则。证书 id 以仪器
    编码为后缀，保证同一测试里可提交多台仪器而不互相 upsert 覆盖。
    """
    cg, cr, cw = f"CG-{code}", f"CR-{code}", f"CW-{code}"
    instruments = [
        {"code": f"G-{code}", "name": f"国家基准-{code}",
         "is_national_standard": True},
        {"code": f"R-{code}", "name": f"参考标准器-{code}"},
        {"code": code, "name": "工作仪器"},
    ]
    certs = [
        {"id": cg, "instrument_code": f"G-{code}", "cert_number": f"{code}-G1",
         "issued_at": "2024-01-01T00:00:00",
         "valid_from": "2024-01-01T00:00:00", "valid_until": "2031-01-01T00:00:00",
         "uncertainty": 0.0001, "unit": "mm"},
        {"id": cr, "instrument_code": f"R-{code}", "cert_number": f"{code}-R1",
         "issued_at": "2025-06-01T00:00:00",
         "valid_from": "2025-06-01T00:00:00", "valid_until": parent_until,
         "uncertainty": 0.001, "unit": "mm"},
        {"id": cw, "instrument_code": code, "cert_number": f"{code}-W1",
         "issued_at": issued_leaf,
         "valid_from": leaf_from, "valid_until": leaf_until,
         "uncertainty": 0.01, "unit": "mm"},
    ]
    edges = [
        {"child_cert_id": cw, "parent_cert_id": cr},
        {"child_cert_id": cr, "parent_cert_id": cg},
    ]
    r = client.post("/api/submissions",
                    json={"instruments": instruments, "certificates": certs, "edges": edges})
    assert r.status_code == 201, r.text


@pytest.fixture()
def client():
    with TestClient(app) as c:
        c.post("/api/admin/reset")
        yield c


# ---------------------------------------------------------------- GET slots

def test_slot_earliest_when_empty(client):
    _chain(client)
    r = client.get("/api/calibration-slots", params={
        "instrument": "W",
        "start": "2026-06-20T00:00:00Z",
        "end": "2026-06-21T00:00:00Z",
        "duration_seconds": 3600,
    })
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["traceability_available"] is True
    assert d["certificate_id"] == "CW-W"
    assert d["certificate_valid_until"] == "2027-01-01T00:00:00Z"
    # 工位空闲：最早时段即窗口起点
    assert d["slot"] == {
        "start": "2026-06-20T00:00:00Z",
        "end": "2026-06-20T01:00:00Z",
        "ends_by_certificate_valid_until": True,
    }
    assert d["busy"] == []
    assert d["reason_code"] is None
    # 附带完整溯源报告以便解释
    assert d["trace_report"]["resolution"] == "unique"


def test_slot_jumps_over_half_open_occupancy(client):
    _chain(client)
    # 占用 [09:00,10:00)
    assert client.post("/api/calibration-bookings", json={
        "instrument_code": "W",
        "start": "2026-06-20T09:00:00Z", "duration_seconds": 3600,
    }).status_code == 201
    r = client.get("/api/calibration-slots", params={
        "instrument": "W",
        "start": "2026-06-20T08:00:00Z",
        "end": "2026-06-20T12:00:00Z",
        "duration_seconds": 3600,
    })
    d = r.json()
    # [08,09) 只有 1 小时空档 -> 放得下，最早从 08:00 开始
    assert d["slot"]["start"] == "2026-06-20T08:00:00Z"
    assert d["slot"]["end"] == "2026-06-20T09:00:00Z"
    assert len(d["busy"]) == 1
    # 半开：[08,09) 与占用 [09,10) 不冲突（ends 恰在 starts）
    assert d["busy"][0]["start_at"] == "2026-06-20T09:00:00Z"


def test_slot_back_to_back_boundary_not_overlap(client):
    _chain(client)
    # 占满 [09,11)，从 09:00 查询 1 小时 -> 最早 11:00（可相接）
    assert client.post("/api/calibration-bookings", json={
        "instrument_code": "W",
        "start": "2026-06-20T09:00:00Z", "duration_seconds": 7200,
    }).status_code == 201
    r = client.get("/api/calibration-slots", params={
        "instrument": "W",
        "start": "2026-06-20T09:00:00Z",
        "end": "2026-06-20T12:00:00Z",
        "duration_seconds": 3600,
    })
    d = r.json()
    assert d["slot"]["start"] == "2026-06-20T11:00:00Z"
    assert d["slot"]["end"] == "2026-06-20T12:00:00Z"


def test_slot_no_free_returns_reason(client):
    _chain(client)
    # 占住 [08:30, 11:30)，窗口 09..12 求 2 小时 -> 剩余空档不足
    assert client.post("/api/calibration-bookings", json={
        "instrument_code": "W",
        "start": "2026-06-20T08:30:00Z", "duration_seconds": 3 * 3600,
    }).status_code == 201
    r = client.get("/api/calibration-slots", params={
        "instrument": "W",
        "start": "2026-06-20T09:00:00Z",
        "end": "2026-06-20T12:00:00Z",
        "duration_seconds": 7200,
    })
    d = r.json()
    assert d["slot"] is None
    assert d["reason_code"] == "NO_FREE_SLOT_IN_WINDOW"
    assert "单个校准工位" in d["reason"]


def test_slot_search_window_clipped_by_certificate_deadline(client):
    _chain(client, leaf_until="2026-06-20T12:00:00Z")
    # 查询窗远超起点证书到期时刻
    r = client.get("/api/calibration-slots", params={
        "instrument": "W",
        "start": "2026-06-20T00:00:00Z",
        "end": "2027-06-20T00:00:00Z",
        "duration_seconds": 3600,
    })
    d = r.json()
    assert d["search_window_end"] == "2026-06-20T12:00:00Z"
    assert d["slot"]["end"] == "2026-06-20T01:00:00Z"
    assert d["slot"]["ends_by_certificate_valid_until"] is True


def test_slot_deadline_too_early(client):
    _chain(client, leaf_until="2026-06-20T10:30:00Z")
    r = client.get("/api/calibration-slots", params={
        "instrument": "W",
        "start": "2026-06-20T10:00:00Z",
        "end": "2026-06-21T00:00:00Z",
        "duration_seconds": 3600,
    })
    d = r.json()
    assert d["slot"] is None
    assert d["reason_code"] == "CERTIFICATE_DEADLINE_TOO_EARLY"
    assert "到期" in d["reason"]
    assert d["search_window_end"] == "2026-06-20T10:30:00Z"


def test_slot_deadline_clips_otherwise_fitting_window(client):
    """时长本在原窗内放得下，但裁剪到证书截止后放不下 -> 归因为截止过早，
    而不是窗口过短。"""
    _chain(client, leaf_until="2026-06-20T10:30:00Z")
    r = client.get("/api/calibration-slots", params={
        "instrument": "W",
        "start": "2026-06-20T10:00:00Z",
        "end": "2026-06-21T00:00:00Z",   # 原窗 14 小时，足够 2 小时
        "duration_seconds": 7200,
    })
    d = r.json()
    assert d["slot"] is None
    assert d["reason_code"] == "CERTIFICATE_DEADLINE_TOO_EARLY"
    assert d["search_window_end"] == "2026-06-20T10:30:00Z"


def test_slot_exact_fit_to_deadline(client):
    _chain(client, leaf_until="2026-06-20T12:00:00Z")
    # [11:00,12:00) 恰在到期瞬间结束：半开，允许
    r = client.get("/api/calibration-slots", params={
        "instrument": "W",
        "start": "2026-06-20T11:00:00Z",
        "end": "2026-06-20T12:00:00Z",
        "duration_seconds": 3600,
    })
    d = r.json()
    assert d["slot"] == {
        "start": "2026-06-20T11:00:00Z",
        "end": "2026-06-20T12:00:00Z",
        "ends_by_certificate_valid_until": True,
    }


def test_slot_chain_unavailable_explains_reason(client):
    # 只有仪器与一张失效起点证书：无有效证书 -> 链不可开始
    client.post("/api/submissions", json={
        "instruments": [{"code": "W", "name": "工作仪器"}],
        "certificates": [
            {"id": "CW", "instrument_code": "W", "cert_number": "W-1",
             "issued_at": "2025-01-01T00:00:00",
             "valid_from": "2025-01-01T00:00:00",
             "valid_until": "2026-01-01T00:00:00",
             "uncertainty": 0.01, "unit": "mm"},
        ],
        "edges": [],
    })
    r = client.get("/api/calibration-slots", params={
        "instrument": "W",
        "start": "2026-06-20T00:00:00Z",
        "end": "2026-06-21T00:00:00Z",
        "duration_seconds": 3600,
    })
    d = r.json()
    assert d["traceability_available"] is False
    assert d["slot"] is None
    assert d["reason_code"] == "TRACEABILITY_CHAIN_UNAVAILABLE"
    assert d["chain_reason_code"] == "NO_ACTIVE_CERTIFICATE"
    assert "没有处于有效区间的校准证书" in d["reason"]
    assert d["trace_report"]["resolution"] == "broken"


def test_slot_ambiguous_chain_explains(client):
    # 同一起点挂两条都到基准的父链 -> 多候选
    client.post("/api/submissions", json={
        "instruments": [
            {"code": "G", "name": "基准", "is_national_standard": True},
            {"code": "RA", "name": "参考A"},
            {"code": "RB", "name": "参考B"},
            {"code": "W", "name": "工作仪器"},
        ],
        "certificates": [
            {"id": "CG", "instrument_code": "G", "cert_number": "G",
             "issued_at": "2024-01-01T00:00:00",
             "valid_from": "2024-01-01T00:00:00", "valid_until": "2031-01-01T00:00:00",
             "uncertainty": 0.0001, "unit": "mm"},
            {"id": "CRA", "instrument_code": "RA", "cert_number": "RA",
             "issued_at": "2025-01-01T00:00:00",
             "valid_from": "2025-01-01T00:00:00", "valid_until": "2030-01-01T00:00:00",
             "uncertainty": 0.001, "unit": "mm"},
            {"id": "CRB", "instrument_code": "RB", "cert_number": "RB",
             "issued_at": "2025-01-01T00:00:00",
             "valid_from": "2025-01-01T00:00:00", "valid_until": "2030-01-01T00:00:00",
             "uncertainty": 0.001, "unit": "mm"},
            {"id": "CW", "instrument_code": "W", "cert_number": "W",
             "issued_at": "2026-01-02T00:00:00",
             "valid_from": "2026-01-01T00:00:00", "valid_until": "2027-01-01T00:00:00",
             "uncertainty": 0.01, "unit": "mm"},
        ],
        "edges": [
            {"child_cert_id": "CW", "parent_cert_id": "CRA"},
            {"child_cert_id": "CW", "parent_cert_id": "CRB"},
            {"child_cert_id": "CRA", "parent_cert_id": "CG"},
            {"child_cert_id": "CRB", "parent_cert_id": "CG"},
        ],
    })
    r = client.get("/api/calibration-slots", params={
        "instrument": "W", "start": "2026-06-20T00:00:00Z",
        "end": "2026-06-21T00:00:00Z", "duration_seconds": 600,
    })
    d = r.json()
    assert d["traceability_available"] is False
    assert d["chain_reason_code"] == "MULTIPLE_VALID_CHAINS"
    assert d["trace_report"]["resolution"] == "ambiguous"


def test_slot_unknown_instrument_404(client):
    r = client.get("/api/calibration-slots", params={
        "instrument": "GHOST",
        "start": "2026-06-20T00:00:00Z", "end": "2026-06-21T00:00:00Z",
        "duration_seconds": 60,
    })
    assert r.status_code == 404


@pytest.mark.parametrize("params", [
    {"start": "2026-06-21T00:00:00Z", "end": "2026-06-20T00:00:00Z",
     "duration_seconds": 60},                       # 起晚于止
    {"start": "2026-06-20T00:00:00Z", "end": "2026-06-20T01:00:00Z",
     "duration_seconds": 7200},                     # 时长超过窗长
    {"start": "2026-06-20T00:00:00Z", "end": "2026-06-20T00:00:00Z",
     "duration_seconds": 60},                       # 空窗（start==end）
])
def test_slot_invalid_window_422(client, params):
    _chain(client)
    r = client.get("/api/calibration-slots",
                   params={"instrument": "W", **params})
    assert r.status_code == 422


def test_slot_nonpositive_duration_422(client):
    _chain(client)
    r = client.get("/api/calibration-slots", params={
        "instrument": "W", "start": "2026-06-20T00:00:00Z",
        "end": "2026-06-21T00:00:00Z", "duration_seconds": 0,
    })
    assert r.status_code == 422


def test_slot_station_shared_across_instruments(client):
    """单工位：另一台仪器的占用同样阻塞本仪器（占用不分仪器）。"""
    _chain(client, code="W")
    _chain(client, code="W2")
    assert client.post("/api/calibration-bookings", json={
        "instrument_code": "W2",
        "start": "2026-06-20T09:00:00Z", "duration_seconds": 3600,
    }).status_code == 201
    r = client.get("/api/calibration-slots", params={
        "instrument": "W", "start": "2026-06-20T09:00:00Z",
        "end": "2026-06-20T12:00:00Z", "duration_seconds": 3600,
    })
    d = r.json()
    # [09,10) 被另一台仪器占用 -> 最早 10:00
    assert d["slot"]["start"] == "2026-06-20T10:00:00Z"
    assert d["busy"][0]["instrument_code"] == "W2"


# ---------------------------------------------------------------- POST bookings

def test_booking_created_and_persisted(client):
    _chain(client)
    r = client.post("/api/calibration-bookings", json={
        "instrument_code": "W",
        "start": "2026-06-20T09:00:00Z",
        "duration_seconds": 5400,
        "note": "例行校准",
    })
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["instrument_code"] == "W"
    assert b["start_at"] == "2026-06-20T09:00:00Z"
    assert b["end_at"] == "2026-06-20T10:30:00Z"
    assert b["duration_seconds"] == 5400
    assert b["certificate_id"] == "CW-W"
    assert b["certificate_valid_until"] == "2027-01-01T00:00:00Z"
    assert b["note"] == "例行校准"
    assert b["id"]

    # 持久化到 SQLite：另起请求/连接可见
    with get_conn_ctx() as conn:
        row = conn.execute(
            "SELECT * FROM calibration_bookings WHERE id=?", (b["id"],)
        ).fetchone()
        assert row is not None
        assert row["instrument_code"] == "W"


def test_booking_overlap_returns_409_with_clashes(client):
    _chain(client)
    assert client.post("/api/calibration-bookings", json={
        "instrument_code": "W",
        "start": "2026-06-20T09:00:00Z", "duration_seconds": 3600,
    }).status_code == 201
    # 另一台仪器在同一时间预约单工位 -> 占用冲突 409
    _chain(client, code="W2")
    r = client.post("/api/calibration-bookings", json={
        "instrument_code": "W2",
        "start": "2026-06-20T09:30:00Z", "duration_seconds": 3600,
    })
    assert r.status_code == 409
    err = r.json()["detail"]
    assert err["code"] == "STATION_OCCUPIED"
    assert len(err["detail"]["conflicting_bookings"]) == 1
    assert err["detail"]["conflicting_bookings"][0]["start_at"] == \
        "2026-06-20T09:00:00Z"
    # 冲突预约未写入
    rl = client.get("/api/calibration-bookings")
    assert len(rl.json()) == 1


def test_booking_touching_ends_allowed(client):
    """半开：后一段 start 恰等于前一段 end，不冲突。"""
    _chain(client)
    assert client.post("/api/calibration-bookings", json={
        "instrument_code": "W",
        "start": "2026-06-20T09:00:00Z", "duration_seconds": 3600,
    }).status_code == 201
    r = client.post("/api/calibration-bookings", json={
        "instrument_code": "W",
        "start": "2026-06-20T10:00:00Z", "duration_seconds": 1800,
    })
    assert r.status_code == 201, r.text
    assert r.json()["end_at"] == "2026-06-20T10:30:00Z"


def test_booking_ends_after_certificate_expiry_409(client):
    # 上级证书远晚于起点证书：链成立，但预约越过起点证书截止
    _chain(client, leaf_until="2027-01-01T00:00:00Z")
    r = client.post("/api/calibration-bookings", json={
        "instrument_code": "W",
        "start": "2026-12-31T23:00:00Z",
        "duration_seconds": 7200,   # end 2027-01-01T01:00 > 截止 2027-01-01T00:00
    })
    assert r.status_code == 409
    err = r.json()["detail"]
    assert err["code"] == "BOOKING_ENDS_AFTER_CERT_EXPIRY"
    assert err["detail"]["certificate_valid_until"] == "2027-01-01T00:00:00Z"
    assert client.get("/api/calibration-bookings").json() == []


def test_booking_end_exactly_at_expiry_allowed(client):
    _chain(client, leaf_until="2027-01-01T00:00:00Z")
    r = client.post("/api/calibration-bookings", json={
        "instrument_code": "W",
        "start": "2026-12-31T23:00:00Z",
        "duration_seconds": 3600,   # end == valid_until
    })
    assert r.status_code == 201, r.text
    assert r.json()["end_at"] == "2027-01-01T00:00:00Z"


def test_booking_chain_broken_409(client):
    client.post("/api/submissions", json={
        "instruments": [{"code": "W", "name": "工作仪器"}],
        "certificates": [], "edges": [],
    })
    r = client.post("/api/calibration-bookings", json={
        "instrument_code": "W",
        "start": "2026-06-20T09:00:00Z", "duration_seconds": 600,
    })
    assert r.status_code == 409
    err = r.json()["detail"]
    assert err["code"] == "TRACEABILITY_CHAIN_UNAVAILABLE"
    assert err["detail"]["reason_code"] == "NO_ACTIVE_CERTIFICATE"
    # detail 内附完整溯源报告说明原因
    assert err["detail"]["trace_report"]["resolution"] == "broken"


def test_booking_unknown_instrument_409(client):
    r = client.post("/api/calibration-bookings", json={
        "instrument_code": "GHOST",
        "start": "2026-06-20T09:00:00Z", "duration_seconds": 60,
    })
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "INSTRUMENT_NOT_FOUND"


def test_booking_bad_body_422(client):
    r = client.post("/api/calibration-bookings", json={
        "instrument_code": "W",
        "start": "2026-06-20T09:00:00Z",
        "duration_seconds": -5,
    })
    assert r.status_code == 422


def test_concurrent_overlapping_bookings_single_winner(client):
    """并发确认同一时段：写事务（BEGIN IMMEDIATE）串行化，恰一个成功，
    另一个在事务内重查到已提交的占用后返回 409（不会出现两条重叠预约）。
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    _chain(client)
    barrier = threading.Barrier(2)
    results = []

    def post(start):
        barrier.wait(timeout=5)
        with TestClient(app) as local:
            r = local.post("/api/calibration-bookings", json={
                "instrument_code": "W",
                "start": start, "duration_seconds": 5400,
            })
            return r.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        for code in pool.map(post, [
            "2026-06-20T09:00:00Z",   # [09:00,10:30)
            "2026-06-20T09:30:00Z",   # [09:30,11:00) 与前者重叠
        ]):
            results.append(code)

    assert sorted(results) == [201, 409]
    rows = client.get("/api/calibration-bookings").json()
    # 恰一条落库（写事务串行化，不会出现两条重叠预约）；赢家不确定，
    # 但必为两笔之一的起点。
    assert len(rows) == 1
    assert rows[0]["start_at"] in (
        "2026-06-20T09:00:00Z", "2026-06-20T09:30:00Z")
    assert rows[0]["duration_seconds"] == 5400


def test_booking_failed_recheck_rolls_back_entire_transaction(client):
    """占用重查失败时，同一写事务内此前的任何写入均随回滚消失。

    替换确认模块内的占用重查函数使其返回一条冲突占用，验证
    confirm 路径抛 BookingConflict 且没有部分写入落库。
    """
    from datetime import datetime

    from app import booking
    from app.database import get_conn_ctx

    _chain(client)

    def fake_clashes(conn, start_s, end_s):
        return [{
            "id": "BK-FAKE", "instrument_code": "OTHER",
            "start_at": "2026-06-20T09:00:00Z", "end_at": "2026-06-20T12:00:00Z",
            "duration_seconds": 10800, "certificate_id": "CW-OTHER",
            "certificate_valid_until": "2030-01-01T00:00:00Z",
            "note": "", "created_at": "2026-06-20T00:00:00Z",
        }]

    mp = pytest.MonkeyPatch()
    mp.setattr(booking, "find_clashes", fake_clashes)
    try:
        with pytest.raises(booking.BookingConflict) as excinfo:
            with get_conn_ctx() as conn:
                conn.execute("BEGIN IMMEDIATE")
                booking.confirm_booking(
                    conn, "W", datetime(2026, 6, 20, 10), 3600)
        assert excinfo.value.code == "STATION_OCCUPIED"
    finally:
        mp.undo()

    # 事务回滚：无任何预约残留
    assert client.get("/api/calibration-bookings").json() == []


# ---------------------------------------------------------------- GET bookings

def test_list_bookings_by_instrument(client):
    _chain(client, code="W")
    _chain(client, code="W2")
    for code, ts in (("W", "09:00:00"), ("W2", "11:00:00"), ("W", "13:00:00")):
        assert client.post("/api/calibration-bookings", json={
            "instrument_code": code,
            "start": f"2026-06-20T{ts}Z", "duration_seconds": 600,
        }).status_code == 201

    all_rows = client.get("/api/calibration-bookings").json()
    assert len(all_rows) == 3
    # 按起点升序
    assert [r["start_at"] for r in all_rows] == [
        "2026-06-20T09:00:00Z", "2026-06-20T11:00:00Z", "2026-06-20T13:00:00Z",
    ]
    w_rows = client.get("/api/calibration-bookings",
                        params={"instrument": "W"}).json()
    assert {r["instrument_code"] for r in w_rows} == {"W"}
    assert len(w_rows) == 2


def test_list_bookings_unknown_instrument_404(client):
    r = client.get("/api/calibration-bookings", params={"instrument": "GHOST"})
    assert r.status_code == 404


# ------------------------------------------------ POST bookings/reschedule

def _book(client, *, code="W", start, duration=3600, note=""):
    """确认一条预约并返回其 ID。"""
    r = client.post("/api/calibration-bookings", json={
        "instrument_code": code,
        "start": start,
        "duration_seconds": duration,
        "note": note,
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _reschedule(client, items):
    return client.post("/api/calibration-bookings/reschedule",
                       json={"bookings": items})


def test_reschedule_moves_two_bookings(client):
    _chain(client)
    a = _book(client, start="2026-06-20T09:00:00Z", note="甲")
    b = _book(client, start="2026-06-20T11:00:00Z", note="乙")
    r = _reschedule(client, [
        {"booking_id": a, "start": "2026-06-20T14:00:00Z"},
        {"booking_id": b, "start": "2026-06-20T15:00:00Z"},
    ])
    assert r.status_code == 200, r.text
    out = r.json()
    assert [x["id"] for x in out] == [a, b]   # 按请求顺序返回
    assert out[0]["start_at"] == "2026-06-20T14:00:00Z"
    assert out[0]["end_at"] == "2026-06-20T15:00:00Z"
    assert out[0]["duration_seconds"] == 3600  # 缺省沿用原时长
    assert out[0]["note"] == "甲"               # 备注保留
    assert out[0]["instrument_code"] == "W"     # 仪器保留
    assert out[1]["start_at"] == "2026-06-20T15:00:00Z"
    assert out[1]["end_at"] == "2026-06-20T16:00:00Z"
    assert out[1]["note"] == "乙"
    # 旧时段已释放：现在可在 09:00 新约（半开），且新时段已被占用
    rows = client.get("/api/calibration-bookings").json()
    assert sorted(x["start_at"] for x in rows) == [
        "2026-06-20T14:00:00Z", "2026-06-20T15:00:00Z"]


def test_reschedule_swap_slots_allowed(client):
    """交换两条预约的时段：本批旧占用整体排除，互换不冲突。"""
    _chain(client, code="W")
    _chain(client, code="W2")
    a = _book(client, code="W", start="2026-06-20T09:00:00Z")
    b = _book(client, code="W2", start="2026-06-20T10:00:00Z")
    r = _reschedule(client, [
        {"booking_id": a, "start": "2026-06-20T10:00:00Z"},  # 占 B 的旧位
        {"booking_id": b, "start": "2026-06-20T09:00:00Z"},  # 占 A 的旧位
    ])
    assert r.status_code == 200, r.text
    out = {x["id"]: x for x in r.json()}
    assert out[a]["start_at"] == "2026-06-20T10:00:00Z"
    assert out[b]["start_at"] == "2026-06-20T09:00:00Z"
    # 仪器、备注未被串改
    assert out[a]["instrument_code"] == "W"
    assert out[b]["instrument_code"] == "W2"


def test_reschedule_back_to_back_allowed(client):
    """新时段彼此首尾相接（半开）不冲突。"""
    _chain(client)
    a = _book(client, start="2026-06-20T09:00:00Z")
    b = _book(client, start="2026-06-20T10:00:00Z")
    r = _reschedule(client, [
        {"booking_id": a, "start": "2026-06-20T11:00:00Z"},  # [11,12)
        {"booking_id": b, "start": "2026-06-20T12:00:00Z"},  # [12,13) 相接
    ])
    assert r.status_code == 200, r.text


def test_reschedule_overlap_with_own_old_slot_allowed(client):
    """新时段与自身旧占用重叠不应计冲突（本批旧占用整体排除）。"""
    _chain(client)
    a = _book(client, start="2026-06-20T09:00:00Z")
    b = _book(client, start="2026-06-20T11:00:00Z")
    r = _reschedule(client, [
        {"booking_id": a, "start": "2026-06-20T09:30:00Z"},  # 与自己旧段重叠
        {"booking_id": b, "start": "2026-06-20T13:00:00Z"},
    ])
    assert r.status_code == 200, r.text
    out = {x["id"]: x for x in r.json()}
    assert out[a]["start_at"] == "2026-06-20T09:30:00Z"
    assert out[a]["end_at"] == "2026-06-20T10:30:00Z"


def test_reschedule_new_slots_overlap_each_other_409(client):
    _chain(client)
    a = _book(client, start="2026-06-20T09:00:00Z")
    b = _book(client, start="2026-06-20T11:00:00Z")
    r = _reschedule(client, [
        {"booking_id": a, "start": "2026-06-20T14:00:00Z"},  # [14,15)
        {"booking_id": b, "start": "2026-06-20T14:30:00Z"},  # [14:30,15:30)
    ])
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "STATION_OCCUPIED"
    failures = {f["booking_id"]: f for f in detail["detail"]["failures"]}
    assert set(failures) == {a, b}
    for f in failures.values():
        assert f["code"] == "STATION_OCCUPIED"
        assert len(f["detail"]["conflicting_rescheduled_bookings"]) == 1
        assert f["detail"]["conflicting_bookings"] == []
    # 整体回滚：两条仍是旧时段
    rows = {x["id"]: x for x in client.get("/api/calibration-bookings").json()}
    assert rows[a]["start_at"] == "2026-06-20T09:00:00Z"
    assert rows[b]["start_at"] == "2026-06-20T11:00:00Z"


def test_reschedule_external_booking_overlap_409(client):
    """与本批之外的预约冲突：不在批次中的第三方占用照常阻塞。"""
    _chain(client, code="W")
    _chain(client, code="W2")
    a = _book(client, code="W", start="2026-06-20T09:00:00Z")
    b = _book(client, code="W", start="2026-06-20T10:00:00Z")
    c = _book(client, code="W2", start="2026-06-20T14:30:00Z")
    r = _reschedule(client, [
        {"booking_id": a, "start": "2026-06-20T14:00:00Z"},  # [14,15) 撞 C
        {"booking_id": b, "start": "2026-06-20T16:00:00Z"},  # 本身可行
    ])
    assert r.status_code == 409
    detail = r.json()["detail"]
    failures = detail["detail"]["failures"]
    assert [f["booking_id"] for f in failures] == [a]
    fa = failures[0]
    assert fa["code"] == "STATION_OCCUPIED"
    assert [x["id"] for x in fa["detail"]["conflicting_bookings"]] == [c]
    # 同批 B 虽单独可行也随整体回滚，保持旧时段
    rows = {x["id"]: x for x in client.get("/api/calibration-bookings").json()}
    assert rows[a]["start_at"] == "2026-06-20T09:00:00Z"
    assert rows[b]["start_at"] == "2026-06-20T10:00:00Z"


def test_reschedule_touching_external_booking_allowed(client):
    """新起点恰等于第三方预约结束点（半开相接）不冲突。"""
    _chain(client, code="W")
    _chain(client, code="W2")
    c = _book(client, code="W2", start="2026-06-20T14:00:00Z")  # [14,15)
    a = _book(client, code="W", start="2026-06-20T09:00:00Z")
    b = _book(client, code="W", start="2026-06-20T10:00:00Z")
    r = _reschedule(client, [
        {"booking_id": a, "start": "2026-06-20T15:00:00Z"},  # 紧接 C
        {"booking_id": b, "start": "2026-06-20T16:00:00Z"},
    ])
    assert r.status_code == 200, r.text
    assert c not in [x["id"] for x in r.json()]  # 批次外预约原样不动


def test_reschedule_missing_booking_409(client):
    _chain(client)
    a = _book(client, start="2026-06-20T09:00:00Z")
    r = _reschedule(client, [
        {"booking_id": a, "start": "2026-06-20T15:00:00Z"},
        {"booking_id": "BK-GHOST", "start": "2026-06-20T16:00:00Z"},
    ])
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "BOOKING_NOT_FOUND"
    failures = detail["detail"]["failures"]
    # A 改到 15:00 与任何占用都不冲突，不应出现在失败项中
    assert all(f["booking_id"] != a for f in failures)
    f_missing = next(f for f in failures
                     if f["booking_id"] == "BK-GHOST")
    assert f_missing["code"] == "BOOKING_NOT_FOUND"
    # 回滚：A 仍是旧时段
    rows = client.get("/api/calibration-bookings").json()
    assert rows[0]["start_at"] == "2026-06-20T09:00:00Z"


def test_reschedule_duplicate_id_409(client):
    _chain(client)
    a = _book(client, start="2026-06-20T09:00:00Z")
    b = _book(client, start="2026-06-20T11:00:00Z")
    r = _reschedule(client, [
        {"booking_id": a, "start": "2026-06-20T14:00:00Z"},
        {"booking_id": a, "start": "2026-06-20T15:00:00Z"},
        {"booking_id": b, "start": "2026-06-20T16:00:00Z"},
    ])
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "BOOKING_ID_DUPLICATED"
    dup = [f for f in detail["detail"]["failures"]
           if f["code"] == "BOOKING_ID_DUPLICATED"]
    assert [f["booking_id"] for f in dup] == [a]  # 第二次出现报一次
    # 重复 ID 自身的两个条目都不参与占用/溯源核验，B 不误伤
    assert all(f["booking_id"] != b for f in detail["detail"]["failures"])


def test_reschedule_chain_unavailable_at_new_start_409(client):
    # 起点证书 2027-01-01 到期：改到 2030 年新起点无有效起点证书
    _chain(client)
    a = _book(client, start="2026-06-20T09:00:00Z")
    b = _book(client, start="2026-06-20T11:00:00Z")
    r = _reschedule(client, [
        {"booking_id": a, "start": "2030-06-20T09:00:00Z"},
        {"booking_id": b, "start": "2026-06-20T15:00:00Z"},
    ])
    assert r.status_code == 409
    failures = r.json()["detail"]["detail"]["failures"]
    fa = next(f for f in failures if f["booking_id"] == a)
    assert fa["code"] == "TRACEABILITY_CHAIN_UNAVAILABLE"
    assert fa["detail"]["reason_code"] == "NO_ACTIVE_CERTIFICATE"
    assert fa["detail"]["trace_report"]["resolution"] == "broken"


def test_reschedule_ends_after_cert_expiry_409(client):
    _chain(client, leaf_until="2027-01-01T00:00:00Z")
    a = _book(client, start="2026-06-20T09:00:00Z")
    b = _book(client, start="2026-06-20T11:00:00Z")
    r = _reschedule(client, [
        {"booking_id": a, "start": "2026-12-31T23:00:00Z",
         "duration_seconds": 7200},   # end 2027-01-01T01:00 > 截止
        {"booking_id": b, "start": "2026-06-20T15:00:00Z"},
    ])
    assert r.status_code == 409
    failures = r.json()["detail"]["detail"]["failures"]
    fa = next(f for f in failures if f["booking_id"] == a)
    assert fa["code"] == "BOOKING_ENDS_AFTER_CERT_EXPIRY"
    assert fa["detail"]["certificate_valid_until"] == "2027-01-01T00:00:00Z"
    rows = {x["id"]: x for x in client.get("/api/calibration-bookings").json()}
    assert rows[a]["start_at"] == "2026-06-20T09:00:00Z"


def test_reschedule_end_exactly_at_expiry_allowed(client):
    _chain(client, leaf_until="2027-01-01T00:00:00Z")
    a = _book(client, start="2026-06-20T09:00:00Z")
    b = _book(client, start="2026-06-20T11:00:00Z")
    r = _reschedule(client, [
        {"booking_id": a, "start": "2026-12-31T23:00:00Z",
         "duration_seconds": 3600},   # end == valid_until
        {"booking_id": b, "start": "2026-06-20T15:00:00Z"},
    ])
    assert r.status_code == 200, r.text
    out = {x["id"]: x for x in r.json()}
    assert out[a]["end_at"] == "2027-01-01T00:00:00Z"


def test_reschedule_with_new_duration(client):
    _chain(client)
    a = _book(client, start="2026-06-20T09:00:00Z", duration=3600)
    b = _book(client, start="2026-06-20T11:00:00Z", duration=3600)
    r = _reschedule(client, [
        {"booking_id": a, "start": "2026-06-20T14:00:00Z",
         "duration_seconds": 1800},
        {"booking_id": b, "start": "2026-06-20T15:00:00Z"},  # 缺省沿用
    ])
    assert r.status_code == 200, r.text
    out = {x["id"]: x for x in r.json()}
    assert out[a]["duration_seconds"] == 1800
    assert out[a]["end_at"] == "2026-06-20T14:30:00Z"
    assert out[b]["duration_seconds"] == 3600


def test_reschedule_refreshes_certificate_snapshot(client):
    """新起点落在换发后的新证书区间：certificate_id / valid_until 快照刷新。"""
    _chain(client)
    # 为 W 补一张 2026-09-01 生效的换发证书（与旧证首尾相接，避免区间断档）
    r = client.post("/api/submissions", json={
        "instruments": [],
        "certificates": [
            {"id": "CW2-W", "instrument_code": "W", "cert_number": "W-W2",
             "issued_at": "2026-08-20T00:00:00Z",
             "valid_from": "2027-01-01T00:00:00Z",
             "valid_until": "2028-01-01T00:00:00Z",
             "uncertainty": 0.01, "unit": "mm"},
        ],
        "edges": [{"child_cert_id": "CW2-W", "parent_cert_id": "CR-W"}],
    })
    assert r.status_code == 201, r.text
    a = _book(client, start="2026-06-20T09:00:00Z")
    b = _book(client, start="2026-06-20T11:00:00Z")
    r = _reschedule(client, [
        {"booking_id": a, "start": "2027-02-01T09:00:00Z"},
        {"booking_id": b, "start": "2027-02-01T10:00:00Z"},
    ])
    assert r.status_code == 200, r.text
    for x in r.json():
        assert x["certificate_id"] == "CW2-W"
        assert x["certificate_valid_until"] == "2028-01-01T00:00:00Z"


def test_reschedule_preserves_note_and_created_at(client):
    _chain(client)
    a = _book(client, start="2026-06-20T09:00:00Z", note="保留备注")
    b = _book(client, start="2026-06-20T11:00:00Z")
    before = {x["id"]: x for x in client.get("/api/calibration-bookings").json()}
    r = _reschedule(client, [
        {"booking_id": a, "start": "2026-06-20T14:00:00Z"},
        {"booking_id": b, "start": "2026-06-20T15:00:00Z"},
    ])
    assert r.status_code == 200, r.text
    after = {x["id"]: x for x in r.json()}
    assert after[a]["note"] == "保留备注"
    assert after[a]["created_at"] == before[a]["created_at"]


@pytest.mark.parametrize("n", [1, 11])
def test_reschedule_count_out_of_range_422(client, n):
    _chain(client)
    a = _book(client, start="2026-06-20T09:00:00Z")
    r = _reschedule(client, [
        {"booking_id": a, "start": "2026-06-20T14:00:00Z"}
        for _ in range(n)
    ])
    assert r.status_code == 422


def test_reschedule_nonpositive_new_duration_422(client):
    _chain(client)
    a = _book(client, start="2026-06-20T09:00:00Z")
    b = _book(client, start="2026-06-20T11:00:00Z")
    r = _reschedule(client, [
        {"booking_id": a, "start": "2026-06-20T14:00:00Z",
         "duration_seconds": 0},
        {"booking_id": b, "start": "2026-06-20T15:00:00Z"},
    ])
    assert r.status_code == 422


def test_reschedule_empty_booking_id_422(client):
    r = _reschedule(client, [
        {"booking_id": "", "start": "2026-06-20T14:00:00Z"},
        {"booking_id": "BK-X", "start": "2026-06-20T15:00:00Z"},
    ])
    assert r.status_code == 422
