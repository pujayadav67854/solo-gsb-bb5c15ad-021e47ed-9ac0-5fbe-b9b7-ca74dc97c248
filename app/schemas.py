"""请求/响应 Pydantic 模型。时间均按 UTC 解释。"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, FiniteFloat, field_validator

from .utils import SUPPORTED_ALGS, compute_digest, fmt


# ---------- 输入 ----------

class InstrumentIn(BaseModel):
    code: str = Field(..., min_length=1, description="仪器唯一编码")
    name: str
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    serial_no: Optional[str] = None
    is_national_standard: bool = Field(
        False, description="是否为国家基准（溯源根节点）"
    )


class CertificateIn(BaseModel):
    id: str = Field(..., min_length=1)
    instrument_code: str
    cert_number: str
    issued_at: datetime
    valid_from: datetime
    valid_until: datetime
    uncertainty: float = Field(..., ge=0)
    unit: str = ""
    digest_alg: str = "sha256"
    signature_digest: Optional[str] = Field(
        None, description="证书签名摘要；留空则由服务端按规范化载荷重算"
    )
    signed_payload: Optional[str] = Field(
        None, description="被签名的原始载荷；留空则按证书字段规范化生成"
    )

    @field_validator("digest_alg")
    @classmethod
    def _check_alg(cls, v: str) -> str:
        v = v.lower()
        if v not in SUPPORTED_ALGS:
            raise ValueError(f"不支持的摘要算法: {v}")
        return v

    def normalized(self) -> dict:
        data = self.model_dump()
        data["issued_at"] = fmt(self.issued_at)
        data["valid_from"] = fmt(self.valid_from)
        data["valid_until"] = fmt(self.valid_until)
        data["digest_alg"] = self.digest_alg.lower()
        if not data["signed_payload"]:
            data["signed_payload"] = None
        if not data["signature_digest"]:
            data["signature_digest"] = None
        return data


class TraceEdgeIn(BaseModel):
    child_cert_id: str
    parent_cert_id: str
    valid_from: Optional[datetime] = Field(
        None, description="边有效起点；留空取子证书 valid_from"
    )
    valid_until: Optional[datetime] = Field(
        None, description="边有效终点；留空取子证书 valid_until"
    )
    note: str = ""


class SubmissionIn(BaseModel):
    """实验室一次提交：仪器 + 证书 + 父子溯源边（全部按编码 upsert）。"""
    instruments: List[InstrumentIn] = Field(default_factory=list)
    certificates: List[CertificateIn] = Field(default_factory=list)
    edges: List[TraceEdgeIn] = Field(default_factory=list)


class SubmissionPreflightIn(SubmissionIn):
    """批量提交预检请求：载荷与 /api/submissions 相同，另加摘要校验开关。"""

    strict_digest: bool = Field(
        False,
        description=(
            "true 时显式 signature_digest 与重算值不符按错误（ERROR）计；"
            "false（默认）时仅按警告（WARN）计，不阻断提交"
        ),
    )


class DeactivationIn(BaseModel):
    """证书停用影响评估请求（dry-run，不写入数据库）。"""
    at: Optional[datetime] = Field(
        None, description="查询时点（UTC）；留空取服务端当前时间"
    )
    certificate_ids: List[str] = Field(
        ..., min_length=1, description="待停用证书 id 列表（至少 1 张）"
    )
    instrument_codes: Optional[List[str]] = Field(
        None,
        description="仅评估指定仪器；留空则自动覆盖全部受影响仪器",
    )

    @field_validator("certificate_ids")
    @classmethod
    def _check_certificate_ids(cls, v: List[str]) -> List[str]:
        if any(not cid or not cid.strip() for cid in v):
            raise ValueError("证书 id 不能为空字符串")
        return v

    @field_validator("instrument_codes")
    @classmethod
    def _check_instrument_codes(cls, v):
        if v is not None:
            if len(v) == 0:
                raise ValueError("仪器编码筛选列表不能为空")
            if any(not c or not c.strip() for c in v):
                raise ValueError("仪器编码不能为空字符串")
        return v


class CalibrationBookingIn(BaseModel):
    """校准工位预约确认请求（起点 + 所需时长，结束时间由服务端计算）。"""

    instrument_code: str = Field(..., min_length=1, description="受校准仪器编码")
    start: datetime = Field(
        ..., description="预约开始时刻（UTC，含），如 2026-06-20T09:00:00Z"
    )
    duration_seconds: int = Field(
        ..., gt=0, description="所需时长（正整数秒）；结束 = start + duration"
    )
    note: str = ""


class CommonTraceIn(BaseModel):
    """批次共同溯源核查请求（只读，不写入数据库）。"""

    at: Optional[datetime] = Field(
        None, description="UTC 查询时点；留空取服务端当前时间"
    )
    instrument_codes: List[str] = Field(
        ...,
        min_length=2,
        description="参与核查的仪器编码列表（至少 2 台，且编码互不相同）",
    )

    @field_validator("instrument_codes")
    @classmethod
    def _check_instrument_codes(cls, v: List[str]) -> List[str]:
        if any(not c or not c.strip() for c in v):
            raise ValueError("仪器编码不能为空字符串")
        codes = [c.strip() for c in v]
        distinct = list(dict.fromkeys(codes))
        if len(distinct) < 2:
            raise ValueError("共同溯源核查至少需要 2 台不同仪器")
        if len(distinct) != len(codes):
            dup = sorted({c for c in codes if codes.count(c) > 1})
            raise ValueError(f"仪器编码不得重复: {dup}")
        return codes


class MeasurementReadingIn(BaseModel):
    """一条参与相容性评估的读数：仪器编码 + 有限测量值。"""

    instrument_code: str = Field(..., min_length=1, description="仪器编码")
    value: FiniteFloat = Field(
        ..., description="测量值（须为有限浮点数，拒绝 NaN/Inf）"
    )


class MeasurementCompatibilityIn(BaseModel):
    """测量相容性评估请求（只读，不写入数据库）。"""

    at: Optional[datetime] = Field(
        None, description="UTC 查询时点；留空取服务端当前时间"
    )
    target_unit: str = Field(
        ..., description="目标单位；与各仪器起点证书的不确定度单位精确比对"
    )
    readings: List[MeasurementReadingIn] = Field(
        ...,
        min_length=2,
        max_length=12,
        description="2～12 条读数；仪器编码互不相同，测量值须为有限浮点数",
    )

    @field_validator("target_unit")
    @classmethod
    def _strip_target_unit(cls, v: str) -> str:
        return v.strip()

    @field_validator("readings")
    @classmethod
    def _check_readings(cls, v: List[MeasurementReadingIn]):
        codes = [r.instrument_code.strip() for r in v]
        if any(not c for c in codes):
            raise ValueError("仪器编码不能为空字符串")
        if len(set(codes)) != len(codes):
            dup = sorted({c for c in codes if codes.count(c) > 1})
            raise ValueError(f"仪器编码不得重复: {dup}")
        for r, c in zip(v, codes):
            r.instrument_code = c
        return v


# ---------- 输出 ----------

class InstrumentOut(InstrumentIn):
    created_at: datetime


class CertificateOut(BaseModel):
    id: str
    instrument_code: str
    cert_number: str
    issued_at: datetime
    valid_from: datetime
    valid_until: datetime
    uncertainty: float
    unit: str
    digest_alg: str
    signature_digest: str
    signed_payload: str
    created_at: datetime


class TraceEdgeOut(BaseModel):
    id: int
    child_cert_id: str
    parent_cert_id: str
    valid_from: datetime
    valid_until: datetime
    note: str


# ---------- 证书修订历史输出 ----------

class CertificateRevisionOut(BaseModel):
    """证书某一版本的完整业务字段快照。"""

    certificate_id: str
    version: int = Field(..., ge=1, description="连续版本号，首次保存为 1")
    instrument_code: str
    cert_number: str
    issued_at: datetime
    valid_from: datetime
    valid_until: datetime
    uncertainty: float
    unit: str
    digest_alg: str
    signature_digest: str
    signed_payload: str
    recorded_at: datetime = Field(..., description="该版本被记录的时刻（UTC）")


class CertificateRevisionListOut(BaseModel):
    certificate_id: str
    revision_count: int
    revisions: List[CertificateRevisionOut]


class CertificateRevisionDiffOut(BaseModel):
    certificate_id: str
    from_version: int
    to_version: int
    changed_fields: List[str] = Field(
        ..., description="两版之间发生变化的业务字段（按固定字段序列出）"
    )
    changes: dict = Field(
        ..., description='逐字段差异：{字段: {"from": 旧值, "to": 新值}}'
    )


class SubmissionResult(BaseModel):
    instruments_upserted: List[str]
    certificates_upserted: List[str]
    edges_upserted: List[int]


class PreflightIssue(BaseModel):
    """预检发现的单条错误/警告，可精确定位到提交载荷中的字段路径。"""

    path: str = Field(
        ...,
        description=(
            "稳定的字段路径（JSON Pointer 风格），如 "
            "certificates/0/instrument_code、edges/2/parent_cert_id"
        ),
    )
    code: str = Field(..., description="机器可读错误码")
    message: str = Field(..., description="中文原因说明")
    detail: dict = Field(default_factory=dict, description="结构化证据")


class PreflightUpsertCount(BaseModel):
    instruments: int
    certificates: int
    edges: int
    total: int


class SubmissionPreflightResult(BaseModel):
    strict_digest: bool
    dry_run: bool = Field(
        ..., description="恒为 true：预检在独立事务中模拟并整体回滚，不写库"
    )
    ok: bool = Field(
        ..., description="errors 为空时为 true（警告不影响 ok）"
    )
    error_count: int
    warning_count: int
    errors: List[PreflightIssue] = Field(
        default_factory=list, description="稳定排序后的错误列表"
    )
    warnings: List[PreflightIssue] = Field(
        default_factory=list, description="稳定排序后的警告列表"
    )
    would_upsert: PreflightUpsertCount = Field(
        ..., description="若按当前载荷正式提交，将执行 upsert 的条目数量"
    )


class Conflict(BaseModel):
    code: str
    severity: str = Field(..., description="ERROR | WARN")
    node: Optional[str] = Field(None, description="冲突所在证书 id（若适用）")
    instrument: Optional[str] = Field(None, description="冲突所在仪器编码（若适用）")
    path_index: Optional[int] = Field(
        None, description="冲突所属候选链下标（节点级检查为 None）"
    )
    message: str
    detail: dict = Field(default_factory=dict)


class ChainNode(BaseModel):
    certificate_id: str
    cert_number: str
    instrument_code: str
    instrument_name: str
    is_national_standard: bool
    valid_from: datetime
    valid_until: datetime
    issued_at: datetime
    uncertainty: float
    unit: str


class ChainEdge(BaseModel):
    edge_id: int
    child_cert_id: str
    parent_cert_id: str
    valid_from: datetime
    valid_until: datetime
    note: str


class Chain(BaseModel):
    nodes: List[ChainNode]
    edges: List[ChainEdge]
    reaches_national_standard: bool
    cycle: bool
    errors: int = 0
    warnings: int = 0


class TraceReport(BaseModel):
    instrument: str
    at: datetime
    resolution: str = Field(
        ..., description="unique | ambiguous | broken（唯一/多候选/断裂）"
    )
    valid: bool = Field(..., description="唯一成链且无 ERROR 级问题时为 true")
    starts: List[str]
    active_chain: Optional[Chain] = None
    candidates: List[Chain] = Field(
        default_factory=list, description="到达国家基准的候选链（ambiguous 时 >=2）"
    )
    broken_chains: List[Chain] = Field(
        default_factory=list, description="未能到达国家基准的部分链"
    )
    conflicts: List[Conflict] = Field(default_factory=list)
    error_count: int = 0
    warning_count: int = 0
    statistics: dict = Field(default_factory=dict)


# ---------- 证书停用影响评估输出 ----------

class ChainSummary(BaseModel):
    role: str = Field(
        ...,
        description="链在评估中的角色：active=唯一有效链 | candidate=候选链 | broken=断裂分支",
    )
    certificate_ids: List[str]
    node_count: int
    reaches_national_standard: bool
    contains_disabled_certificate: bool
    disabled_certificates_on_path: List[str]
    errors: int = 0
    warnings: int = 0
    chain: Chain = Field(..., description="完整链（节点 + 边）摘要")


class TraceStateSnapshot(BaseModel):
    """停用前/后任一时刻的完整评估快照（与 TraceReport 同构）。"""
    resolution: str
    valid: bool
    starts: List[str]
    active_chain: Optional[Chain] = None
    candidates: List[Chain] = Field(default_factory=list)
    broken_chains: List[Chain] = Field(default_factory=list)
    conflicts: List[Conflict] = Field(default_factory=list)
    error_count: int = 0
    warning_count: int = 0
    statistics: dict = Field(default_factory=dict)


class InstrumentDeactivationResult(BaseModel):
    instrument: str
    impacted: bool = Field(..., description="停用前后评估结果是否发生变化")
    transition: str = Field(
        ...,
        description=(
            "状态迁移：unchanged | unique_to_broken | unique_to_ambiguous | "
            "ambiguous_to_unique | ambiguous_to_broken | broken_to_unique | "
            "broken_to_ambiguous"
        ),
    )
    before: TraceStateSnapshot = Field(..., description="停用前状态")
    after: TraceStateSnapshot = Field(..., description="停用后（模拟）状态")
    alternative_chain: Optional[ChainSummary] = Field(
        None, description="停用后的唯一有效替代链（若存在）"
    )
    alternative_chains: List[ChainSummary] = Field(
        default_factory=list,
        description="停用后仍存在的全部到基准候选链（多候选时 >=2）",
    )
    severed_chains: List[ChainSummary] = Field(
        default_factory=list,
        description="停用前存在、因途经被停用证书而被切断的链",
    )
    simulation_notes: List[Conflict] = Field(
        default_factory=list,
        description="模拟过程的可解释说明（severity=INFO，非真实数据冲突）",
    )


# ---------- 仪器时间窗覆盖查询输出 ----------

class CoverageSegment(BaseModel):
    """时间窗内状态恒定的一个区段（按起点升序，互不重叠，覆盖整个查询窗）。"""

    start: datetime = Field(..., description="区段起点（UTC）")
    end: datetime = Field(..., description="区段终点（UTC）")
    start_inclusive: bool = Field(
        ..., description="是否包含起点；与相邻区段在边界点上恰好一含一不含"
    )
    end_inclusive: bool = Field(
        ..., description="是否包含终点；与相邻区段在边界点上恰好一含一不含"
    )
    resolution: str = Field(
        ..., description="unique | ambiguous | broken（与 /api/reports 同义）"
    )
    valid: bool = Field(..., description="该区段代表采样点上唯一成链且无 ERROR")
    conflict_codes: List[str] = Field(
        default_factory=list,
        description="该区段全部冲突码（去重排序）；合并时按冲突码多重集判定",
    )
    error_count: int
    warning_count: int
    sample_at: datetime = Field(
        ...,
        description=(
            "该区段代表评估点：点区段为该边界点本身；"
            "开区间区段为两端点之间的内部采样时刻（整秒中点或半秒）"
        ),
    )
    starts: List[str] = Field(
        default_factory=list, description="代表采样点上该仪器的有效起点证书"
    )
    conflicts: List[Conflict] = Field(
        default_factory=list, description="代表采样点的完整可解释冲突明细"
    )
    statistics: dict = Field(default_factory=dict)


class CoverageWindowReport(BaseModel):
    instrument: str
    window_start: datetime = Field(..., description="查询窗起点（UTC，含）")
    window_end: datetime = Field(..., description="查询窗终点（UTC，含）")
    boundary_count: int = Field(
        ..., description="窗内相关证书/溯源边有效期边界点数量（不含窗口端点）"
    )
    grid_points: List[datetime] = Field(
        default_factory=list,
        description="窗口端点与窗内边界点合并去重、升序排列后的完整网格",
    )
    segment_count: int
    segments: List[CoverageSegment] = Field(
        default_factory=list,
        description=(
            "按时间排列的状态区段；仅当 resolution/valid/冲突码多重集相同"
            "且端点严格连续（一开一闭相接）时合并，瞬时冲突独占点区段"
        ),
    )
    statistics: dict = Field(default_factory=dict)


class DeactivationImpactReport(BaseModel):
    at: datetime
    dry_run: bool = Field(
        ..., description="恒为 true：模拟不写入数据库"
    )
    disabled_certificate_ids: List[str]
    unknown_certificate_ids: List[str] = Field(
        default_factory=list, description="不存在于库中的待停用证书 id"
    )
    instruments_filter: Optional[List[str]] = Field(
        None, description="请求指定的仪器筛选范围；未筛选为 null"
    )
    affected_owner_instruments: List[str] = Field(
        default_factory=list, description="被停用证书直接所属的仪器编码"
    )
    removed_edge_count: int = Field(
        ..., description="模拟中一并移除的溯源边数量"
    )
    affected_instrument_count: int
    evaluated_instrument_count: int
    transition_counts: dict = Field(
        default_factory=dict, description="按状态迁移类型计数"
    )
    results: List[InstrumentDeactivationResult]
    statistics: dict = Field(default_factory=dict)


# ---------- 批次共同溯源核查输出 ----------

class CommonTraceNonparticipant(BaseModel):
    """一台不可参与共同比较的仪器及其原因。"""

    instrument: str
    reason_code: str = Field(
        ...,
        description=(
            "不可参与原因码：INSTRUMENT_NOT_FOUND | NO_ACTIVE_CERTIFICATE | "
            "MULTIPLE_ACTIVE_CERTIFICATES | MULTIPLE_VALID_CHAINS | "
            "CHAIN_BROKEN | ALL_CHAINS_FAILED_CHECKS | CHAIN_VALIDATION_FAILED"
        ),
    )
    reason: str = Field(..., description="中文原因说明")
    resolution: Optional[str] = Field(
        None, description="该时点单仪器报告的 resolution（仪器不存在时为 null）"
    )
    valid: bool
    starts: List[str] = Field(
        default_factory=list, description="该时点有效的起点证书 id"
    )
    conflicts: List[Conflict] = Field(
        default_factory=list,
        description="该时点单仪器报告的完整可解释冲突明细（结构同 /api/reports）",
    )
    error_count: Optional[int] = None
    warning_count: Optional[int] = None


class CommonCandidateDistance(BaseModel):
    instrument: str
    distance: int = Field(
        ..., description="该仪器的唯一链从起点证书到候选共同证书经过的边数"
    )


class CommonCertificateCandidate(BaseModel):
    """最近共同证书的一个候选（参与仪器链证书集合的交集成员）。"""

    certificate_id: str
    max_distance: int = Field(..., description="各仪器到该证书距离的最大值")
    total_distance: int = Field(..., description="各仪器到该证书距离的总和")
    distances: List[CommonCandidateDistance] = Field(
        ..., description="各参与仪器到该证书的距离"
    )
    selected: bool = Field(..., description="是否为最终选出的最近共同证书")


class InstrumentCommonChain(BaseModel):
    """单台仪器在共同核查中的结果：参与则给子链与距离，否则给不可参与原因。"""

    instrument: str
    participated: bool = Field(
        ..., description="该仪器是否以唯一且校验通过的证书链参与了比较"
    )
    start_certificate_id: Optional[str] = Field(
        None, description="该仪器在查询时点的唯一有效起点证书 id"
    )
    chain_certificate_ids: List[str] = Field(
        default_factory=list,
        description="该仪器完整的唯一有效链（叶子→根的证书 id 序列）",
    )
    sub_chain: Optional[Chain] = Field(
        None, description="从该仪器起点证书到共同证书（含）为止的证书子链"
    )
    distance: Optional[int] = Field(
        None, description="起点证书到共同证书经过的边数（起点证书自身为 0）"
    )
    nonparticipation_reason_code: Optional[str] = None
    nonparticipation_reason: Optional[str] = None


class CommonTraceReport(BaseModel):
    at: datetime
    dry_run: bool = Field(
        ..., description="恒为 true：共同核查为只读计算，不写入业务数据"
    )
    instrument_codes: List[str] = Field(
        ..., description="请求提交的仪器编码（按提交顺序、已去空白）"
    )
    participant_count: int
    nonparticipant_count: int
    participants: List[str] = Field(
        default_factory=list,
        description="实际以唯一且校验通过的证书链参与比较的仪器编码",
    )
    nonparticipants: List[CommonTraceNonparticipant] = Field(
        default_factory=list,
        description="逐台给出的不可参与仪器及原因（多候选链不任取一条）",
    )
    common_certificate_found: bool = Field(
        ..., description="参与仪器的链证书 id 交集是否非空"
    )
    common_certificate_id: Optional[str] = Field(
        None, description="选出的最近共同证书 id；无交集时为 null"
    )
    common_certificate: Optional[ChainNode] = Field(
        None, description="最近共同证书节点详情；无共同证书时为 null"
    )
    reason_code: Optional[str] = Field(
        None,
        description=(
            "未能给出共同证书时的原因码：INSUFFICIENT_PARTICIPANTS | "
            "NO_COMMON_CERTIFICATE；成功时为 null"
        ),
    )
    reason: Optional[str] = Field(None, description="中文原因说明")
    selection_rule: List[str] = Field(
        default_factory=list,
        description="最近共同证书选择规则（按优先级的中文描述）",
    )
    candidates: List[CommonCertificateCandidate] = Field(
        default_factory=list,
        description="全部共同证书候选及距离指标（按当选顺序排列）",
    )
    instrument_chains: List[InstrumentCommonChain] = Field(
        default_factory=list,
        description="逐台仪器的参与结果：子链、距离或不可参与原因",
    )
    statistics: dict = Field(default_factory=dict)


# ---------- 测量相容性评估输出 ----------

class MeasurementReadingResult(BaseModel):
    """单条读数的评估结果：参与则给出所用不确定度，否则给出不可参与原因。"""

    instrument: str
    value: float
    participated: bool = Field(
        ..., description="该读数是否以唯一且校验通过的链参与了 En 计算"
    )
    start_certificate_id: Optional[str] = Field(
        None, description="该仪器在查询时点的唯一有效链起点证书 id"
    )
    certificate_unit: Optional[str] = Field(
        None, description="起点证书的不确定度单位"
    )
    uncertainty: Optional[float] = Field(
        None, description="参与计算时使用的起点证书扩展不确定度 U"
    )
    nonparticipation_reason_code: Optional[str] = None
    nonparticipation_reason: Optional[str] = None


class MeasurementNonparticipant(BaseModel):
    """一条不可参与计算的读数及其原因（附该时点链路报告证据）。"""

    instrument: str
    value: float
    reason_code: str = Field(
        ...,
        description=(
            "不可参与原因码：INSTRUMENT_NOT_FOUND | NO_ACTIVE_CERTIFICATE | "
            "MULTIPLE_ACTIVE_CERTIFICATES | MULTIPLE_VALID_CHAINS | "
            "CHAIN_BROKEN | ALL_CHAINS_FAILED_CHECKS | CHAIN_VALIDATION_FAILED | "
            "CERTIFICATE_UNIT_MISMATCH"
        ),
    )
    reason: str = Field(..., description="中文原因说明")
    resolution: Optional[str] = Field(
        None, description="该时点单仪器报告的 resolution（仪器不存在时为 null）"
    )
    valid: bool
    starts: List[str] = Field(
        default_factory=list, description="该时点有效的起点证书 id"
    )
    conflicts: List[Conflict] = Field(
        default_factory=list,
        description="该时点单仪器报告的完整可解释冲突明细（结构同 /api/reports）",
    )
    start_certificate_id: Optional[str] = Field(
        None, description="链可用但单位不符时给出起点证书 id，否则为 null"
    )
    certificate_unit: Optional[str] = Field(
        None, description="链可用但单位不符时给出起点证书单位，否则为 null"
    )
    error_count: Optional[int] = None
    warning_count: Optional[int] = None


class CompatibilityPair(BaseModel):
    """一对可参与读数的 En 数计算与相容判定。"""

    instrument_a: str
    instrument_b: str
    value_a: float
    value_b: float
    uncertainty_a: float
    uncertainty_b: float
    difference: float = Field(..., description="两读数之差绝对值 |x_a - x_b|")
    en: Optional[float] = Field(
        None,
        description=(
            "En=|x_a-x_b|/sqrt(U_a^2+U_b^2)；两个不确定度均为 0 时无定义（null）"
        ),
    )
    compatible: bool = Field(
        ..., description="En<=1 判为相容；两 U 均为 0 时仅读数相等才相容"
    )
    rule: str = Field(
        ...,
        description=(
            "判定规则：en_le_1（按 En 数判定）| "
            "zero_uncertainty_equality（两 U 均为 0，按读数是否相等判定）"
        ),
    )


class CompatibilityGroup(BaseModel):
    """一个极大两两相容分组（极大团，人数 ≥ 2，组间可重叠）。"""

    members: List[str] = Field(
        ..., description="组内仪器编码，按字典序排列"
    )
    size: int = Field(..., description="组内仪器数量（≥2）")


class MeasurementCompatibilityReport(BaseModel):
    at: datetime
    dry_run: bool = Field(
        ..., description="恒为 true：相容性评估为只读计算，不写入业务数据"
    )
    target_unit: str = Field(..., description="参与比对的目标单位（已去空白）")
    reading_count: int
    participant_count: int
    nonparticipant_count: int
    participants: List[str] = Field(
        default_factory=list,
        description="实际参与 En 计算的仪器编码（按请求顺序）",
    )
    nonparticipants: List[MeasurementNonparticipant] = Field(
        default_factory=list,
        description="逐条给出的不可参与读数及原因（多候选链不任取一条）",
    )
    readings: List[MeasurementReadingResult] = Field(
        default_factory=list,
        description="逐条读数的评估结果（按请求顺序）",
    )
    pairs: List[CompatibilityPair] = Field(
        default_factory=list,
        description="可参与读数的两两 En 计算与判定（按仪器编码排序）",
    )
    groups: List[CompatibilityGroup] = Field(
        default_factory=list,
        description=(
            "全部人数 ≥2 的极大两两相容分组（极大团，可重叠，"
            "按仪器编码稳定排序，不以连通分量代替）"
        ),
    )
    reason_code: Optional[str] = Field(
        None,
        description=(
            "可参与读数不足 2 条时为 INSUFFICIENT_PARTICIPANTS；否则为 null"
        ),
    )
    reason: Optional[str] = Field(None, description="中文原因说明")
    statistics: dict = Field(default_factory=dict)


# ---------- 校准工位预约输出 ----------

class CalibrationBookingOut(BaseModel):
    """一条已确认的校准工位预约（占用区间为半开 [start_at, end_at)）。"""

    id: str
    instrument_code: str
    start_at: datetime = Field(..., description="占用起点（UTC，含）")
    end_at: datetime = Field(..., description="占用终点（UTC，不含）")
    duration_seconds: int
    certificate_id: str = Field(
        ..., description="确认时该仪器唯一有效溯源链的起点证书 id"
    )
    certificate_valid_until: datetime = Field(
        ..., description="确认时起点证书的有效期截止（UTC）"
    )
    note: str
    created_at: datetime


class SlotBusyBooking(BaseModel):
    """空档查询窗内与该窗（半开）相交的一条既有占用。"""

    booking_id: str
    instrument_code: str
    start_at: datetime
    end_at: datetime


class CalibrationSlot(BaseModel):
    """最早可用时段（半开 [start, end)）。"""

    start: datetime = Field(..., description="时段起点（UTC，含）")
    end: datetime = Field(..., description="时段终点（UTC，不含）")
    ends_by_certificate_valid_until: bool = Field(
        ..., description="时段结束是否不晚于起点证书有效期截止"
    )


class CalibrationSlotsOut(BaseModel):
    """GET /api/calibration-slots 响应。

    链不可用、窗内放不下或无空档时 ``slot`` 为 null 并给出
    ``reason_code`` / ``reason``；``trace_report`` 为查询起点该仪器
    的完整溯源校验报告（链不可用时据此说明原因）。
    """

    instrument: str
    window_start: datetime = Field(..., description="请求的查询窗起点（UTC，含）")
    window_end: datetime = Field(..., description="请求的查询窗终点（UTC）")
    duration_seconds: int
    traceability_at: datetime = Field(
        ..., description="溯源链判定时点（UTC，取查询窗起点）"
    )
    traceability_available: bool = Field(
        ..., description="查询起点是否存在唯一且校验通过的溯源链"
    )
    reason_code: Optional[str] = Field(
        None,
        description=(
            "无空档原因：TRACEABILITY_CHAIN_UNAVAILABLE | "
            "WINDOW_TOO_SHORT_FOR_DURATION | NO_FREE_SLOT_IN_WINDOW | "
            "CERTIFICATE_DEADLINE_TOO_EARLY；有时段时为 null"
        ),
    )
    reason: Optional[str] = Field(None, description="中文原因说明")
    chain_reason_code: Optional[str] = Field(
        None,
        description=(
            "溯源链不可用的细分原因：NO_ACTIVE_CERTIFICATE | "
            "MULTIPLE_ACTIVE_CERTIFICATES | MULTIPLE_VALID_CHAINS | "
            "CHAIN_BROKEN | ALL_CHAINS_FAILED_CHECKS | CHAIN_VALIDATION_FAILED"
        ),
    )
    certificate_id: Optional[str] = Field(
        None, description="唯一有效链的起点证书 id（链不可用时为 null）"
    )
    certificate_valid_until: Optional[datetime] = Field(
        None, description="起点证书有效期截止（预约须在此之前结束）"
    )
    search_window_start: datetime = Field(
        ..., description="实际搜索窗起点（UTC，等于请求窗起点）"
    )
    search_window_end: datetime = Field(
        ..., description="实际搜索窗终点（UTC，受证书截止裁剪）"
    )
    slot: Optional[CalibrationSlot] = Field(
        None, description="时间窗内最早可用时段；不存在时为 null"
    )
    busy: List[SlotBusyBooking] = Field(
        default_factory=list,
        description="与搜索窗（半开）相交的既有预约，按起点升序",
    )
    trace_report: dict = Field(
        ..., description="查询起点该仪器的完整溯源校验报告（结构同 /api/reports）"
    )
