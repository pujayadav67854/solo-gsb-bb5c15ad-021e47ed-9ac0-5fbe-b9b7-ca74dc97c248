AT = "2026-06-15T00:00:00"


def _instruments():
    return [
        {"code": "G", "name": "国家基准", "is_national_standard": True},
        {"code": "R", "name": "参考标准器"},
        {"code": "W", "name": "工作仪器"},
    ]


def _certs():
    return [
        {"id": "CG", "instrument_code": "G", "cert_number": "G-1",
         "issued_at": "2024-01-01T00:00:00",
         "valid_from": "2024-01-01T00:00:00",
         "valid_until": "2030-01-01T00:00:00",
         "uncertainty": 0.0001, "unit": "mm"},
        {"id": "CR", "instrument_code": "R", "cert_number": "R-1",
         "issued_at": "2025-06-01T00:00:00",
         "valid_from": "2025-06-01T00:00:00",
         "valid_until": "2027-06-01T00:00:00",
         "uncertainty": 0.001, "unit": "mm"},
        {"id": "CW", "instrument_code": "W", "cert_number": "W-1",
         "issued_at": "2026-01-01T00:00:00",
         "valid_from": "2026-01-01T00:00:00",
         "valid_until": "2027-01-01T00:00:00",
         "uncertainty": 0.01, "unit": "mm"},
    ]


def _edges():
    return [
        {"child_cert_id": "CW", "parent_cert_id": "CR"},
        {"child_cert_id": "CR", "parent_cert_id": "CG"},
    ]


def _submit(client, instruments=None, certs=None, edges=None):
    return client.post(
        "/api/submissions",
        json={
            "instruments": instruments or _instruments(),
            "certificates": certs or _certs(),
            "edges": edges if edges is not None else _edges(),
        },
    )


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_submit_and_unique_chain(client):
    r = _submit(client)
    assert r.status_code == 201, r.text
    assert set(r.json()["instruments_upserted"]) == {"G", "R", "W"}

    r = client.get(f"/api/trace/W?at={AT}")
    report = r.json()
    assert report["resolution"] == "unique"
    assert report["valid"] is True
    chain = report["active_chain"]
    assert [n["certificate_id"] for n in chain["nodes"]] == ["CW", "CR", "CG"]
    assert chain["reaches_national_standard"] is True
    assert report["error_count"] == 0
    assert chain["nodes"][-1]["is_national_standard"] is True


def test_multiple_candidate_chains_ambiguous(client):
    insts = _instruments() + [
        {"code": "R2", "name": "第二参考标准器"},
    ]
    certs = _certs() + [
        {"id": "CR2", "instrument_code": "R2", "cert_number": "R2-1",
         "issued_at": "2025-07-01T00:00:00",
         "valid_from": "2025-07-01T00:00:00",
         "valid_until": "2027-07-01T00:00:00",
         "uncertainty": 0.002, "unit": "mm"},
    ]
    edges = _edges() + [
        {"child_cert_id": "CW", "parent_cert_id": "CR2"},
        {"child_cert_id": "CR2", "parent_cert_id": "CG"},
    ]
    _submit(client, insts, certs, edges)

    report = client.get(f"/api/reports/W?at={AT}").json()
    assert report["resolution"] == "ambiguous"
    assert report["valid"] is False
    assert len(report["candidates"]) == 2
    codes = {
        tuple(n["certificate_id"] for n in c["nodes"])
        for c in report["candidates"]
    }
    assert codes == {("CW", "CR", "CG"), ("CW", "CR2", "CG")}
    assert any(c["code"] == "MULTIPLE_VALID_CHAINS" for c in report["conflicts"])


def test_broken_chain_dead_end_and_interval_gap(client):
    insts = [
        {"code": "TP", "name": "参考温度计"},  # 非国家基准且无父边 -> 死路
        {"code": "T", "name": "工作热电偶"},
    ]
    certs = [
        {"id": "CTP", "instrument_code": "TP", "cert_number": "TP-1",
         "issued_at": "2025-01-01T00:00:00",
         "valid_from": "2025-01-01T00:00:00",
         "valid_until": "2027-01-01T00:00:00",
         "uncertainty": 0.05, "unit": "C"},
        {"id": "CT-1", "instrument_code": "T", "cert_number": "T-1",
         "issued_at": "2025-01-05T00:00:00",
         "valid_from": "2025-01-01T00:00:00",
         "valid_until": "2025-06-30T00:00:00",
         "uncertainty": 0.5, "unit": "C"},
        {"id": "CT-2", "instrument_code": "T", "cert_number": "T-2",
         "issued_at": "2025-09-01T00:00:00",
         "valid_from": "2025-09-01T00:00:00",
         "valid_until": "2026-09-01T00:00:00",
         "uncertainty": 0.4, "unit": "C"},
    ]
    edges = [{"child_cert_id": "CT-2", "parent_cert_id": "CTP"}]
    _submit(client, insts, certs, edges)

    report = client.get(f"/api/trace/T?at={AT}").json()
    assert report["resolution"] == "broken"
    assert report["valid"] is False
    codes = {c["code"] for c in report["conflicts"]}
    assert "DEAD_END" in codes
    assert "INTERVAL_GAP" in codes
    assert "CHAIN_BROKEN" in codes
    # 冲突节点可解释：断档应指向具体证书
    gap = next(c for c in report["conflicts"] if c["code"] == "INTERVAL_GAP")
    assert gap["detail"]["prev_cert_id"] == "CT-1"
    assert len(report["broken_chains"]) == 1


def test_no_active_certificate_at_time(client):
    _submit(client)
    report = client.get("/api/trace/W?at=2035-01-01T00:00:00").json()
    assert report["resolution"] == "broken"
    assert any(
        c["code"] == "NO_ACTIVE_CERTIFICATE" for c in report["conflicts"]
    )


def test_signature_digest_mismatch_invalidates_chain(client):
    certs = _certs()
    tampered = next(c for c in certs if c["id"] == "CW")
    tampered["signature_digest"] = "0" * 64  # 与规范化载荷重算结果不符
    _submit(client, certs=certs)

    report = client.get(f"/api/trace/W?at={AT}").json()
    assert report["resolution"] == "broken"
    mismatch = [
        c for c in report["conflicts"]
        if c["code"] == "SIGNATURE_DIGEST_MISMATCH"
    ]
    assert len(mismatch) == 1
    assert mismatch[0]["node"] == "CW"


def test_uncertainty_regression_detected(client):
    certs = _certs()
    # 工作仪器不确定度小于上级参考标准 -> 沿链收缩
    next(c for c in certs if c["id"] == "CW")["uncertainty"] = 0.0001
    _submit(client, certs=certs)

    report = client.get(f"/api/trace/W?at={AT}").json()
    assert report["valid"] is False
    assert any(
        c["code"] == "UNCERTAINTY_REGRESSION" and c["node"] == "CW"
        for c in report["conflicts"]
    )


def test_export_returns_json_attachment(client):
    _submit(client)
    r = client.get(f"/api/export/W?at={AT}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/json"
    assert "attachment" in r.headers["content-disposition"]
    assert r.json()["resolution"] == "unique"
    assert r.headers["x-trace-valid"] == "true"


def test_self_referencing_edge_rejected(client):
    _submit(client, edges=[])
    r = client.post(
        "/api/edges",
        json={"child_cert_id": "CW", "parent_cert_id": "CW"},
    )
    assert r.status_code == 422


def test_trace_missing_instrument_404(client):
    assert client.get("/api/trace/NOPE").status_code == 404
