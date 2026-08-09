"""聚合网关（8080 端口）核心引擎 —— 纯逻辑，无网络 I/O。

设计决策（务必先读）：
1. 会话粘性 key = (virtual_model_id, session_id)。同一 session_id 在不同虚拟模型下
   拥有独立的粘性条目，互不影响（同一物理端口可被多个虚拟模型复用）。
2. 熔断粒度 = 端口（port）。trip(port) 会摘除该端口下*所有*虚拟模型池中的*所有*成员——
   一个端口可能同时服务 agg:sonnet 和 agg:opus，两者都会失去该端口。
3. 429 / 限流 ≠ 配额耗尽。quotaErrorPatterns 是独立配置，与 server.py 里翻译 429 用的
   _VENDOR_ERROR_PATTERNS 完全分离。429 类文本（rate_limit_exceeded / too_many_requests /
   ResourceExhausted / rate_limit_error）绝不触发熔断；只有 "insufficient credit" /
   "quota exceeded" / "余额不足" 这类配额耗尽文本才触发熔断。
4. 降级池（fallbackPool）成功后不更新会话粘性——降级是临时逃生舱，会话仍应偏好默认池，
   下次请求继续走默认池 + 粘性优先，除非粘性成员被熔断。
5. 重试语义：defaultRetries 是默认池最多尝试次数（含首次），每次尝试挑选不同成员
   （避免反复打同一个失败成员）。粘性成员失败但未被熔断 → 不更新粘性；
   粘性成员被熔断 → 后续请求会重新粘到新的成功成员。
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field


class AllPoolsExhausted(Exception):
    """默认池与降级池均尝试失败。"""

    def __init__(self, message: str, last_error: Exception | None = None) -> None:
        super().__init__(message)
        self.last_error = last_error


# 聚合网关失败分类表（状态码严格优先于文本）。
# 命中此表的状态码直接返回对应分类，绝不回退到文本判断。
_HTTP_STATUS_CLASSIFICATION: dict[int, str] = {
    400: "pass_through_to_client",
    401: "retry_other_or_fallback",
    402: "retry_other_or_fallback",
    403: "retry_other_or_fallback",
    404: "pass_through_to_client",
    408: "retry_same",
    422: "pass_through_to_client",
    429: "retry_same",
    500: "retry_same",
    502: "retry_same",
    503: "retry_same",
    504: "retry_same",
    508: "retry_same",
}

# retry_other_or_fallback 分类下的熔断原因（状态码 → reason）。
# 不在表内（即由配额文本兜底命中）统一记为 "quota_text"。
_RETRY_OTHER_REASONS: dict[int, str] = {
    401: "401_auth",
    402: "402_billing",
    403: "403_forbidden",
}


@dataclass(frozen=True)
class PoolMember:
    port: int
    model: str
    weight: float
    raw: dict = field(default_factory=dict, compare=False, repr=False)

    @property
    def key(self) -> tuple[int, str]:
        return (self.port, self.model)


@dataclass
class BreakerState:
    state: str  # "normal" | "tripped" | "probing"
    reason: str = ""
    tripped_at: float = 0.0
    probes: int = 0


@dataclass
class MemberStats:
    requests: int = 0
    ok: int = 0
    err: int = 0
    degraded: int = 0
    latency_sum_ms: float = 0.0
    latency_count: int = 0
    error_types: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        avg = self.latency_sum_ms / self.latency_count if self.latency_count else 0.0
        return {
            "requests": self.requests,
            "ok": self.ok,
            "err": self.err,
            "degraded": self.degraded,
            "avg_latency_ms": avg,
            "error_types": dict(self.error_types),
        }


@dataclass
class _VirtualModelConfig:
    default_pool: list[PoolMember]
    fallback_pool: list[PoolMember]
    default_retries: int
    fallback_retries: int


@dataclass
class _SessionEntry:
    member: PoolMember
    expires_at: float


class AggregatorEngine:
    def __init__(self, target: dict, *, clock=None, rng: random.Random | None = None) -> None:
        self._clock = clock or time.time
        self._rng = rng or random.Random()
        self._breakers: dict[int, BreakerState] = {}
        self._sessions: dict[tuple[str, str], _SessionEntry] = {}
        self._stats: dict[tuple[int, str], MemberStats] = {}
        self._session_hits = 0
        self._session_lookups = 0
        self._models: dict[str, _VirtualModelConfig] = {}
        self._quota_patterns: list[re.Pattern] = []
        self._session_ttl: float = 3600.0
        self._probe_interval: float = 300.0
        self._started_at: float = self._clock()
        self.reload(target)

    @classmethod
    def from_target(cls, target: dict, **kw) -> "AggregatorEngine":
        return cls(target, **kw)

    # ─── 配置装载 ───

    def reload(self, target: dict) -> None:
        """热重载：重算池成员/配置。会话缓存与熔断状态对仍存在的端口保留；
        被移除端口的熔断状态清除（该端口不再属于任何虚拟模型池）。"""
        defaults = target.get("poolDefaults", {}) or {}
        default_weight = defaults.get("weight", 1)
        default_retries_g = defaults.get("defaultRetries", 2)
        fallback_retries_g = defaults.get("fallbackRetries", 1)
        self._session_ttl = float(defaults.get("sessionAffinityTtlSeconds", 3600))
        self._probe_interval = float(defaults.get("probeIntervalSeconds", 300))

        patterns = target.get("quotaErrorPatterns", []) or []
        self._quota_patterns = [re.compile(p, re.IGNORECASE) for p in patterns]

        virtual_models = target.get("virtualModels", {}) or {}
        new_models: dict[str, _VirtualModelConfig] = {}
        known_ports: set[int] = set()

        for vm_id, vm_cfg in virtual_models.items():
            default_pool_raw = vm_cfg.get("defaultPool") or []
            if not default_pool_raw:
                raise ValueError(f"virtual model {vm_id!r} 的 defaultPool 不能为空")
            default_pool = [self._build_member(m, default_weight) for m in default_pool_raw]
            fallback_pool_raw = vm_cfg.get("fallbackPool") or []
            fallback_pool = [self._build_member(m, default_weight) for m in fallback_pool_raw]

            for m in default_pool + fallback_pool:
                known_ports.add(m.port)

            new_models[vm_id] = _VirtualModelConfig(
                default_pool=default_pool,
                fallback_pool=fallback_pool,
                default_retries=vm_cfg.get("defaultRetries", default_retries_g),
                fallback_retries=vm_cfg.get("fallbackRetries", fallback_retries_g),
            )

        self._models = new_models

        # 清除不再被任何虚拟模型引用的端口的熔断状态
        for port in list(self._breakers.keys()):
            if port not in known_ports:
                del self._breakers[port]

        # 清理会话缓存中指向已不存在虚拟模型的条目
        for key in list(self._sessions.keys()):
            vm_id, _ = key
            if vm_id not in self._models:
                del self._sessions[key]

    @staticmethod
    def _build_member(m: dict, default_weight: float) -> PoolMember:
        return PoolMember(port=m["port"], model=m["model"], weight=m.get("weight", default_weight), raw=m)

    # ─── 查询 ───

    def list_virtual_models(self) -> list[str]:
        return list(self._models.keys())

    def has_virtual_model(self, model_id: str) -> bool:
        return model_id in self._models

    def _require_vm(self, virtual_model_id: str) -> _VirtualModelConfig:
        vm = self._models.get(virtual_model_id)
        if vm is None:
            raise ValueError(f"未知虚拟模型: {virtual_model_id!r}")
        return vm

    # ─── 熔断 ───

    def _is_tripped(self, port: int) -> bool:
        b = self._breakers.get(port)
        return b is not None and b.state in ("tripped", "probing")

    def trip(self, port: int, reason: str) -> None:
        self._breakers[port] = BreakerState(state="tripped", reason=reason, tripped_at=self._clock(), probes=0)

    def record_probe_result(self, port: int, ok: bool) -> None:
        b = self._breakers.get(port)
        if b is None:
            return
        if ok:
            del self._breakers[port]
        else:
            b.state = "tripped"
            b.tripped_at = self._clock()

    def probe_due_ports(self) -> list[int]:
        now = self._clock()
        due = []
        for port, b in self._breakers.items():
            if b.state == "tripped" and (now - b.tripped_at) >= self._probe_interval:
                b.state = "probing"
                b.probes += 1
                due.append(port)
        return due

    def tripped_ports(self) -> dict[int, BreakerState]:
        return dict(self._breakers)

    # ─── 选择逻辑 ───

    def _available(self, pool: list[PoolMember]) -> list[PoolMember]:
        return [m for m in pool if not self._is_tripped(m.port)]

    def _weighted_choice(self, members: list[PoolMember]) -> PoolMember:
        if not members:
            raise ValueError("候选成员池为空")
        total = sum(m.weight for m in members)
        if total <= 0:
            return self._rng.choice(members)
        r = self._rng.uniform(0, total)
        upto = 0.0
        for m in members:
            upto += m.weight
            if r <= upto:
                return m
        return members[-1]

    def _session_key(self, virtual_model_id: str, session_id: str) -> tuple[str, str]:
        return (virtual_model_id, session_id)

    def _get_sticky(self, virtual_model_id: str, session_id: str | None) -> PoolMember | None:
        if session_id is None:
            return None
        self._session_lookups += 1
        key = self._session_key(virtual_model_id, session_id)
        entry = self._sessions.get(key)
        if entry is None:
            return None
        if self._clock() >= entry.expires_at:
            del self._sessions[key]
            return None
        if self._is_tripped(entry.member.port):
            return None
        self._session_hits += 1
        return entry.member

    def _set_sticky(self, virtual_model_id: str, session_id: str | None, member: PoolMember) -> None:
        if session_id is None:
            return
        key = self._session_key(virtual_model_id, session_id)
        self._sessions[key] = _SessionEntry(member=member, expires_at=self._clock() + self._session_ttl)

    def pick_member(self, virtual_model_id: str, session_id: str | None) -> PoolMember:
        vm = self._require_vm(virtual_model_id)
        sticky = self._get_sticky(virtual_model_id, session_id)
        if sticky is not None:
            return sticky
        available = self._available(vm.default_pool)
        if not available:
            raise ValueError(f"虚拟模型 {virtual_model_id!r} 无可用默认池成员（全部熔断）")
        member = self._weighted_choice(available)
        self._set_sticky(virtual_model_id, session_id, member)
        return member

    # ─── 编排 ───

    def quota_error(self, body_text: str) -> bool:
        return any(p.search(body_text) for p in self._quota_patterns)

    def classify_failure(
        self,
        status_code: int | None,
        body_text: str,
        quota_error_fn=None,
    ) -> str:
        """分类一次失败的下游响应，供后续重试/熔断/降级使用。

        判定优先级（严格）：状态码优先于文本。
        1. 2xx 状态码 → "success"，绝不调用 quota_error_fn
           （配额词可能出现在正常对话内容里，对 2xx 做文本兜底会误熔断）。
        2. 状态码命中 _HTTP_STATUS_CLASSIFICATION → 直接返回映射分类，不看文本。
        3. 状态码不在映射表（含 None 无状态码信号）→ 此时回退到文本：
           用 quota_error_fn（None 时 self.quota_error）查配额词，命中返回
           "retry_other_or_fallback"。
           - None 且无状态码信号、文本也未命中 → 安全默认 "success"（裸字符串正常内容）
           - 有状态码但未知且文本未命中 → "unclassified"
        """
        if status_code is not None and 200 <= status_code < 300:
            return "success"
        if status_code is not None and status_code in _HTTP_STATUS_CLASSIFICATION:
            return _HTTP_STATUS_CLASSIFICATION[status_code]
        fn = quota_error_fn if quota_error_fn is not None else self.quota_error
        if fn(body_text):
            return "retry_other_or_fallback"
        if status_code is None:
            return "success"  # 无状态码且文本未命中 → 安全默认成功
        return "unclassified"  # 有状态码但未知且文本未命中 → unclassified

    async def route_request(self, virtual_model_id: str, session_id: str | None, send_fn):
        vm = self._require_vm(virtual_model_id)
        last_error: Exception | None = None
        tried_ports: set[int] = set()
        # 端口 → 该端口累计的 retry_same（408/5xx）次数。
        # 第一次允许同端点重试（不加入 tried_ports），第二次起换端点。
        retry_same_counts: dict[int, int] = {}

        sticky = self._get_sticky(virtual_model_id, session_id)
        # 本轮进入时的粘性成员快照。若粘性成员失败后换到了别的成员并成功，
        # 必须把粘性确定性地重新生根到新成员（否则下一次请求还会先打已失败的旧成员）。
        original_sticky_member = sticky
        candidates_order: list[PoolMember] = []
        if sticky is not None:
            candidates_order.append(sticky)

        attempts = max(vm.default_retries, 1)
        attempt_no = 0
        while attempt_no < attempts:
            attempt_no += 1
            member = None
            if candidates_order:
                member = candidates_order.pop(0)
            else:
                available = [m for m in self._available(vm.default_pool) if m.port not in tried_ports]
                if not available:
                    available = self._available(vm.default_pool)
                if not available:
                    break
                member = self._weighted_choice(available)

            if self._is_tripped(member.port):
                # 粘性成员被熔断：需要重新选择并在成功后重粘
                available = [m for m in self._available(vm.default_pool) if m.port not in tried_ports]
                if not available:
                    available = self._available(vm.default_pool)
                if not available:
                    break
                member = self._weighted_choice(available)

            was_sticky = session_id is not None and self._sessions.get(
                self._session_key(virtual_model_id, session_id)
            ) is not None and self._sessions[self._session_key(virtual_model_id, session_id)].member == member

            start = self._clock()
            try:
                result = await send_fn(member, {"attempt": attempt_no, "pool": "default"})
            except Exception as e:  # noqa: BLE001 - 成员失败即重试，非本引擎逻辑错误
                last_error = e
                tried_ports.add(member.port)
                self.note_request(member, "err", (self._clock() - start) * 1000, vm_id=virtual_model_id)
                continue

            latency_ms = (self._clock() - start) * 1000
            status_code = getattr(result, "status_code", None)
            # 惰性 body 读取：仅当状态码能确定分类（2xx 或命中映射表）时才不需要 body；
            # None 状态码无成功/分类信号，需要文本兜底，也必须读 body
            if status_code is not None and (200 <= status_code < 300 or status_code in _HTTP_STATUS_CLASSIFICATION):
                body_text = ""
            else:
                body_text = self._extract_body_text(result)
            classification = self.classify_failure(status_code, body_text, self.quota_error)

            # D. 成功（2xx / 无状态码）—— 唯一成功路径
            if classification == "success":
                tried_ports.add(member.port)
                self.note_request(member, "ok", latency_ms, vm_id=virtual_model_id)
                # 首次建立粘性，或粘性成员失败后换到了新成员 → 重新生根到当前成功成员
                if not was_sticky or member != original_sticky_member:
                    self._set_sticky(virtual_model_id, session_id, member)
                return member, result

            # A. 凭据/配额类失败：熔断该端口 + 换端点
            if classification == "retry_other_or_fallback":
                reason = (
                    _RETRY_OTHER_REASONS.get(status_code, "quota_text")
                    if isinstance(status_code, int)
                    else "quota_text"
                )
                tried_ports.add(member.port)
                self.trip(member.port, reason)
                self.note_request(member, "err", latency_ms, error_type=reason, vm_id=virtual_model_id)
                last_error = RuntimeError(f"{reason} on port {member.port}")
                continue

            if classification == "retry_same":
                # B1. 429 账号级限流：立刻换端点，但绝不熔断
                #     （限流会自行恢复，trip 300s 会误伤共享该端口的其他虚拟模型/会话）
                if status_code == 429:
                    tried_ports.add(member.port)
                    self.note_request(member, "err", latency_ms, error_type="429_rate_limit", vm_id=virtual_model_id)
                    last_error = RuntimeError(f"429_rate_limit on port {member.port}")
                    continue

                # B2. 408/5xx 网络抖动：同端点重试一次，仍失败才换端点；全程不熔断
                count = retry_same_counts.get(member.port, 0) + 1
                retry_same_counts[member.port] = count
                if count == 1:
                    # 不加入 tried_ports，并把该成员放回候选队首 → 下一轮确定性地
                    # 重新选中同一端口，真实实现"同端点重试 1 次"语义
                    tried_ports.discard(member.port)
                    candidates_order.insert(0, member)
                    self.note_request(member, "err", latency_ms, error_type=f"{status_code}_transient", vm_id=virtual_model_id)
                    continue
                tried_ports.add(member.port)
                self.note_request(member, "err", latency_ms, error_type="5xx_persistent", vm_id=virtual_model_id)
                last_error = RuntimeError(f"5xx_persistent on port {member.port}")
                continue

            # C. pass_through_to_client / unclassified：不重试、不换端点、不熔断
            if classification == "unclassified":
                import server as _srv

                _srv.logger.warning(
                    f"[aggregator] unclassified failure: port={member.port} "
                    f"model={member.model} status={status_code} "
                    f"body_prefix={body_text[:200]!r}"
                )
            tried_ports.add(member.port)
            self.note_request(member, "err", latency_ms, error_type=classification, vm_id=virtual_model_id)
            return member, result

        # 降级池：与默认池使用完全一致的 classify_failure 五分类语义
        # （429 / 5xx 持续失败同样绝不熔断——两个池的"不熔断"语义必须一致）。
        # 唯一差异：成功记 "degraded" 且不更新会话粘性（模块头设计原则第 4 条）。
        if vm.fallback_pool:
            fb_attempts = max(vm.fallback_retries, 1)
            fb_tried: set[int] = set()
            # 独立于默认池的计数器/候选队列，避免跨池计数污染（fallbackPool 可能引用同端口）
            fb_retry_same_counts: dict[int, int] = {}
            fb_candidates_order: list[PoolMember] = []
            attempt_no = 0
            while attempt_no < fb_attempts:
                attempt_no += 1
                if fb_candidates_order:
                    member = fb_candidates_order.pop(0)
                    if self._is_tripped(member.port):
                        continue
                else:
                    available = [m for m in self._available(vm.fallback_pool) if m.port not in fb_tried]
                    if not available:
                        available = self._available(vm.fallback_pool)
                    if not available:
                        break
                    member = self._weighted_choice(available)
                start = self._clock()
                try:
                    result = await send_fn(member, {"attempt": attempt_no, "pool": "fallback"})
                except Exception as e:  # noqa: BLE001
                    last_error = e
                    fb_tried.add(member.port)
                    self.note_request(member, "err", (self._clock() - start) * 1000, vm_id=virtual_model_id)
                    continue

                latency_ms = (self._clock() - start) * 1000
                status_code = getattr(result, "status_code", None)
                # 惰性 body 读取：仅当状态码能确定分类（2xx 或命中映射表）时才不需要 body；
                # None 状态码无成功/分类信号，需要文本兜底，也必须读 body
                if status_code is not None and (200 <= status_code < 300 or status_code in _HTTP_STATUS_CLASSIFICATION):
                    body_text = ""
                else:
                    body_text = self._extract_body_text(result)
                classification = self.classify_failure(status_code, body_text, self.quota_error)

                # D. 成功（2xx / 无状态码）—— 降级池成功记 degraded，且不更新会话粘性
                if classification == "success":
                    fb_tried.add(member.port)
                    self.note_request(member, "degraded", latency_ms, vm_id=virtual_model_id)
                    return member, result

                # A. 凭据/配额类失败：熔断该端口 + 换端点
                if classification == "retry_other_or_fallback":
                    reason = (
                        _RETRY_OTHER_REASONS.get(status_code, "quota_text")
                        if isinstance(status_code, int)
                        else "quota_text"
                    )
                    fb_tried.add(member.port)
                    self.trip(member.port, reason)
                    self.note_request(member, "err", latency_ms, error_type=reason, vm_id=virtual_model_id)
                    last_error = RuntimeError(f"{reason} on port {member.port}")
                    continue

                if classification == "retry_same":
                    # B1. 429 账号级限流：立刻换端点，但绝不熔断
                    if status_code == 429:
                        fb_tried.add(member.port)
                        self.note_request(member, "err", latency_ms, error_type="429_rate_limit", vm_id=virtual_model_id)
                        last_error = RuntimeError(f"429_rate_limit on port {member.port}")
                        continue

                    # B2. 408/5xx 网络抖动：同端点重试一次，仍失败才换端点；全程不熔断
                    count = fb_retry_same_counts.get(member.port, 0) + 1
                    fb_retry_same_counts[member.port] = count
                    if count == 1:
                        fb_tried.discard(member.port)
                        fb_candidates_order.insert(0, member)
                        self.note_request(member, "err", latency_ms, error_type=f"{status_code}_transient", vm_id=virtual_model_id)
                        continue
                    fb_tried.add(member.port)
                    self.note_request(member, "err", latency_ms, error_type="5xx_persistent", vm_id=virtual_model_id)
                    last_error = RuntimeError(f"5xx_persistent on port {member.port}")
                    continue

                # C. pass_through_to_client / unclassified：不重试、不换端点、不熔断
                if classification == "unclassified":
                    import server as _srv

                    _srv.logger.warning(
                        f"[aggregator] unclassified failure: port={member.port} "
                        f"model={member.model} status={status_code} "
                        f"body_prefix={body_text[:200]!r}"
                    )
                fb_tried.add(member.port)
                self.note_request(member, "err", latency_ms, error_type=classification, vm_id=virtual_model_id)
                return member, result

        raise AllPoolsExhausted(
            f"虚拟模型 {virtual_model_id!r} 默认池与降级池均失败", last_error=last_error
        )

    # ─── 统计 ───

    @staticmethod
    def _extract_body_text(result) -> str:
        """提取响应文本用于配额检测。

        - str：直接用（测试注入的 fake send_fn）
        - httpx 流式响应（stream=True 未 read）：.text 会抛异常，返回 "" 跳过检测
          （真实场景的配额检测由 server.py 的非流式路径负责）
        - 其他对象：str() 兜底
        """
        if isinstance(result, str):
            return result
        # 流式响应（httpx stream=True 未 read）的提前判断：content-type 含
        # text/event-stream 直接返回 ""，避免访问 .text 触发 httpx.ResponseNotRead
        # 异常（虽被下方 try/except 兜底，但无谓异常路径会污染日志）。
        # 核心约束：绝不调用 .aread()/.read() 消费流式 body，否则会破坏
        # http_adapter 的 _write_response 流式转发（body 只能被下游读一次）。
        headers = getattr(result, "headers", None)
        if headers is not None:
            ct = headers.get("content-type", "") if hasattr(headers, "get") else ""
            if "text/event-stream" in ct:
                return ""
        try:
            text = getattr(result, "text", None)
            if text is None:
                return str(result)
            return text if isinstance(text, str) else str(text)
        except Exception:
            return ""

    def note_request(
        self,
        member: PoolMember,
        outcome: str,
        latency_ms: float,
        error_type: str | None = None,
        *,
        vm_id: str | None = None,
    ) -> None:
        stats = self._stats.setdefault(member.key, MemberStats())
        stats.requests += 1
        if outcome == "ok":
            stats.ok += 1
        elif outcome == "degraded":
            stats.degraded += 1
        else:
            stats.err += 1
            if error_type is not None:
                stats.error_types[error_type] = stats.error_types.get(error_type, 0) + 1
        stats.latency_sum_ms += latency_ms
        stats.latency_count += 1

        # 旁路：同步累加进 server._AGGREGATOR_ACCUM（异常绝不影响主链路；与 server._bump_model_stats 同风格）
        try:
            import server as _srv
            _srv._bump_aggregator_usage(vm_id, member.port, member.model, outcome, error_type)
        except Exception:
            pass

    def session_stats(self) -> dict:
        hit_rate = self._session_hits / self._session_lookups if self._session_lookups else 0.0
        return {
            "cache_size": len(self._sessions),
            "hits": self._session_hits,
            "lookups": self._session_lookups,
            "hit_rate": hit_rate,
        }

    def get_stats(self) -> dict:
        per_vm = {}
        for vm_id, vm in self._models.items():
            members_stats = {}
            for m in vm.default_pool + vm.fallback_pool:
                s = self._stats.get(m.key)
                if s is not None:
                    members_stats[f"{m.port}:{m.model}"] = s.as_dict()
            per_vm[vm_id] = members_stats
        uptime_seconds = self._clock() - self._started_at
        return {
            "virtual_models": per_vm,
            "session": self.session_stats(),
            "breakers": {port: {"state": b.state, "reason": b.reason, "tripped_at": b.tripped_at} for port, b in self._breakers.items()},
            "started_at": self._started_at,
            "uptime_seconds": uptime_seconds,
        }
