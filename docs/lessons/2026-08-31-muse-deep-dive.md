# 教训：只看表面现象，未深挖翻译层根因（2026-08-31）

**用户反馈原话**： “我都让你改了，你一直都是只看表面现象” “没有深挖问题的根因”

## 表象
- `8094:muse-spark-1.2-contributor` 连续 `400/500 Over capacity`，第一反应是“上游过载，等恢复”，并用 `8080` 聚合降级成功来证明“已通”。
- 但用户给出最强区分证据：**直连 muse 没问题，走了代理就有问题 → 必是代理弄丢/写错了什么**。该证据被初期排查以“间歇性上游过载”搪塞过去。

## 根因
- `open-go` 的 `muse` 是 `Responses-only` 模型，`8082` 的 `chat→responses` 翻译被直接复用，但未适配 `muse` 的参数约束：
  - `reasoning_effort:max` → `reasoning.effort:max`（上游只认 `high`） → `400 unknown variant max`
  - `max_tokens:4` → `max_output_tokens:4`（上游要求 `>=16`） → `400 max_output_tokens must be >=16`
- 聚合层按 `400→熔断` 直接摘掉 `8094`，导致 `deepseek-v4-pro:agg` 持续降级。

## 正确做法
1. 出现“直连通、代理不通”稳定复现时，**先假设代理丢参**，用同一份真实业务报文（带 `system`/`tools`/`reasoning_effort`/`stream_options`）并排抓包对比 `body_keys` 与上游原始报文，而不是用极简 `hi` 自证“不丢”。
2. 翻译层兜底：`max_output_tokens<16 → 16`，`reasoning.effort max → high`（见 `gateways/open_go.py`）。
3. 执行纪律：用户说“一直改到位、改完重启再观察”，就按 `改→重启→真实流量探针` 闭环执行，不以“上游过载”等表面解释提前收尾。

## 关联改动
- `gateways/open_go.py` 新增 `muse` 的 Responses 翻译与参数兜底
- `gateways/aggregator/http_adapter.py` 探针超时 `5s→15s` + `x-session-id` 透传
- 全局 `~/.config/opencode/AGENTS.md` §7 已同步记录
