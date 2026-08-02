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

    def as_dict(self) -> dict:
        avg = self.latency_sum_ms / self.latency_count if self.latency_count else 0.0
        return {
            "requests": self.requests,
            "ok": self.ok,
            "err": self.err,
            "degraded": self.degraded,
            "avg_latency_ms": avg,
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

    async def route_request(self, virtual_model_id: str, session_id: str | None, send_fn):
        vm = self._require_vm(virtual_model_id)
        last_error: Exception | None = None
        tried_ports: set[int] = set()

        sticky = self._get_sticky(virtual_model_id, session_id)
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

            tried_ports.add(member.port)
            was_sticky = session_id is not None and self._sessions.get(
                self._session_key(virtual_model_id, session_id)
            ) is not None and self._sessions[self._session_key(virtual_model_id, session_id)].member == member

            start = self._clock()
            try:
                result = await send_fn(member, {"attempt": attempt_no, "pool": "default"})
            except Exception as e:  # noqa: BLE001 - 成员失败即重试，非本引擎逻辑错误
                last_error = e
                self.note_request(member, "err", (self._clock() - start) * 1000)
                continue

            latency_ms = (self._clock() - start) * 1000
            body_text = self._extract_body_text(result)
            if self.quota_error(body_text):
                self.trip(member.port, "quota_error")
                last_error = RuntimeError(f"quota exhausted on port {member.port}")
                self.note_request(member, "err", latency_ms)
                continue

            self.note_request(member, "ok", latency_ms)
            if not was_sticky:
                self._set_sticky(virtual_model_id, session_id, member)
            return member, result

        # 降级池
        if vm.fallback_pool:
            fb_attempts = max(vm.fallback_retries, 1)
            fb_tried: set[int] = set()
            for attempt_no in range(1, fb_attempts + 1):
                available = [m for m in self._available(vm.fallback_pool) if m.port not in fb_tried]
                if not available:
                    available = self._available(vm.fallback_pool)
                if not available:
                    break
                member = self._weighted_choice(available)
                fb_tried.add(member.port)
                start = self._clock()
                try:
                    result = await send_fn(member, {"attempt": attempt_no, "pool": "fallback"})
                except Exception as e:  # noqa: BLE001
                    last_error = e
                    self.note_request(member, "err", (self._clock() - start) * 1000)
                    continue
                latency_ms = (self._clock() - start) * 1000
                body_text = self._extract_body_text(result)
                if self.quota_error(body_text):
                    self.trip(member.port, "quota_error")
                    last_error = RuntimeError(f"quota exhausted on port {member.port}")
                    self.note_request(member, "err", latency_ms)
                    continue
                self.note_request(member, "degraded", latency_ms)
                # 降级池成功不更新会话粘性
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
        try:
            text = getattr(result, "text", None)
            if text is None:
                return str(result)
            return text if isinstance(text, str) else str(text)
        except Exception:
            return ""

    def note_request(self, member: PoolMember, outcome: str, latency_ms: float) -> None:
        stats = self._stats.setdefault(member.key, MemberStats())
        stats.requests += 1
        if outcome == "ok":
            stats.ok += 1
        elif outcome == "degraded":
            stats.degraded += 1
        else:
            stats.err += 1
        stats.latency_sum_ms += latency_ms
        stats.latency_count += 1

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
        return {
            "virtual_models": per_vm,
            "session": self.session_stats(),
            "breakers": {port: {"state": b.state, "reason": b.reason} for port, b in self._breakers.items()},
        }
