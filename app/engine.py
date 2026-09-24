"""溯源链重建与校验引擎。

核心概念
--------
* 一台仪器持有多张按时间排列的校准证书，理想情况下区间首尾相接。
* ``trace_edges`` 描述“子证书 -> 父（参考标准）证书”的父子溯源关系，
  同一子证书可存在多条候选父边（多参考标准 / 多渠道送检），因此候选链可能不止一条。
* 查询时给定仪器与时点 ``at``：

  1. 取该仪器在 ``at`` 生效（区间闭区间）的证书作为起点；0 张=断裂，>1 张=多源冲突；
  2. 自起点沿在 ``at`` 有效的边做深度优先枚举，直到“仪器为国家基准”的证书（成链）、
     无边可走（死路）、父证书在该时点失效（断档）或遇到环；
  3. 对每条链执行：签名摘要重算、父区间是否覆盖子校准时刻、测量不确定度
     沿链不得收缩（逐级放大）、单位一致性；
  4. 全部成链且通过校验的路径唯一 -> ``unique``；>=2 -> ``ambiguous``；
     0 条 -> ``broken``。所有问题以带 code/节点/链下标的冲突列表返回。
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta
from itertools import combinations
from typing import Any, Optional

from .crud import ValidationError, get_instrument
from .utils import UNCERTAINTY_EPS, compute_digest, fmt, to_utc_naive

MAX_PATHS = 500  # 候选链枚举上限，防止异常数据下的指数膨胀

_TS_PARSE_FMT = "%Y-%m-%dT%H:%M:%SZ"


# ---------------------------------------------------------------- 数据装载

class Catalog:
    """证书/仪器/边的内存快照。

    常规评估直接从连接装载；证书停用模拟则在内存中构造一个“移除证书与
    关联边”后的派生 Catalog，全程不执行任何写库操作。
    """

    def __init__(
        self,
        conn: Optional[sqlite3.Connection] = None,
        *,
        instruments: Optional[dict[str, Any]] = None,
        certs: Optional[dict[str, Any]] = None,
        edges: Optional[list[Any]] = None,
    ) -> None:
        if conn is not None:
            instruments = {
                r["code"]: r
                for r in conn.execute("SELECT * FROM instruments").fetchall()
            }
            certs = {
                r["id"]: r
                for r in conn.execute("SELECT * FROM certificates").fetchall()
            }
            edges = list(conn.execute("SELECT * FROM trace_edges").fetchall())
        self.instruments: dict[str, Any] = instruments or {}
        self.certs: dict[str, Any] = certs or {}
        self.edges: list[Any] = edges or []
        self.out_edges: dict[str, list[Any]] = {}
        for e in self.edges:
            self.out_edges.setdefault(e["child_cert_id"], []).append(e)


# ---------------------------------------------------------------- 冲突

def _conflict(code, severity, message, node=None, instrument=None, detail=None):
    return {
        "code": code,
        "severity": severity,
        "node": node,
        "instrument": instrument,
        "path_index": None,
        "message": message,
        "detail": detail or {},
    }


# ---------------------------------------------------------------- 链枚举

def _enumerate_paths(cat: Catalog, starts: list[str], at_s: str):
    """返回 (paths, truncated)。

    path = {"cert_ids": [...leaf -> root...], "edge_ids": [...], "reaches_root",
            "cycle", "issues": [...路径级冲突...]}
    终止节点的问题记录在该 path 的 issues 中。
    """
    paths: list[dict[str, Any]] = []
    truncated = False

    def active_parents(cid: str) -> list[sqlite3.Row]:
        # 同一父证书的并行边视为同一“仪器级”溯源步，取 id 最小的边，保持确定性
        chosen: dict[str, sqlite3.Row] = {}
        for e in cat.out_edges.get(cid, []):
            if e["valid_from"] <= at_s <= e["valid_until"]:
                pid = e["parent_cert_id"]
                if pid not in chosen or e["id"] < chosen[pid]["id"]:
                    chosen[pid] = e
        return [chosen[pid] for pid in sorted(chosen)]

    def record(cert_ids, edge_ids, reaches_root, cycle, issues):
        paths.append(
            {
                "cert_ids": list(cert_ids),
                "edge_ids": list(edge_ids),
                "reaches_root": reaches_root,
                "cycle": cycle,
                "issues": issues,
            }
        )

    def dfs(cert_ids, edge_ids, visited):
        nonlocal truncated
        if len(paths) >= MAX_PATHS:
            truncated = True
            return
        current = cert_ids[-1]
        current_cert = cat.certs[current]
        current_inst = cat.instruments[current_cert["instrument_code"]]
        parents = active_parents(current)

        if current_inst["is_national_standard"]:
            record(cert_ids, edge_ids, True, False, [])
            return
        if not parents:
            record(
                cert_ids,
                edge_ids,
                False,
                False,
                [
                    _conflict(
                        "DEAD_END",
                        "ERROR",
                        f"证书 {current}（{current_cert['cert_number']}）"
                        "在该时点没有任何有效的向上溯源边，无法到达国家基准",
                        node=current,
                    )
                ],
            )
            return

        for edge in parents:
            pid = edge["parent_cert_id"]
            if pid in visited:
                issues = [
                    _conflict(
                        "CYCLE_DETECTED",
                        "ERROR",
                        f"溯源关系存在环：证书 {pid} 在链路中重复出现",
                        node=pid,
                        detail={"edge_id": edge["id"]},
                    )
                ]
                record(cert_ids + [pid], edge_ids + [edge["id"]], False, True, issues)
                continue
            parent_cert = cat.certs.get(pid)
            if parent_cert is None:
                continue
            if not (parent_cert["valid_from"] <= at_s <= parent_cert["valid_until"]):
                issues = [
                    _conflict(
                        "PARENT_CERT_NOT_ACTIVE",
                        "ERROR",
                        f"父证书 {pid} 在查询时点 {at_s} 已失效或尚未生效"
                        f"（有效区间 {parent_cert['valid_from']} ~ "
                        f"{parent_cert['valid_until']}），溯源链在此断裂",
                        node=pid,
                        detail={
                            "edge_id": edge["id"],
                            "valid_from": parent_cert["valid_from"],
                            "valid_until": parent_cert["valid_until"],
                        },
                    )
                ]
                record(
                    cert_ids + [pid],
                    edge_ids + [edge["id"]],
                    False,
                    False,
                    issues,
                )
                continue
            dfs(cert_ids + [pid], edge_ids + [edge["id"]], visited | {pid})

    for start in starts:
        dfs([start], [], {start})

    # 完全相同的节点序列去重
    dedup: dict[tuple, dict[str, Any]] = {}
    for p in paths:
        key = tuple(p["cert_ids"])
        dedup.setdefault(key, p)
    return list(dedup.values()), truncated


# ---------------------------------------------------------------- 节点级检查

def _check_node(cat: Catalog, cert_id: str, at_s: str):
    """对单张证书做时点有效性 + 签名摘要复核。返回冲突列表。"""
    issues: list[dict[str, Any]] = []
    cert = cat.certs[cert_id]
    if not (cert["valid_from"] <= at_s <= cert["valid_until"]):
        issues.append(
            _conflict(
                "CERT_NOT_ACTIVE",
                "ERROR",
                f"证书 {cert_id} 在查询时点 {at_s} 不处于有效区间",
                node=cert_id,
                detail={
                    "valid_from": cert["valid_from"],
                    "valid_until": cert["valid_until"],
                },
            )
        )
    expected = compute_digest(cert["signed_payload"], cert["digest_alg"])
    if expected != cert["signature_digest"]:
        issues.append(
            _conflict(
                "SIGNATURE_DIGEST_MISMATCH",
                "ERROR",
                f"证书 {cert_id}（{cert['cert_number']}）签名摘要校验失败："
                f"登记 {cert['signature_digest']}，重算 {expected}",
                node=cert_id,
                detail={
                    "digest_alg": cert["digest_alg"],
                    "stored": cert["signature_digest"],
                    "recomputed": expected,
                },
            )
        )
    return issues


# ---------------------------------------------------------------- 链级检查

def _check_chain(cat: Catalog, path: dict[str, Any]):
    """父子相邻关系检查：区间覆盖、不确定度收缩、边覆盖校准时刻。"""
    issues: list[dict[str, Any]] = []
    cert_ids = path["cert_ids"]
    edges_by_id = {e["id"]: e for e in cat.edges}

    for i in range(len(cert_ids) - 1):
        child = cat.certs[cert_ids[i]]
        parent = cat.certs.get(cert_ids[i + 1])
        edge = edges_by_id.get(path["edge_ids"][i]) if i < len(path["edge_ids"]) else None
        if parent is None:  # 环回的重复节点仍可查到，这里仅作防御
            continue
        pair_detail = {
            "child_cert_id": child["id"],
            "parent_cert_id": parent["id"],
        }

        # 父证书有效期必须覆盖子证书的校准（签发）时刻
        if not (parent["valid_from"] <= child["issued_at"] <= parent["valid_until"]):
            issues.append(
                _conflict(
                    "PARENT_INTERVAL_NOT_COVER_CALIBRATION",
                    "ERROR",
                    f"父证书 {parent['id']} 的有效区间（{parent['valid_from']} ~ "
                    f"{parent['valid_until']}）未覆盖子证书 {child['id']} 的校准时刻 "
                    f"{child['issued_at']}，溯源关系在校准发生时不成立",
                    node=parent["id"],
                    detail={
                        **pair_detail,
                        "child_issued_at": child["issued_at"],
                    },
                )
            )

        # 扩展测量不确定度沿溯源方向只能逐级放大（child >= parent）
        cu, pu = child["unit"].strip(), parent["unit"].strip()
        if cu and pu and cu != pu:
            issues.append(
                _conflict(
                    "UNCERTAINTY_UNIT_MISMATCH",
                    "ERROR",
                    f"子证书 {child['id']} 不确定度单位 '{cu}' 与父证书 "
                    f"{parent['id']} 单位 '{pu}' 不一致，无法比较量值",
                    node=child["id"],
                    detail={**pair_detail, "child_unit": cu, "parent_unit": pu},
                )
            )
        elif child["uncertainty"] + UNCERTAINTY_EPS < parent["uncertainty"]:
            issues.append(
                _conflict(
                    "UNCERTAINTY_REGRESSION",
                    "ERROR",
                    f"测量不确定度沿溯源链收缩：子证书 {child['id']} 为 "
                    f"{child['uncertainty']}{cu}，父证书 {parent['id']} 为 "
                    f"{parent['uncertainty']}{pu}（下级不确定度不得小于上级）",
                    node=child["id"],
                    detail={
                        **pair_detail,
                        "child_uncertainty": child["uncertainty"],
                        "parent_uncertainty": parent["uncertainty"],
                    },
                )
            )
        elif abs(child["uncertainty"] - parent["uncertainty"]) <= UNCERTAINTY_EPS:
            issues.append(
                _conflict(
                    "UNCERTAINTY_NOT_DECAY",
                    "WARN",
                    f"子证书 {child['id']} 与父证书 {parent['id']} 的扩展不确定度"
                    f"相同（{child['uncertainty']}{cu}），未体现逐级放大",
                    node=child["id"],
                    detail=pair_detail,
                )
            )

        # 溯源边本身的有效期应覆盖子证书的校准时刻
        if edge is not None and not (
            edge["valid_from"] <= child["issued_at"] <= edge["valid_until"]
        ):
            issues.append(
                _conflict(
                    "EDGE_NOT_COVER_CALIBRATION",
                    "WARN",
                    f"溯源边 #{edge['id']}（{child['id']} -> {parent['id']}）"
                    f"有效区间未覆盖子证书校准时刻 {child['issued_at']}",
                    node=child["id"],
                    detail={
                        "edge_id": edge["id"],
                        "edge_valid_from": edge["valid_from"],
                        "edge_valid_until": edge["valid_until"],
                        **pair_detail,
                    },
                )
            )
    return issues


# ---------------------------------------------------------------- 仪器历史区间连续性

def _check_history_gaps(cat: Catalog, instrument_code: str):
    issues: list[dict[str, Any]] = []
    certs = sorted(
        [c for c in cat.certs.values() if c["instrument_code"] == instrument_code],
        key=lambda c: (c["valid_from"], c["valid_until"]),
    )
    for prev, nxt in zip(certs, certs[1:]):
        if nxt["valid_from"] > prev["valid_until"]:
            issues.append(
                _conflict(
                    "INTERVAL_GAP",
                    "ERROR",
                    f"仪器 {instrument_code} 的校准证书区间不连续："
                    f"{prev['cert_number']} 止于 {prev['valid_until']}，"
                    f"{nxt['cert_number']} 始于 {nxt['valid_from']}，中间存在断档",
                    node=prev["id"],
                    instrument=instrument_code,
                    detail={
                        "prev_cert_id": prev["id"],
                        "next_cert_id": nxt["id"],
                        "gap_from": prev["valid_until"],
                        "gap_to": nxt["valid_from"],
                    },
                )
            )
    return issues


# ---------------------------------------------------------------- 序列化

def _serialize_chain(cat: Catalog, path: dict[str, Any]) -> dict[str, Any]:
    edges_by_id = {e["id"]: e for e in cat.edges}
    nodes = []
    for cid in path["cert_ids"]:
        c = cat.certs[cid]
        inst = cat.instruments[c["instrument_code"]]
        nodes.append(
            {
                "certificate_id": c["id"],
                "cert_number": c["cert_number"],
                "instrument_code": inst["code"],
                "instrument_name": inst["name"],
                "is_national_standard": bool(inst["is_national_standard"]),
                "valid_from": c["valid_from"],
                "valid_until": c["valid_until"],
                "issued_at": c["issued_at"],
                "uncertainty": c["uncertainty"],
                "unit": c["unit"],
            }
        )
    edges = []
    for eid in path["edge_ids"]:
        e = edges_by_id[eid]
        edges.append(
            {
                "edge_id": e["id"],
                "child_cert_id": e["child_cert_id"],
                "parent_cert_id": e["parent_cert_id"],
                "valid_from": e["valid_from"],
                "valid_until": e["valid_until"],
                "note": e["note"],
            }
        )
    return {
        "nodes": nodes,
        "edges": edges,
        "reaches_national_standard": path["reaches_root"],
        "cycle": path["cycle"],
        "errors": 0,
        "warnings": 0,
    }


# ---------------------------------------------------------------- 主入口

def build_report(
    conn: sqlite3.Connection,
    instrument_code: str,
    at: Optional[datetime] = None,
) -> dict[str, Any]:
    at_naive = to_utc_naive(at) if at else datetime.utcnow()
    at_s = fmt(at_naive)

    instrument = get_instrument(conn, instrument_code)
    if instrument is None:
        raise KeyError(instrument_code)

    return _build_report(Catalog(conn), instrument_code, at_s)


def _active_starts(cat: Catalog, instrument_code: str, at_s: str) -> list[str]:
    return sorted(
        c["id"]
        for c in cat.certs.values()
        if c["instrument_code"] == instrument_code
        and c["valid_from"] <= at_s <= c["valid_until"]
    )


def _build_report(
    cat: Catalog, instrument_code: str, at_s: str
) -> dict[str, Any]:
    global_issues: list[dict[str, Any]] = []

    # 1) 起点：该仪器在 at 生效的证书
    starts = _active_starts(cat, instrument_code, at_s)
    if not starts:
        global_issues.append(
            _conflict(
                "NO_ACTIVE_CERTIFICATE",
                "ERROR",
                f"仪器 {instrument_code} 在时点 {at_s} 没有处于有效区间的校准证书，"
                "溯源链无法开始（区间断裂）",
                instrument=instrument_code,
            )
        )
    elif len(starts) > 1:
        global_issues.append(
            _conflict(
                "MULTIPLE_ACTIVE_CERTIFICATES",
                "ERROR",
                f"仪器 {instrument_code} 在时点 {at_s} 同时存在 {len(starts)} 张"
                f"有效证书 {starts}，溯源起点不唯一",
                instrument=instrument_code,
                detail={"certificate_ids": starts},
            )
        )

    # 2) DFS 枚举全部候选路径
    paths, truncated = _enumerate_paths(cat, starts, at_s)
    if truncated:
        global_issues.append(
            _conflict(
                "TOO_MANY_CANDIDATES",
                "WARN",
                f"候选链数量超过枚举上限 {MAX_PATHS}，结果可能不完整",
                instrument=instrument_code,
            )
        )

    # 3) 节点级检查（跨路径去重，节点问题属于经过它的每一条链）
    node_issues: dict[str, list[dict]] = {}
    seen_nodes: set[str] = set()
    for p in paths:
        for cid in p["cert_ids"]:
            if cid not in seen_nodes:
                seen_nodes.add(cid)
                node_issues[cid] = _check_node(cat, cid, at_s)

    # 国家基准证书不应再拥有有效向上溯源边
    for cid, cert in cat.certs.items():
        inst = cat.instruments[cert["instrument_code"]]
        if not inst["is_national_standard"]:
            continue
        for e in cat.out_edges.get(cid, []):
            if e["valid_from"] <= at_s <= e["valid_until"]:
                global_issues.append(
                    _conflict(
                        "ROOT_HAS_PARENT",
                        "WARN",
                        f"国家基准证书 {cid} 在该时点仍存在向上溯源边 #{e['id']}，"
                        "基准节点不应再有上级",
                        node=cid,
                        detail={"edge_id": e["id"]},
                    )
                )
                break

    # 4) 仪器历史区间连续性（候选链涉及的全部仪器）
    gap_issues: list[dict] = []
    checked_insts = {instrument_code}
    for p in paths:
        for cid in p["cert_ids"]:
            checked_insts.add(cat.certs[cid]["instrument_code"])
    for ic in sorted(checked_insts):
        gap_issues.extend(_check_history_gaps(cat, ic))

    # 5) 链级检查并统计每条链的错误数（含其经过节点的节点级错误）
    for p in paths:
        p["chain_issues"] = _check_chain(cat, p)
        err_nodes = {
            cid
            for cid in p["cert_ids"]
            if any(i["severity"] == "ERROR" for i in node_issues.get(cid, []))
        }
        p["error_nodes"] = err_nodes
        path_errors = [i for i in p["issues"] if i["severity"] == "ERROR"]
        chain_errors = [i for i in p["chain_issues"] if i["severity"] == "ERROR"]
        p["error_count"] = len(path_errors) + len(chain_errors) + len(err_nodes)

    root_paths = [p for p in paths if p["reaches_root"]]
    valid_root = [p for p in root_paths if p["error_count"] == 0]

    # 6) 决策：唯一 / 多候选 / 断裂
    chains = [_serialize_chain(cat, p) for p in paths]
    for p, ch in zip(paths, chains):
        ch["errors"] = p["error_count"]
        ch["warnings"] = (
            sum(1 for i in p["issues"] if i["severity"] == "WARN")
            + sum(1 for i in p["chain_issues"] if i["severity"] == "WARN")
        )
    path_index = {id(p): idx for idx, p in enumerate(paths)}

    if len(valid_root) >= 2:
        resolution = "ambiguous"
        active_chain = None
        candidate_paths = valid_root
        candidate_chains = [chains[path_index[id(p)]] for p in candidate_paths]
        other_paths = [p for p in paths if p not in candidate_paths]
        broken_chains = [chains[path_index[id(p)]] for p in other_paths]
        global_issues.append(
            _conflict(
                "MULTIPLE_VALID_CHAINS",
                "ERROR",
                f"存在 {len(valid_root)} 条均能到达国家基准且通过校验的候选链，"
                "溯源链不唯一；请核对重复送检或重复登记的溯源边",
                instrument=instrument_code,
                detail={
                    "path_indices": [path_index[id(p)] for p in candidate_paths]
                },
            )
        )
    elif len(valid_root) == 1:
        resolution = "unique"
        chosen = valid_root[0]
        active_chain = chains[path_index[id(chosen)]]
        candidate_chains = []
        other_paths = [p for p in paths if p is not chosen]
        broken_chains = [chains[path_index[id(p)]] for p in other_paths]
        for p in root_paths:
            if p is not chosen:
                global_issues.append(
                    _conflict(
                        "CANDIDATE_CHAIN_FAILED_CHECKS",
                        "WARN",
                        f"另一条到达国家基准的候选链（下标 "
                        f"{path_index[id(p)]}）存在 {p['error_count']} 个错误，"
                        "已从唯一有效链中排除",
                        node=p["cert_ids"][0],
                        detail={"path_index": path_index[id(p)]},
                    )
                )
        if other_paths:
            global_issues.append(
                _conflict(
                    "ALTERNATIVE_BRANCH_PRESENT",
                    "WARN",
                    f"除唯一有效链外还检测到 {len(other_paths)} 条未到达国家基准的"
                    "候选分支，详见 broken_chains",
                    instrument=instrument_code,
                )
            )
    else:
        resolution = "broken"
        active_chain = None
        candidate_paths = root_paths
        candidate_chains = [chains[path_index[id(p)]] for p in root_paths]
        broken_chains = [
            chains[path_index[id(p)]] for p in paths if not p["reaches_root"]
        ]
        if root_paths:
            global_issues.append(
                _conflict(
                    "ALL_CHAINS_FAILED_CHECKS",
                    "ERROR",
                    f"有 {len(root_paths)} 条候选链可到达国家基准，但均未通过校验"
                    "（签名/区间/不确定度），无有效溯源链",
                    instrument=instrument_code,
                    detail={
                        "path_indices": [path_index[id(p)] for p in root_paths]
                    },
                )
            )
        elif paths:
            global_issues.append(
                _conflict(
                    "CHAIN_BROKEN",
                    "ERROR",
                    "全部候选分支均在到达国家基准前终止（死路/断档/环），"
                    "具体冲突节点见各链 conflicts",
                    instrument=instrument_code,
                )
            )

    # 7) 汇总带 path_index 的冲突列表
    conflicts: list[dict[str, Any]] = list(global_issues)
    conflicts.extend(gap_issues)
    for cid, issues in node_issues.items():
        conflicts.extend(issues)
    for p in paths:
        idx = path_index[id(p)]
        for issue in p["issues"] + p["chain_issues"]:
            issue = dict(issue)
            issue["path_index"] = idx
            conflicts.append(issue)

    error_count = sum(1 for c in conflicts if c["severity"] == "ERROR")
    warning_count = sum(1 for c in conflicts if c["severity"] == "WARN")

    return {
        "instrument": instrument_code,
        "at": at_s,
        "resolution": resolution,
        "valid": resolution == "unique" and error_count == 0,
        "starts": starts,
        "active_chain": active_chain,
        "candidates": candidate_chains,
        "broken_chains": broken_chains,
        "conflicts": conflicts,
        "error_count": error_count,
        "warning_count": warning_count,
        "statistics": {
            "instruments_total": len(cat.instruments),
            "certificates_total": len(cat.certs),
            "edges_total": len(cat.edges),
            "paths_enumerated": len(paths),
            "root_reaching_chains": len(root_paths),
            "valid_root_chains": len(valid_root),
            "active_chain_depth": (
                len(active_chain["nodes"]) if active_chain else None
            ),
        },
    }


# ---------------------------------------------------------------- 批次共同溯源核查

# 不可参与仪器的原因码
REASON_INSTRUMENT_NOT_FOUND = "INSTRUMENT_NOT_FOUND"
REASON_NO_ACTIVE_CERTIFICATE = "NO_ACTIVE_CERTIFICATE"
REASON_MULTIPLE_ACTIVE_CERTIFICATES = "MULTIPLE_ACTIVE_CERTIFICATES"
REASON_MULTIPLE_VALID_CHAINS = "MULTIPLE_VALID_CHAINS"
REASON_CHAIN_BROKEN = "CHAIN_BROKEN"
REASON_ALL_CHAINS_FAILED_CHECKS = "ALL_CHAINS_FAILED_CHECKS"
REASON_CHAIN_VALIDATION_FAILED = "CHAIN_VALIDATION_FAILED"
REASON_INSUFFICIENT_PARTICIPANTS = "INSUFFICIENT_PARTICIPANTS"
REASON_NO_COMMON_CERTIFICATE = "NO_COMMON_CERTIFICATE"

# 最近共同证书的选择规则（按优先级）
COMMON_SELECTION_RULE = [
    "各参与仪器唯一有效链到候选共同证书的距离最大值最小",
    "距离最大值相同时，距离总和最小",
    "仍相同时，取证书 ID 字典序末位",
]


def rank_common_certificates(
    chain_ids: dict[str, list[str]],
) -> list[dict[str, Any]]:
    """对可作为“最近共同证书”的候选证书排序。

    ``chain_ids`` 为参与仪器编码 -> 该仪器唯一且校验通过的链（叶子→根的
    证书 id 序列）。候选集 = 各链证书 id 集合的交集；排序规则：

    1. 各链到该证书的距离（边数）最大值最小；
    2. 最大值相同取距离总和最小；
    3. 仍相同取证书 ID 字典序末位（确定性兜底）。

    返回按当选顺序排列的候选列表，每项含 max_distance / total_distance /
    各仪器距离；``chain_ids`` 为空或交集为空时返回空列表。
    """
    shared: Optional[set[str]] = None
    for ids in chain_ids.values():
        shared = set(ids) if shared is None else shared & set(ids)
    if not shared:
        return []

    rows: list[dict[str, Any]] = []
    for cid in shared:
        distances = {inst: ids.index(cid) for inst, ids in chain_ids.items()}
        rows.append(
            {
                "certificate_id": cid,
                "distances": distances,
                "max_distance": max(distances.values()),
                "total_distance": sum(distances.values()),
            }
        )
    # 先按证书 ID 降序（字典序末位在前），再以稳定排序按距离升序，
    # 使距离完全相同的候选保留“字典序末位优先”的次序
    rows.sort(key=lambda r: r["certificate_id"], reverse=True)
    rows.sort(key=lambda r: (r["max_distance"], r["total_distance"]))
    return rows


def _sub_chain_to(chain: dict[str, Any], common_id: str) -> tuple[dict[str, Any], int]:
    """从叶子→根的完整链中截取到共同证书（含）为止的证书子链。

    返回 (子链, 距离)；距离 = 起点证书到共同证书经过的边数
    （起点证书自身距离为 0）。
    """
    ids = [n["certificate_id"] for n in chain["nodes"]]
    idx = ids.index(common_id)
    nodes = chain["nodes"][: idx + 1]
    return (
        {
            "nodes": nodes,
            "edges": chain["edges"][:idx],
            "reaches_national_standard": bool(nodes[-1]["is_national_standard"]),
            "cycle": False,
            "errors": 0,
            "warnings": 0,
        },
        idx,
    )


def _nonparticipation(
    code: str, report: dict[str, Any], at_s: str
) -> Optional[tuple[str, str]]:
    """判定一台仪器在该时点不可参与共同核查的原因；可参与返回 None。

    仅当该仪器在该时点的链路报告为“唯一成链（resolution=unique）且
    零 ERROR”时才允许参与；多候选链绝不任意选取其中一条。
    """
    if not report["starts"]:
        return (
            REASON_NO_ACTIVE_CERTIFICATE,
            f"仪器 {code} 在查询时点 {at_s} 没有处于有效区间的校准证书，"
            "溯源链无法开始",
        )
    if len(report["starts"]) > 1:
        return (
            REASON_MULTIPLE_ACTIVE_CERTIFICATES,
            f"仪器 {code} 在查询时点 {at_s} 同时存在 {len(report['starts'])} 张"
            f"有效证书 {report['starts']}，溯源起点不唯一，不得任意选取其一参与",
        )
    if report["resolution"] == "ambiguous":
        return (
            REASON_MULTIPLE_VALID_CHAINS,
            f"仪器 {code} 在查询时点 {at_s} 存在 "
            f"{report['statistics'].get('valid_root_chains', 0)} 条均到达国家基准且"
            "通过校验的候选链，链不唯一，不得任意选取一条参与共同核查",
        )
    if report["resolution"] == "broken":
        if report["statistics"].get("root_reaching_chains", 0) > 0:
            return (
                REASON_ALL_CHAINS_FAILED_CHECKS,
                f"仪器 {code} 在查询时点 {at_s} 虽有到达国家基准的候选链，"
                "但均未通过校验（签名/区间/不确定度），无有效链可参与",
            )
        return (
            REASON_CHAIN_BROKEN,
            f"仪器 {code} 在查询时点 {at_s} 的全部候选分支均在到达国家基准前"
            "终止（死路/断档/环），无有效链可参与",
        )
    # resolution == "unique"
    if report["error_count"] > 0:
        return (
            REASON_CHAIN_VALIDATION_FAILED,
            f"仪器 {code} 的唯一证书链存在 {report['error_count']} 个校验错误，"
            "链未校验通过，不得参与共同核查",
        )
    return None


def unique_chain_gate(
    conn: sqlite3.Connection,
    instrument_code: str,
    at: Optional[datetime] = None,
) -> dict[str, Any]:
    """在指定时点要求该仪器存在“唯一且校验通过”的溯源链。

    供校准工位预约等需要确定性溯源保证的业务复用：仅当报告
    ``resolution=unique`` 且零 ERROR（即 ``valid=true``）时放行，
    返回起点证书 id 与其有效期截止 ``valid_until``；其余情况
    （仪器存在但无有效起点/起点不唯一/多候选/断裂/校验失败）
    返回 ``(reason_code, reason, report)``，由调用方向用户说明原因。
    仪器不存在由调用方（KeyError）自行处理为 404。
    """
    at_naive = to_utc_naive(at) if at else datetime.utcnow()
    at_s = fmt(at_naive)
    cat = Catalog(conn)
    if instrument_code not in cat.instruments:
        raise KeyError(instrument_code)

    report = _build_report(cat, instrument_code, at_s)
    rejection = _nonparticipation(instrument_code, report, at_s)
    if rejection is not None:
        reason_code, reason = rejection
        return {
            "ok": False,
            "at": at_s,
            "reason_code": reason_code,
            "reason": reason,
            "report": report,
        }
    start_node = report["active_chain"]["nodes"][0]
    start_cert_id = start_node["certificate_id"]
    return {
        "ok": True,
        "at": at_s,
        "reason_code": None,
        "reason": None,
        "report": report,
        "start_certificate_id": start_cert_id,
        "certificate_valid_until": start_node["valid_until"],
    }


def build_common_trace_report(
    conn: sqlite3.Connection,
    at: Optional[datetime],
    instrument_codes: list[str],
) -> dict[str, Any]:
    """批次共同溯源核查：对多台仪器在同一 UTC 时点的唯一有效链求共同证书。

    复用现有单仪器链路报告（``_build_report``）：仅当仪器在该时点的报告为
    唯一成链且零 ERROR 时，该仪器才以其唯一链参与比较；其余仪器逐台给出
    不可参与原因（多候选绝不任取一条）。对参与仪器的链证书 id 求交集，
    按“距离最大值最小 → 距离总和最小 → 证书 ID 字典序末位”选出最近共同
    证书，并返回各仪器到该证书的证书子链与距离。全程只读，不写入数据。
    """
    at_naive = to_utc_naive(at) if at else datetime.utcnow()
    at_s = fmt(at_naive)

    cat = Catalog(conn)

    reports: dict[str, dict[str, Any]] = {}
    nonparticipants: list[dict[str, Any]] = []
    chain_ids: dict[str, list[str]] = {}
    start_ids: dict[str, str] = {}

    for code in instrument_codes:
        if code not in cat.instruments:
            nonparticipants.append(
                {
                    "instrument": code,
                    "reason_code": REASON_INSTRUMENT_NOT_FOUND,
                    "reason": f"仪器 {code} 不存在，无法参与共同溯源核查",
                    "resolution": None,
                    "valid": False,
                    "starts": [],
                    "conflicts": [],
                    "error_count": None,
                    "warning_count": None,
                }
            )
            continue

        report = _build_report(cat, code, at_s)
        reports[code] = report
        rejection = _nonparticipation(code, report, at_s)
        if rejection is not None:
            reason_code, reason = rejection
            nonparticipants.append(
                {
                    "instrument": code,
                    "reason_code": reason_code,
                    "reason": reason,
                    "resolution": report["resolution"],
                    "valid": report["valid"],
                    "starts": report["starts"],
                    "conflicts": report["conflicts"],
                    "error_count": report["error_count"],
                    "warning_count": report["warning_count"],
                }
            )
            continue

        active = report["active_chain"]
        chain_ids[code] = [n["certificate_id"] for n in active["nodes"]]
        start_ids[code] = chain_ids[code][0]

    participants = list(chain_ids)
    participants_set = set(participants)

    # 最近共同证书选举（至少 2 台仪器参与才有意义）
    ranked: list[dict[str, Any]] = []
    common_id: Optional[str] = None
    common_node: Optional[dict[str, Any]] = None
    reason_code: Optional[str] = None
    reason: Optional[str] = None

    if len(participants) < 2:
        reason_code = REASON_INSUFFICIENT_PARTICIPANTS
        reason = (
            f"仅有 {len(participants)} 台仪器的唯一且校验通过的证书链可参与比较，"
            "共同溯源核查至少需要 2 台参与仪器，无法选出共同证书"
        )
    else:
        ranked = rank_common_certificates(chain_ids)
        if ranked:
            common_id = ranked[0]["certificate_id"]
            ranked[0]["selected"] = True
            for row in ranked[1:]:
                row["selected"] = False
            # 同一证书在各链视图中字段一致，从任一参与链取节点详情即可
            first_chain = reports[participants[0]]["active_chain"]
            common_node = next(
                n
                for n in first_chain["nodes"]
                if n["certificate_id"] == common_id
            )
        else:
            reason_code = REASON_NO_COMMON_CERTIFICATE
            reason = (
                f"参与比较的 {len(participants)} 台仪器"
                f"（{participants}）的唯一有效证书链不含任何共同证书，"
                "证书 id 交集为空，无共同溯源证书"
            )

    # 各仪器到共同证书的子链与距离
    instrument_chains: list[dict[str, Any]] = []
    for code in instrument_codes:
        if code in participants_set and common_id is not None:
            sub_chain, distance = _sub_chain_to(
                reports[code]["active_chain"], common_id
            )
            instrument_chains.append(
                {
                    "instrument": code,
                    "participated": True,
                    "start_certificate_id": start_ids[code],
                    "chain_certificate_ids": chain_ids[code],
                    "sub_chain": sub_chain,
                    "distance": distance,
                    "nonparticipation_reason_code": None,
                    "nonparticipation_reason": None,
                }
            )
        elif code in participants_set:
            # 参与了比较但链证书集合无交集（无共同证书），无子链可给
            instrument_chains.append(
                {
                    "instrument": code,
                    "participated": True,
                    "start_certificate_id": start_ids[code],
                    "chain_certificate_ids": chain_ids[code],
                    "sub_chain": None,
                    "distance": None,
                    "nonparticipation_reason_code": None,
                    "nonparticipation_reason": None,
                }
            )
        else:
            np_row = next(
                item for item in nonparticipants if item["instrument"] == code
            )
            instrument_chains.append(
                {
                    "instrument": code,
                    "participated": False,
                    "start_certificate_id": None,
                    "chain_certificate_ids": [],
                    "sub_chain": None,
                    "distance": None,
                    "nonparticipation_reason_code": np_row["reason_code"],
                    "nonparticipation_reason": np_row["reason"],
                }
            )

    candidates = [
        {
            "certificate_id": row["certificate_id"],
            "max_distance": row["max_distance"],
            "total_distance": row["total_distance"],
            "distances": [
                {"instrument": inst, "distance": row["distances"][inst]}
                for inst in participants
            ],
            "selected": row.get("selected", False),
        }
        for row in ranked
    ]

    return {
        "at": at_s,
        "dry_run": True,
        "instrument_codes": list(instrument_codes),
        "participant_count": len(participants),
        "nonparticipant_count": len(nonparticipants),
        "participants": participants,
        "nonparticipants": nonparticipants,
        "common_certificate_found": common_id is not None,
        "common_certificate_id": common_id,
        "common_certificate": common_node,
        "reason_code": reason_code,
        "reason": reason,
        "selection_rule": COMMON_SELECTION_RULE if ranked else [],
        "candidates": candidates,
        "instrument_chains": instrument_chains,
        "statistics": {
            "instruments_total": len(cat.instruments),
            "certificates_total": len(cat.certs),
            "edges_total": len(cat.edges),
            "requested_instrument_count": len(instrument_codes),
            "participant_count": len(participants),
            "nonparticipant_count": len(nonparticipants),
            "reports_evaluated": len(reports),
            "common_certificate_candidate_count": len(ranked),
        },
    }


# ---------------------------------------------------------------- 测量相容性评估（En 数）

# 起点证书单位与目标单位不符时的不可参与原因码
REASON_CERTIFICATE_UNIT_MISMATCH = "CERTIFICATE_UNIT_MISMATCH"

# En 判定规则标识
EN_RULE_RATIO = "en_le_1"
EN_RULE_ZERO_UNCERTAINTY = "zero_uncertainty_equality"


def _en_score(
    xi: float, xj: float, ui: float, uj: float
) -> tuple[Optional[float], bool, str]:
    """计算一对读数的 En 数与相容判定。

    En = |x_i - x_j| / sqrt(U_i^2 + U_j^2)，En <= 1 判为相容；
    两个不确定度均为 0 时分母为 0、En 无定义（返回 None），
    此时仅当两读数相等才相容。
    """
    difference = abs(xi - xj)
    denominator = math.sqrt(ui * ui + uj * uj)
    if denominator == 0:
        return None, difference == 0, EN_RULE_ZERO_UNCERTAINTY
    en = difference / denominator
    return en, en <= 1.0, EN_RULE_RATIO


def maximal_compatible_groups(
    instrument_codes: list[str], compatible_pairs: list[tuple[str, str]]
) -> list[list[str]]:
    """由相容对集合求全部“人数 ≥ 2”的极大两两相容分组（极大团）。

    分组可重叠（同一仪器可属于多个分组）；严禁用连通分量代替——
    连通分量会把两两并不相容的仪器并入同一组（如 A-B、B-C 相容而
    A-C 不相容时，{A,B,C} 不是两两相容分组）。采用 Bron–Kerbosch
    （带枢轴）枚举全部极大团；组内成员与分组列表均按仪器编码
    字典序排列，保证结果确定、稳定。
    """
    adjacency: dict[str, set[str]] = {c: set() for c in instrument_codes}
    for a, b in compatible_pairs:
        adjacency[a].add(b)
        adjacency[b].add(a)

    cliques: list[list[str]] = []

    def bron_kerbosch(r: set[str], p: set[str], x: set[str]) -> None:
        if not p and not x:
            cliques.append(sorted(r))
            return
        # 枢轴优化：从 p ∪ x 中选取在 p 内邻居最多的顶点，减少递归分支
        pivot = max(p | x, key=lambda u: len(adjacency[u] & p))
        for v in sorted(p - adjacency[pivot]):
            bron_kerbosch(r | {v}, p & adjacency[v], x & adjacency[v])
            p.discard(v)
            x.add(v)

    bron_kerbosch(set(), set(instrument_codes), set())
    groups = [c for c in cliques if len(c) >= 2]
    groups.sort()
    return groups


def build_measurement_compatibility_report(
    conn: sqlite3.Connection,
    at: Optional[datetime],
    target_unit: str,
    readings: list[tuple[str, float]],
) -> dict[str, Any]:
    """多仪器读数相容性评估（En 数，只读，不写库）。

    逐条读数复用现有单仪器链路报告：仅以该时点**唯一且校验通过**
    （resolution=unique 且零 ERROR）的溯源链参与，取其**起点证书**的
    扩展不确定度作为该读数的 U；链不可用（多候选绝不任取一条）或
    起点证书单位与目标单位不符的读数逐条给出原因、不参与计算。
    对可参与读数两两计算 En 并判定相容性，再以极大团（而非连通分量）
    给出全部人数 ≥ 2 的极大两两相容分组（可重叠，按仪器编码稳定排序）。
    """
    at_naive = to_utc_naive(at) if at else datetime.utcnow()
    at_s = fmt(at_naive)
    unit = target_unit.strip()
    cat = Catalog(conn)

    reading_results: list[dict[str, Any]] = []
    nonparticipants: list[dict[str, Any]] = []
    participants: list[str] = []
    accepted: dict[str, dict[str, Any]] = {}
    reports_evaluated = 0

    for code, value in readings:
        result: dict[str, Any] = {
            "instrument": code,
            "value": value,
            "participated": False,
            "start_certificate_id": None,
            "certificate_unit": None,
            "uncertainty": None,
            "nonparticipation_reason_code": None,
            "nonparticipation_reason": None,
        }
        np_detail: Optional[dict[str, Any]] = None

        if code not in cat.instruments:
            np_detail = {
                "instrument": code,
                "value": value,
                "reason_code": REASON_INSTRUMENT_NOT_FOUND,
                "reason": f"仪器 {code} 不存在，无法参与测量相容性评估",
                "resolution": None,
                "valid": False,
                "starts": [],
                "conflicts": [],
                "start_certificate_id": None,
                "certificate_unit": None,
                "error_count": None,
                "warning_count": None,
            }
        else:
            report = _build_report(cat, code, at_s)
            reports_evaluated += 1
            rejection = _nonparticipation(code, report, at_s)
            if rejection is not None:
                reason_code, reason = rejection
                np_detail = {
                    "instrument": code,
                    "value": value,
                    "reason_code": reason_code,
                    "reason": reason,
                    "resolution": report["resolution"],
                    "valid": report["valid"],
                    "starts": report["starts"],
                    "conflicts": report["conflicts"],
                    "start_certificate_id": None,
                    "certificate_unit": None,
                    "error_count": report["error_count"],
                    "warning_count": report["warning_count"],
                }
            else:
                start_node = report["active_chain"]["nodes"][0]
                cert_unit = (start_node["unit"] or "").strip()
                if cert_unit != unit:
                    np_detail = {
                        "instrument": code,
                        "value": value,
                        "reason_code": REASON_CERTIFICATE_UNIT_MISMATCH,
                        "reason": (
                            f"仪器 {code} 唯一有效链起点证书 "
                            f"{start_node['certificate_id']} 的不确定度单位 "
                            f"'{cert_unit}' 与目标单位 '{unit}' 不符，"
                            "该读数不参与 En 计算"
                        ),
                        "resolution": report["resolution"],
                        "valid": report["valid"],
                        "starts": report["starts"],
                        "conflicts": report["conflicts"],
                        "start_certificate_id": start_node["certificate_id"],
                        "certificate_unit": cert_unit,
                        "error_count": report["error_count"],
                        "warning_count": report["warning_count"],
                    }
                    result["start_certificate_id"] = start_node["certificate_id"]
                    result["certificate_unit"] = cert_unit
                else:
                    participants.append(code)
                    accepted[code] = {
                        "value": value,
                        "uncertainty": start_node["uncertainty"],
                    }
                    result.update(
                        {
                            "participated": True,
                            "start_certificate_id": start_node["certificate_id"],
                            "certificate_unit": cert_unit,
                            "uncertainty": start_node["uncertainty"],
                        }
                    )

        if np_detail is not None:
            result["nonparticipation_reason_code"] = np_detail["reason_code"]
            result["nonparticipation_reason"] = np_detail["reason"]
            nonparticipants.append(np_detail)
        reading_results.append(result)

    # 两两 En 计算（按仪器编码排序，保证输出稳定）
    pairs: list[dict[str, Any]] = []
    compatible_pairs: list[tuple[str, str]] = []
    for a, b in combinations(sorted(participants), 2):
        da, db = accepted[a], accepted[b]
        en, compatible, rule = _en_score(
            da["value"], db["value"], da["uncertainty"], db["uncertainty"]
        )
        pairs.append(
            {
                "instrument_a": a,
                "instrument_b": b,
                "value_a": da["value"],
                "value_b": db["value"],
                "uncertainty_a": da["uncertainty"],
                "uncertainty_b": db["uncertainty"],
                "difference": abs(da["value"] - db["value"]),
                "en": en,
                "compatible": compatible,
                "rule": rule,
            }
        )
        if compatible:
            compatible_pairs.append((a, b))

    groups = [
        {"members": members, "size": len(members)}
        for members in maximal_compatible_groups(
            sorted(participants), compatible_pairs
        )
    ]

    if len(participants) < 2:
        reason_code: Optional[str] = REASON_INSUFFICIENT_PARTICIPANTS
        reason: Optional[str] = (
            f"仅有 {len(participants)} 条读数可参与计算（要求溯源链唯一且"
            "校验通过、起点证书单位与目标单位一致），测量相容性评估至少需要 "
            "2 条可参与读数，无法进行两两 En 计算与相容分组"
        )
    else:
        reason_code = None
        reason = None

    return {
        "at": at_s,
        "dry_run": True,
        "target_unit": unit,
        "reading_count": len(readings),
        "participant_count": len(participants),
        "nonparticipant_count": len(nonparticipants),
        "participants": participants,
        "nonparticipants": nonparticipants,
        "readings": reading_results,
        "pairs": pairs,
        "groups": groups,
        "reason_code": reason_code,
        "reason": reason,
        "statistics": {
            "instruments_total": len(cat.instruments),
            "certificates_total": len(cat.certs),
            "edges_total": len(cat.edges),
            "requested_reading_count": len(readings),
            "participant_count": len(participants),
            "nonparticipant_count": len(nonparticipants),
            "reports_evaluated": reports_evaluated,
            "pair_count": len(pairs),
            "compatible_pair_count": len(compatible_pairs),
            "group_count": len(groups),
        },
    }


# ---------------------------------------------------------------- 证书停用影响评估

def _catalog_without_certificates(
    cat: Catalog, disabled_ids: set[str]
) -> Catalog:
    """在内存中模拟移除指定证书及其全部关联溯源边，不写入数据库。"""
    kept_certs = {
        cid: c for cid, c in cat.certs.items() if cid not in disabled_ids
    }
    kept_edges = [
        e
        for e in cat.edges
        if e["child_cert_id"] not in disabled_ids
        and e["parent_cert_id"] not in disabled_ids
    ]
    return Catalog(
        instruments=dict(cat.instruments),
        certs=kept_certs,
        edges=kept_edges,
    )


def _chain_fingerprint(chain: Optional[dict[str, Any]]) -> Any:
    """链结构指纹：节点序列 + 到基准情况；None 表示不存在链。"""
    if chain is None:
        return None
    return (
        tuple(n["certificate_id"] for n in chain["nodes"]),
        bool(chain["reaches_national_standard"]),
    )


def _chain_prints(report: dict[str, Any]) -> list[Any]:
    """报告中全部链（唯一链 + 候选链 + 断裂分支）的结构指纹集合。"""
    return [
        _chain_fingerprint(report["active_chain"]),
        *[_chain_fingerprint(c) for c in report["candidates"]],
        *[_chain_fingerprint(c) for c in report["broken_chains"]],
    ]


def _snapshot(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "resolution": report["resolution"],
        "valid": report["valid"],
        "starts": report["starts"],
        "active_chain": report["active_chain"],
        "candidates": report["candidates"],
        "broken_chains": report["broken_chains"],
        "conflicts": report["conflicts"],
        "error_count": report["error_count"],
        "warning_count": report["warning_count"],
        "statistics": report["statistics"],
    }


def _chain_summary(chain: dict[str, Any], disabled_ids: set[str], role: str) -> dict[str, Any]:
    cert_ids = [n["certificate_id"] for n in chain["nodes"]]
    return {
        "role": role,
        "certificate_ids": cert_ids,
        "node_count": len(cert_ids),
        "reaches_national_standard": bool(chain["reaches_national_standard"]),
        "contains_disabled_certificate": any(
            cid in disabled_ids for cid in cert_ids
        ),
        "disabled_certificates_on_path": [
            cid for cid in cert_ids if cid in disabled_ids
        ],
        "errors": chain["errors"],
        "warnings": chain["warnings"],
        "chain": chain,
    }


def assess_deactivation(
    conn: sqlite3.Connection,
    at: Optional[datetime],
    disabled_certificate_ids: list[str],
    instrument_codes: Optional[list[str]] = None,
) -> dict[str, Any]:
    """模拟停用证书：在内存中移除证书及其溯源边，重新评估受影响仪器。

    该过程只读数据库并在内存快照上重算，绝不执行任何写入。
    """
    at_naive = to_utc_naive(at) if at else datetime.utcnow()
    at_s = fmt(at_naive)

    # 去空白、去重并保持提交顺序
    disabled = list(dict.fromkeys(cid.strip() for cid in disabled_certificate_ids))
    base = Catalog(conn)
    unknown = sorted(cid for cid in disabled if cid not in base.certs)
    if unknown:
        raise ValidationError(f"待停用证书不存在: {unknown}")

    scope: Optional[list[str]] = None
    if instrument_codes is not None:
        scope = list(dict.fromkeys(c.strip() for c in instrument_codes))
        missing_inst = sorted(c for c in scope if c not in base.instruments)
        if missing_inst:
            raise ValidationError(f"筛选仪器不存在: {missing_inst}")

    disabled_set = set(disabled)
    simulated = _catalog_without_certificates(base, disabled_set)

    # 受影响候选仪器：被停用证书所属仪器 + 停用前任一溯源链经过被停用证书的仪器
    touched: set[str] = {
        base.certs[cid]["instrument_code"] for cid in disabled
    }
    if scope is None:
        for code in sorted(base.instruments):
            starts = _active_starts(base, code, at_s)
            paths, _truncated = _enumerate_paths(base, starts, at_s)
            if any(
                cid in disabled_set
                for p in paths
                for cid in p["cert_ids"]
            ):
                touched.add(code)
        targets = sorted(touched)
    else:
        targets = scope

    results: list[dict[str, Any]] = []
    transition_counts: dict[str, int] = {}

    for code in targets:
        before = _build_report(base, code, at_s)
        after = _build_report(simulated, code, at_s)

        removed_starts = sorted(set(before["starts"]) & disabled_set)
        severed = []
        role_label = {"active": "唯一有效链", "candidate": "候选链", "broken": "断裂分支"}
        for chain, role in (
            (before["active_chain"], "active"),
            *[(c, "candidate") for c in before["candidates"]],
            *[(c, "broken") for c in before["broken_chains"]],
        ):
            if chain is None:
                continue
            summary = _chain_summary(chain, disabled_set, role)
            if summary["contains_disabled_certificate"]:
                severed.append(summary)

        # 受影响判定：状态翻转 / 链集合结构变化 / 起点证书被停用 / 原链被切断
        impacted = (
            before["resolution"] != after["resolution"]
            or _chain_prints(before) != _chain_prints(after)
            or bool(removed_starts)
            or bool(severed)
        )

        if before["resolution"] == after["resolution"]:
            transition = "unchanged"
        else:
            transition = f"{before['resolution']}_to_{after['resolution']}"
        transition_counts[transition] = transition_counts.get(transition, 0) + 1

        notes: list[dict[str, Any]] = []
        for cid in removed_starts:
            notes.append(
                _conflict(
                    "SIMULATED_START_CERTIFICATE_REMOVED",
                    "INFO",
                    f"仪器 {code} 在时点 {at_s} 的有效起点证书 {cid} 在停用清单中，"
                    "模拟移除后该起点不再可用",
                    node=cid,
                    instrument=code,
                    detail={"certificate_id": cid},
                )
            )
        for s in severed:
            notes.append(
                _conflict(
                    "SIMULATED_CHAIN_SEVERED",
                    "INFO",
                    f"停用前{role_label[s['role']]} "
                    f"{' -> '.join(s['certificate_ids'])} 途经被停用证书"
                    f" {s['disabled_certificates_on_path']}，该链在模拟后不复存在",
                    instrument=code,
                    detail={
                        "role": s["role"],
                        "certificate_ids": s["certificate_ids"],
                        "disabled_certificates_on_path":
                            s["disabled_certificates_on_path"],
                    },
                )
            )

        alternative: Optional[dict[str, Any]] = None
        if after["active_chain"] is not None:
            alternative = _chain_summary(after["active_chain"], disabled_set, "active")
            notes.append(
                _conflict(
                    "SIMULATED_ALTERNATIVE_CHAIN_AVAILABLE",
                    "INFO",
                    "停用后仍存在唯一有效替代链："
                    f"{' -> '.join(alternative['certificate_ids'])}",
                    instrument=code,
                    detail={"certificate_ids": alternative["certificate_ids"]},
                )
            )
        if impacted and after["active_chain"] is None:
            note_code = {
                "broken": "SIMULATED_NO_ALTERNATIVE_CHAIN",
                "ambiguous": "SIMULATED_AMBIGUOUS_AFTER_DEACTIVATION",
            }.get(after["resolution"])
            if note_code is not None:
                notes.append(
                    _conflict(
                        note_code,
                        "INFO",
                        f"停用后该仪器状态为 {after['resolution']}，"
                        "不存在唯一有效替代链",
                        instrument=code,
                    )
                )
        if not impacted:
            notes.append(
                _conflict(
                    "SIMULATED_NO_EFFECT",
                    "INFO",
                    f"停用清单中的证书均不在仪器 {code} 于 {at_s} 的溯源路径上，"
                    "唯一链/多候选/断裂状态不发生变化",
                    instrument=code,
                )
            )

        results.append(
            {
                "instrument": code,
                "impacted": impacted,
                "transition": transition,
                "before": _snapshot(before),
                "after": _snapshot(after),
                "alternative_chain": alternative,
                "alternative_chains": [
                    _chain_summary(c, disabled_set, "candidate")
                    for c in after["candidates"]
                ],
                "severed_chains": severed,
                "simulation_notes": notes,
            }
        )

    impacted_count = sum(1 for r in results if r["impacted"])
    owners = sorted(
        {base.certs[cid]["instrument_code"] for cid in disabled}
    )

    return {
        "at": at_s,
        "dry_run": True,
        "disabled_certificate_ids": disabled,
        "unknown_certificate_ids": unknown,
        "instruments_filter": scope,
        "affected_owner_instruments": owners,
        "removed_edge_count": len(base.edges) - len(simulated.edges),
        "affected_instrument_count": impacted_count,
        "evaluated_instrument_count": len(results),
        "transition_counts": transition_counts,
        "results": results,
        "statistics": {
            "certificates_before": len(base.certs),
            "certificates_after": len(simulated.certs),
            "edges_before": len(base.edges),
            "edges_after": len(simulated.edges),
        },
    }


# ---------------------------------------------------------------- 仪器时间窗覆盖查询

def _parse_ts(s: str) -> datetime:
    """内部采样串解析（接受整秒 UTC 串及 ``Z\x7f`` 后缀的半开哨兵串）。"""
    return datetime.strptime(s.rstrip("\x7f"), _TS_PARSE_FMT)


def _collect_relevant_boundaries(
    cat: Catalog, instrument_code: str, lo_s: str, hi_s: str
) -> set[str]:
    """收集窗口内与该仪器溯源评估相关的全部有效期边界。

    范围 = 该仪器全部证书 → 沿“子→父”边闭包可达的全部证书与边
    （国家基准证书一并纳入，保证根节点检查一致）；各证书/边的
    valid_from / valid_until 落在闭查询窗内的即为边界。
    区间有效性按闭区间判定，因此边界点本身单独评估，不并入开区间。
    """
    relevant_certs: set[str] = set()
    stack = [
        cid
        for cid, c in cat.certs.items()
        if c["instrument_code"] == instrument_code
    ]
    while stack:
        cid = stack.pop()
        if cid in relevant_certs or cid not in cat.certs:
            continue
        relevant_certs.add(cid)
        cert = cat.certs[cid]
        if cat.instruments[cert["instrument_code"]]["is_national_standard"]:
            # 根节点不再向上溯源
            continue
        for e in cat.out_edges.get(cid, []):
            stack.append(e["parent_cert_id"])

    boundaries: set[str] = set()

    def clamp_add(ts: str) -> None:
        if lo_s <= ts <= hi_s:
            boundaries.add(ts)

    for cid in relevant_certs:
        c = cat.certs[cid]
        clamp_add(c["valid_from"])
        clamp_add(c["valid_until"])
        for e in cat.out_edges.get(cid, []):
            clamp_add(e["valid_from"])
            clamp_add(e["valid_until"])
    return boundaries


def _interior_moment(lo_s: str, hi_s: str) -> tuple[str, datetime]:
    """两个整秒网格点之间严格位于内部的采样时刻。

    跨度 >=2 秒取整秒中点；跨度 1 秒时取后半秒，并在内部串上追加
    ``\\x7f`` 哨兵后缀，使其字符串序严格位于整秒点 ``tZ`` 与
    ``t+1sZ`` 之间（时间存储串按字典序比较）。内部串只用于本次查询，
    既不入库也不出现在响应中（响应的 sample_at 给出真实采样时刻）。
    """
    lo_dt, hi_dt = _parse_ts(lo_s), _parse_ts(hi_s)
    mid = lo_dt + (hi_dt - lo_dt) / 2
    if (hi_dt - lo_dt) >= timedelta(seconds=2):
        sample = mid.replace(microsecond=0)
    else:
        sample = mid
    return fmt(sample) + ("\x7f" if sample.microsecond else ""), sample


def _sample_key(report: dict[str, Any]) -> tuple:
    """区段合并依据：状态 + 有效性 + 冲突码多重集（含数量，保序）。"""
    codes = tuple(sorted(c["code"] for c in report["conflicts"]))
    return (report["resolution"], report["valid"], codes)


def _display_ts(dt_value: datetime) -> str:
    """对外展示用 UTC 串（整秒为 Z 结尾，半开采样保留微秒）。"""
    base = dt_value.strftime(_TS_PARSE_FMT[:-1])  # 去掉结尾 Z
    if dt_value.microsecond:
        base += f".{dt_value.microsecond:06d}"
    return base + "Z"


def build_window_report(
    conn: sqlite3.Connection,
    instrument_code: str,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """按有效期边界把 [start, end]（UTC 闭区间）划分为状态区段。

    1. 收集与该仪器相关证书/溯源边在窗内的全部有效期边界；
    2. 边界点（闭区间端点）与相邻边界间开区间分别评估现有校验报告；
    3. 仅当状态、有效性、冲突码多重集相同且端点严格连续时才合并，
       每段显式标明 start_inclusive / end_inclusive，
       避免闭区间交接时吞掉仅存在于瞬时（边界点）的冲突。
    """
    start_naive = to_utc_naive(start)
    end_naive = to_utc_naive(end)
    if start_naive > end_naive:
        raise ValidationError(
            f"时间窗起点 {fmt(start_naive)} 不能晚于终点 {fmt(end_naive)}"
        )
    lo_s = fmt(start_naive)
    hi_s = fmt(end_naive)

    instrument = get_instrument(conn, instrument_code)
    if instrument is None:
        raise KeyError(instrument_code)

    cat = Catalog(conn)
    boundaries = _collect_relevant_boundaries(cat, instrument_code, lo_s, hi_s)
    grid = sorted({lo_s, hi_s, *boundaries})

    # 原子段：(起点, 终点, 起点是否包含, 终点是否包含, 代表报告, 真实采样时刻)
    atoms: list[tuple[str, str, bool, bool, dict[str, Any], datetime]] = []
    for idx, t in enumerate(grid):
        if lo_s <= t <= hi_s:
            atoms.append(
                (t, t, True, True, _build_report(cat, instrument_code, t),
                 _parse_ts(t))
            )
        if idx + 1 < len(grid):
            t1, t2 = grid[idx], grid[idx + 1]
            if t1 < t2 and lo_s <= t1 and t2 <= hi_s:
                inner_s, inner_dt = _interior_moment(t1, t2)
                atoms.append(
                    (t1, t2, False, False,
                     _build_report(cat, instrument_code, inner_s), inner_dt)
                )

    # 相邻且键相同才合并；端点包含性取最左/最右原子段
    merged: list[dict[str, Any]] = []
    for a_lo, a_hi, a_lo_in, a_hi_in, rep, sample_dt in atoms:
        key = _sample_key(rep)
        if merged:
            prev = merged[-1]
            contiguous = (
                prev["_key"] == key
                and prev["end"] == a_lo
                and prev["end_inclusive"] != a_lo_in
            )
            if contiguous:
                prev["end"] = a_hi
                prev["end_inclusive"] = a_hi_in
                continue
        merged.append(
            {
                "_key": key,
                "start": a_lo,
                "end": a_hi,
                "start_inclusive": a_lo_in,
                "end_inclusive": a_hi_in,
                "_rep": rep,
                "_sample_at": sample_dt,
            }
        )

    segments = []
    for m in merged:
        rep = m.pop("_rep")
        sample_dt = m.pop("_sample_at")
        m.pop("_key")
        m["resolution"] = rep["resolution"]
        m["valid"] = rep["valid"]
        m["conflict_codes"] = sorted({c["code"] for c in rep["conflicts"]})
        m["error_count"] = rep["error_count"]
        m["warning_count"] = rep["warning_count"]
        m["sample_at"] = _display_ts(sample_dt)
        m["starts"] = rep["starts"]
        m["conflicts"] = rep["conflicts"]
        m["statistics"] = rep["statistics"]
        segments.append(m)

    return {
        "instrument": instrument_code,
        "window_start": lo_s,
        "window_end": hi_s,
        "boundary_count": len(boundaries),
        "grid_points": grid,
        "segment_count": len(segments),
        "segments": segments,
        "statistics": {
            "boundaries_in_window": len(boundaries),
            "grid_points": len(grid),
            "point_samples": sum(1 for a in atoms if a[0] == a[1]),
            "interval_samples": sum(1 for a in atoms if a[0] != a[1]),
            "reports_evaluated": len(atoms),
        },
    }
