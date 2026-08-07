"""Dashboard 前端渲染层（DASHBOARD_STYLE + 辅助函数 + dashboard() 页面）。

从 routes.py 抽出，行为逐字节不变。依赖方向：frontend → routes(router) / server。
"""

import asyncio
import json
import socket
from datetime import datetime
from typing import Dict, List, Optional

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

import config_store as _cfg
import server as _srv
from server import (
    _ANTHROPIC_STATS,
    _MODEL_STATS,
    _TARGET_STATS,
    _anthropic_port_models,
    _build_models_list,
    _crack_env_check,
    _fetch_live_models,
    _get_target_models_async,
    _humanize_model_name,
    _refresh_secrets,
    _reload_targets,
    _run_crack_tool,
    _scan_dangling_refs,
    _target_model_source,
    crack_common,
    logger,
)
from dashboard.routes import dashboard_router

# ─── 统一管理面板（所有 LLM 相关服务一览）─────────────────────────────

DASHBOARD_STYLE = """
  /* ── 设计 Token（OpenRouter 风格：深色近黑 + 品牌青蓝渐变）── */
  :root {
    --bg-page: #0a0a0f;
    --bg-elev: #10101a;
    --bg-card: #13131d;
    --bg-card-hi: #171724;
    --bg-inset: #0d0d14;
    --border: rgba(148, 163, 184, 0.14);
    --border-strong: rgba(148, 163, 184, 0.28);
    --border-focus: rgba(34, 211, 238, 0.55);
    --brand-cyan: #22d3ee;
    --brand-blue: #3b82f6;
    --brand-grad: linear-gradient(135deg, #22d3ee 0%, #3b82f6 100%);
    --brand-glow: rgba(34, 211, 238, 0.35);
    --text-primary: #eceef4;
    --text-secondary: #9aa3b2;
    --text-tertiary: #6b7280;
    --success: #34d399;
    --warning: #fbbf24;
    --danger: #f87171;
    --radius-lg: 14px;
    --radius-md: 10px;
    --radius-sm: 7px;
    --font-mono: ui-monospace, "SF Mono", "Cascadia Mono", monospace;
  }

  /* ── 全局 ── */
  *, *::before, *::after { box-sizing: border-box; }
  body { font-family: -apple-system, "Segoe UI", ui-monospace, sans-serif; background-color: var(--bg-page); color: var(--text-primary); margin: 0; padding: 32px; min-height: 100vh; background-image: radial-gradient(1000px 500px at 85% -10%, rgba(59, 130, 246, 0.10), transparent 60%), radial-gradient(900px 460px at -10% 0%, rgba(34, 211, 238, 0.07), transparent 55%), radial-gradient(2px 2px at 20% 30%, rgba(148,163,184,0.10), transparent 100%), radial-gradient(2px 2px at 70% 60%, rgba(148,163,184,0.08), transparent 100%); background-attachment: fixed; }
  h1 { font-size: 21px; font-weight: 700; margin: 0 0 2px 0; letter-spacing: -0.3px; }
  h3 { font-size: 14px; font-weight: 600; margin: 0 0 10px 0; }
  .sub { color: var(--text-secondary); font-size: 13px; margin-bottom: 22px; }
  .sub .refresh-time { font-size: 12px; color: var(--text-tertiary); }
  code { color: var(--brand-cyan); }
  a { color: #7aa2ff; }
  .field-error { border-color: var(--danger) !important; box-shadow: 0 0 0 2px rgba(248,113,113,0.25); }

  /* ── 总览栏：KPI 统计卡（OpenRouter 大数字风格）── */
  .overview-bar { display: flex; gap: 20px; flex-wrap: wrap; align-items: stretch; background: linear-gradient(180deg, rgba(22,22,36,0.9) 0%, rgba(13,13,20,0.9) 100%); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 16px; margin-bottom: 26px; box-shadow: 0 10px 40px rgba(0,0,0,0.45), inset 0 1px 0 rgba(255,255,255,0.05); backdrop-filter: blur(4px); }
  .kpi-grid { display: grid; grid-template-columns: repeat(4, minmax(150px, 1fr)); gap: 14px; flex: 1 1 auto; min-width: 0; }
  .kpi-card { position: relative; display: flex; flex-direction: column; gap: 6px; background: linear-gradient(180deg, rgba(255,255,255,0.045) 0%, rgba(255,255,255,0.015) 100%), var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius-md); padding: 14px 18px 16px; overflow: hidden; transition: border-color 0.2s, transform 0.2s, box-shadow 0.2s; }
  .kpi-card::before { content: ""; position: absolute; top: 0; left: 12%; right: 12%; height: 1px; background: linear-gradient(90deg, transparent, rgba(34,211,238,0.7), transparent); }
  .kpi-card::after { content: ""; position: absolute; inset: 0; background: radial-gradient(120% 90% at 100% 0%, rgba(34,211,238,0.09), transparent 55%); pointer-events: none; }
  .kpi-card:hover { border-color: rgba(34,211,238,0.4); transform: translateY(-2px); box-shadow: 0 8px 28px rgba(34,211,238,0.10), 0 4px 16px rgba(0,0,0,0.35); }
  .kpi-label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 1.2px; color: var(--text-tertiary); }
  .kpi-value { font-family: var(--font-mono); font-variant-numeric: tabular-nums; font-size: 32px; font-weight: 700; line-height: 1.05; color: var(--text-primary); letter-spacing: -0.5px; }
  .kpi-value small { font-size: 16px; font-weight: 600; color: var(--text-secondary); margin-left: 3px; }
  .kpi-value.accent { background: var(--brand-grad); -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent; }
  .kpi-sub { font-size: 11px; color: var(--text-tertiary); margin-top: 2px; }
  .kpi-sub .kpi-dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-right: 5px; vertical-align: 1px; }
  .ov-side { display: flex; flex-direction: column; align-items: flex-end; justify-content: space-between; gap: 12px; flex-shrink: 0; }
  .ov-dots { display: flex; align-items: center; gap: 5px; flex-wrap: wrap; }
  .ov-actions { display: flex; gap: 10px; align-items: center; }
  .status-dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; }
  .status-dot.green { background: var(--success); box-shadow: 0 0 8px rgba(52,211,153,0.6); }
  .status-dot.yellow { background: var(--warning); box-shadow: 0 0 8px rgba(251,191,36,0.5); }
  .status-dot.red { background: var(--danger); box-shadow: 0 0 8px rgba(248,113,113,0.5); }

  /* ── 卡片头启动状态灯（呼吸动画）── */
  .ct-lamp { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; transition: background 0.3s, box-shadow 0.3s; }
  .ct-lamp.on { background: var(--success); box-shadow: 0 0 10px rgba(52,211,153,0.7); animation: lampPulse 2.2s ease-in-out infinite; }
  .ct-lamp.off { background: var(--danger); box-shadow: 0 0 8px rgba(248,113,113,0.6); }
  .ct-lamp.idle { background: #9aa3b2; box-shadow: 0 0 6px rgba(154,163,178,0.4); }
  @keyframes lampPulse { 0%, 100% { box-shadow: 0 0 6px rgba(52,211,153,0.4); } 50% { box-shadow: 0 0 14px rgba(52,211,153,0.9); } }

  /* ── 区块 ── */
  .section { margin-bottom: 28px; }
  .section-title { font-size: 14px; font-weight: 700; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 1.4px; margin-bottom: 12px; padding-bottom: 10px; border-bottom: 1px solid rgba(148,163,184,0.10); display: flex; align-items: center; gap: 8px; }
  .section-title::before { content: ""; width: 3px; height: 15px; border-radius: 3px; background: var(--brand-grad); flex-shrink: 0; box-shadow: 0 0 8px var(--brand-glow); }
  .section-title .sec-count { display: inline-flex; align-items: center; justify-content: center; min-width: 22px; height: 20px; font-size: 11px; font-weight: 700; color: var(--brand-cyan); background: rgba(34,211,238,0.10); border: 1px solid rgba(34,211,238,0.25); border-radius: 999px; padding: 0 8px; margin-left: 2px; vertical-align: middle; letter-spacing: 0; }

  /* ── 卡片纵向排列（单列，不做自适应 flow）── */
  .card-grid { display: flex; flex-direction: column; gap: 14px; }

  /* ── 卡片容器：深色渐变底 + 顶部高光线 + hover 品牌光晕 ── */
  .card { position: relative; background: linear-gradient(180deg, rgba(255,255,255,0.035) 0%, rgba(255,255,255,0.008) 40%, rgba(255,255,255,0) 100%), var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius-lg); overflow: hidden; transition: border-color 0.25s, transform 0.2s, box-shadow 0.25s; box-shadow: inset 0 1px 0 rgba(255,255,255,0.05), 0 1px 4px rgba(0,0,0,0.3); }
  .card::before { content: ""; position: absolute; top: 0; left: 8%; right: 8%; height: 1px; background: linear-gradient(90deg, transparent, rgba(34,211,238,0.45), transparent); z-index: 1; pointer-events: none; }
  .card:hover { border-color: rgba(34,211,238,0.35); transform: translateY(-3px); box-shadow: inset 0 1px 0 rgba(255,255,255,0.06), 0 12px 40px rgba(34,211,238,0.10), 0 6px 20px rgba(0,0,0,0.4); }
  /* 端口强调条：左 3px 彩色 border */
  .card.accent-8082 { border-left: 3px solid #3b82f6; }
  .card.accent-8084 { border-left: 3px solid #a78bfa; }
  .card.accent-8090 { border-left: 3px solid #f59e0b; }
  .card.accent-8091 { border-left: 3px solid #34d399; }
  .card.accent-8092 { border-left: 3px solid #22d3ee; }
  .card.accent-8093 { border-left: 3px solid #c084fc; }
  .card.accent-8094 { border-left: 3px solid #fbbf24; }
  .card.accent-8083 { border-left: 3px solid #38bdf8; }
  .card.accent-8085 { border-left: 3px solid #f472b6; }
  .card.accent-8086 { border-left: 3px solid #34d399; }
  .card.accent-8080 { border-left: 3px solid #22d3ee; }

  /* ── 卡片头（可点击 toggle）── */
  .card-toggle { display: flex; align-items: center; gap: 10px; width: 100%; padding: 14px 22px; background: none; border: none; color: inherit; font: inherit; cursor: pointer; text-align: left; user-select: none; transition: background 0.2s; }
  .card-toggle:hover { background: rgba(255,255,255,0.03); }
  .card-toggle:focus-visible { outline: 2px solid var(--brand-cyan); outline-offset: -2px; }
  .card-toggle:active { transform: scale(0.98); }
  .card-toggle .ct-name { font-size: 16px; font-weight: 600; flex-shrink: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .card-toggle .ct-port { font-size: 13px; color: var(--text-secondary); font-family: var(--font-mono); white-space: nowrap; margin-left: 2px; }
  .card-toggle .ct-summary { font-size: 12px; color: var(--text-tertiary); font-family: var(--font-mono); font-variant-numeric: tabular-nums; white-space: nowrap; margin-left: auto; }
  .card-toggle .ct-arrow { font-size: 12px; color: var(--text-tertiary); transition: transform 0.25s ease; flex-shrink: 0; margin-left: 4px; }
  .card-toggle .ct-arrow.open { transform: rotate(180deg); }
  .card-toggle .badge-group { display: flex; gap: 6px; flex-wrap: wrap; flex-shrink: 0; }

  /* ── 详情区（手风琴体）── */
  .card-detail { max-height: 0; overflow: hidden; opacity: 0; transition: max-height 0.25s ease, opacity 0.2s ease, padding 0.25s ease; padding: 0 22px; }
  .card-detail.open { max-height: 4000px; opacity: 1; padding: 0 22px 18px 22px; }
  .card-detail > *:first-child { margin-top: 0; }
  /* 内嵌子容器：把 kv/统计/模型/凭据 分成独立视觉区块，避免展开后"杂货铺"感 */
  .card-detail > .kv, .card-detail > .card-desc, .card-detail > .stats-block, .card-detail > .model-section, .card-detail > .token-edit { background: linear-gradient(180deg, rgba(255,255,255,0.02), rgba(255,255,255,0.004)), var(--bg-inset); border: 1px solid rgba(148,163,184,0.10); border-radius: var(--radius-sm); }
  .card-detail > .kv { padding: 12px 14px; margin: 12px 0 0 0; }
  .card-detail > .card-desc { padding: 10px 14px; margin: 10px 0 0 0; }
  .card-detail > .stats-block { padding: 12px 14px; margin: 10px 0 0 0; }
  .card-detail > .model-section { padding: 12px 14px; margin: 10px 0 0 0; }
  .card-detail > .token-edit { padding: 12px 14px; margin: 10px 0 0 0; }

  /* ── badge（分类 + 状态）：低饱和半透明底 + 细边框，品牌青蓝为主，状态色仅语义用 ── */
  .badge { display: inline-flex; align-items: center; gap: 6px; font-size: 11px; padding: 3px 10px; border-radius: 999px; font-weight: 600; white-space: nowrap; letter-spacing: 0.02em; backdrop-filter: blur(2px); }
  .badge-dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
  /* 分类 badge：crack（品牌青蓝）/ free（语义绿）/ paid（语义橙）/ generic（中性灰） */
  .badge.b-crack { background: rgba(34,211,238,0.10); color: #7dd3fc; border: 1px solid rgba(34,211,238,0.28); }
  .badge.b-crack .badge-dot { background: var(--brand-cyan); box-shadow: 0 0 6px rgba(34,211,238,0.7); }
  .badge.b-free { background: rgba(52,211,153,0.09); color: #6ee7b7; border: 1px solid rgba(52,211,153,0.26); }
  .badge.b-free .badge-dot { background: var(--success); box-shadow: 0 0 6px rgba(52,211,153,0.6); }
  .badge.b-paid { background: rgba(251,191,36,0.09); color: #fcd34d; border: 1px solid rgba(251,191,36,0.26); }
  .badge.b-paid .badge-dot { background: #f59e0b; box-shadow: 0 0 6px rgba(245,158,11,0.6); }
  .badge.b-generic { background: rgba(148,163,184,0.09); color: #aab4c5; border: 1px solid rgba(148,163,184,0.24); }
  .badge.b-generic .badge-dot { background: #9aa3b2; }
  /* 状态 badge：细底 + 状态点 */
  .badge.b-st-green { background: rgba(52,211,153,0.09); color: #6ee7b7; border: 1px solid rgba(52,211,153,0.26); }
  .badge.b-st-green::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: var(--success); box-shadow: 0 0 6px rgba(52,211,153,0.6); flex-shrink: 0; }
  .badge.b-st-blue { background: rgba(59,130,246,0.10); color: #93c5fd; border: 1px solid rgba(59,130,246,0.30); }
  .badge.b-st-blue::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: var(--brand-blue); box-shadow: 0 0 6px rgba(59,130,246,0.6); flex-shrink: 0; }
  .badge.b-st-red { background: rgba(248,113,113,0.09); color: #fca5a5; border: 1px solid rgba(248,113,113,0.26); }
  .badge.b-st-red::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: var(--danger); box-shadow: 0 0 6px rgba(248,113,113,0.6); flex-shrink: 0; }
  .badge.b-st-yellow { background: rgba(251,191,36,0.09); color: #fcd34d; border: 1px solid rgba(251,191,36,0.26); }
  .badge.b-st-yellow::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: var(--warning); flex-shrink: 0; }
  .badge.b-st-purple { background: rgba(192,132,252,0.09); color: #d8b4fe; border: 1px solid rgba(192,132,252,0.26); }
  .badge.b-st-purple::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: #c084fc; flex-shrink: 0; }
  .badge.b-st-orange { background: rgba(251,146,60,0.09); color: #fdba74; border: 1px solid rgba(251,146,60,0.26); }
  .badge.b-st-orange::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: #f59e0b; flex-shrink: 0; }
  .badge.b-st-gray { background: rgba(148,163,184,0.09); color: #aab4c5; border: 1px solid rgba(148,163,184,0.24); }
  .badge.b-st-gray::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: #9aa3b2; flex-shrink: 0; }
  /* 元数据标签：破解/非破解、免费/收费、稳定性 */
  .badge.b-meta-crack { background: rgba(34,211,238,0.08); color: #7dd3fc; border: 1px solid rgba(34,211,238,0.24); }
  .badge.b-meta-normal { background: rgba(148,163,184,0.09); color: #aab4c5; border: 1px solid rgba(148,163,184,0.24); }
  .badge.b-meta-free { background: rgba(52,211,153,0.08); color: #6ee7b7; border: 1px solid rgba(52,211,153,0.24); }
  .badge.b-meta-paid { background: rgba(251,191,36,0.08); color: #fcd34d; border: 1px solid rgba(251,191,36,0.24); }
  .badge.b-meta-stable { background: rgba(52,211,153,0.08); color: #6ee7b7; border: 1px solid rgba(52,211,153,0.24); }
  .badge.b-meta-stable::before { content: '●'; font-size: 8px; margin-right: 3px; color: var(--success); }
  .badge.b-meta-unstable { background: rgba(251,191,36,0.08); color: #fcd34d; border: 1px solid rgba(251,191,36,0.24); }
  .badge.b-meta-unstable::before { content: '◐'; font-size: 9px; margin-right: 3px; color: var(--warning); }
  .badge.b-meta-agg { background: rgba(34,211,238,0.08); color: #7dd3fc; border: 1px solid rgba(34,211,238,0.24); }
  .badge.b-meta-agg::before { content: '◎'; font-size: 9px; margin-right: 3px; color: var(--brand-cyan); }
  .badge.b-meta-gemini { background: rgba(56,189,248,0.08); color: #7dd3fc; border: 1px solid rgba(56,189,248,0.24); }
  .badge.b-meta-gemini::before { content: '◆'; font-size: 8px; margin-right: 3px; color: #38bdf8; }
  .badge.b-meta-oa { background: rgba(129,140,248,0.08); color: #a5b4fc; border: 1px solid rgba(129,140,248,0.24); }
  .badge.b-meta-oa::before { content: '◈'; font-size: 8px; margin-right: 3px; color: #818cf8; }

  /* ── kv 元信息 ── */
  .kv { display: grid; grid-template-columns: 130px 1fr; gap: 6px 16px; font-size: 13px; margin-bottom: 10px; }
  .kv div:nth-child(odd) { color: var(--text-secondary); }
  .card-desc { font-size: 12.5px; color: var(--text-secondary); margin-bottom: 8px; }

  /* ── 流量统计块 ── */
  .stats-block { display: flex; gap: 22px; flex-wrap: wrap; margin: 12px 0 10px 0; }
  .stat-item { display: flex; flex-direction: column; gap: 3px; }
  .stat-label { font-size: 11px; color: var(--text-tertiary); text-transform: uppercase; letter-spacing: 0.8px; font-weight: 600; }
  .stat-value { font-size: 28px; font-weight: 700; color: var(--text-primary); font-family: var(--font-mono); font-variant-numeric: tabular-nums; letter-spacing: -0.5px; line-height: 1.1; text-shadow: 0 0 20px rgba(34,211,238,0.25); }

  /* ── 进度条 ── */
  .rate-bar { display: flex; height: 9px; border-radius: 5px; overflow: hidden; background: rgba(148,163,184,0.08); border: 1px solid rgba(148,163,184,0.10); margin: 10px 0; box-shadow: inset 0 1px 2px rgba(0,0,0,0.3); }
  .rate-bar-seg { transition: width 0.6s ease; }
  .rate-bar-seg.ok { background: linear-gradient(90deg, #10b981, #34d399); box-shadow: 0 0 8px rgba(52,211,153,0.4); }
  .rate-bar-seg.tr429 { background: linear-gradient(90deg, #d97706, #fbbf24); box-shadow: 0 0 8px rgba(251,191,36,0.35); }
  .rate-bar-seg.err { background: linear-gradient(90deg, #dc2626, #f87171); box-shadow: 0 0 8px rgba(248,113,113,0.35); }
  .mini-stats { display: flex; gap: 18px; flex-wrap: wrap; font-size: 12.5px; color: var(--text-secondary); margin-top: 6px; }
  .mini-stats b { color: var(--text-primary); font-family: var(--font-mono); font-variant-numeric: tabular-nums; }

  /* ── 模型表格 ── */
  .model-count { display: inline-block; background: rgba(59,130,246,0.12); color: #93c5fd; font-size: 11px; padding: 2px 8px; border-radius: 999px; margin-left: 6px; font-weight: 600; border: 1px solid rgba(59,130,246,0.28); }
  .no-models { font-size: 12.5px; color: var(--text-tertiary); margin-top: 6px; font-style: italic; }
  .model-table { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 12.5px; table-layout: fixed; }
  .model-table th { text-align: left; padding: 8px 12px; color: var(--text-tertiary); font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 1px solid var(--border); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .model-table th:nth-child(1) { width: 3%; text-align: center; }
  .model-table th:nth-child(2) { width: 18%; }
  .model-table th:nth-child(3) { width: 22%; }
  .model-table th:nth-child(4) { width: 10%; text-align: center; }
  .model-table th:nth-child(5) { width: 12%; text-align: center; }
  .model-table th:nth-child(6) { width: 10%; text-align: center; }
  .model-table th:nth-child(7) { width: 10%; text-align: center; }
  .model-table td { padding: 7px 12px; border-bottom: 1px solid rgba(148,163,184,0.10); overflow-wrap: anywhere; }
  .model-table td.num { color: var(--text-tertiary); font-family: var(--font-mono); text-align: center; }
  .model-table td.mid { font-family: var(--font-mono); overflow-wrap: anywhere; }
  .model-table td.name { color: #c9cedd; }
  .model-table td.mstat { text-align: center; }
  .model-table td.act { width: 36px; text-align: center; }
  .model-table tbody tr:nth-child(even) { background: rgba(255,255,255,0.02); }
  .model-table tbody tr:hover { background: rgba(34,211,238,0.05); }
  .model-table tbody tr:hover { background: rgba(34,211,238,0.06); }
  .mstat { text-align: center; padding: 4px 8px; }
  .mstat.err { color: #f87171; }
  .mstat.warn { color: #fbbf24; }

  /* ── 模型编辑操作行（编辑态切换 + 保存）── */
  .model-ops { display: flex; gap: 8px; margin-top: 8px; align-items: center; flex-wrap: wrap; }
  .model-edit-toggle, .model-save-btn { border-radius: var(--radius-sm); padding: 5px 12px; cursor: pointer; font-size: 12.5px; font-weight: 600; white-space: nowrap; transition: background 0.2s, border-color 0.2s, transform 0.15s, box-shadow 0.2s; }
  .model-edit-toggle { background: transparent; color: var(--brand-cyan); border: 1px solid rgba(34,211,238,0.28); }
  .model-edit-toggle:hover { background: rgba(34,211,238,0.10); border-color: rgba(34,211,238,0.5); transform: translateY(-1px); }
  .model-edit-toggle:active, .model-save-btn:active { transform: scale(0.98); }
  .model-prune-btn { border-radius: var(--radius-sm); padding: 5px 12px; cursor: pointer; font-size: 12.5px; font-weight: 600; white-space: nowrap; transition: background 0.2s, transform 0.15s; background: transparent; color: #fca5a5; border: 1px solid rgba(248,113,113,0.30); }
  .model-prune-btn:hover { background: rgba(248,113,113,0.10); border-color: rgba(248,113,113,0.55); transform: translateY(-1px); }
  .model-prune-btn:disabled { opacity: 0.6; cursor: wait; }
  .model-save-btn { background: var(--brand-grad); color: #fff; border: none; box-shadow: 0 4px 14px rgba(59,130,246,0.35); }
  .model-save-btn:hover { filter: brightness(1.1); box-shadow: 0 6px 20px rgba(34,211,238,0.4); transform: translateY(-1px); }

  /* ── 展示开关（iOS 风格滑动 switch）── */
  .switch { position: relative; display: inline-block; width: 44px; height: 26px; vertical-align: middle; cursor: pointer; flex-shrink: 0; }
  .switch input { opacity: 0; width: 0; height: 0; }
  .switch-slider { position: absolute; inset: 0; background: linear-gradient(135deg, #3a4158, #2c3148); border-radius: 999px; transition: background 0.25s ease; box-shadow: inset 0 1px 3px rgba(0,0,0,0.35), 0 1px 0 rgba(255,255,255,0.04); }
  .switch-slider::before { content: ''; position: absolute; width: 20px; height: 20px; left: 3px; top: 3px; background: radial-gradient(circle at 35% 30%, #f5f7fb, #c7ccd8); border-radius: 50%; transition: transform 0.25s cubic-bezier(0.34, 1.56, 0.64, 1), background 0.25s ease; box-shadow: 0 2px 5px rgba(0,0,0,0.4); }
  .switch input:checked + .switch-slider { background: var(--brand-grad); box-shadow: inset 0 1px 2px rgba(0,0,0,0.15), 0 0 12px rgba(34,211,238,0.30); }
  .switch input:checked + .switch-slider::before { transform: translateX(18px); background: radial-gradient(circle at 35% 30%, #ffffff, #d5f4fc); }
  .switch input:focus-visible + .switch-slider { outline: 2px solid var(--brand-cyan); outline-offset: 2px; }
  .switch input:disabled + .switch-slider { opacity: 0.5; cursor: not-allowed; }

  /* ── 模型编辑 modal ── */
  .modal-overlay { position: fixed; inset: 0; background: rgba(5, 6, 10, 0.8); backdrop-filter: blur(6px); display: none; align-items: center; justify-content: center; z-index: 100; padding: 20px; }
  .modal-overlay.open { display: flex; }
  .modal { background: linear-gradient(180deg, #17172a 0%, #101019 100%); border: 1px solid var(--border-strong); border-radius: 16px; width: 100%; max-width: 640px; max-height: 82vh; display: flex; flex-direction: column; box-shadow: 0 24px 70px rgba(0,0,0,0.65), 0 0 0 1px rgba(34,211,238,0.05), inset 0 1px 0 rgba(255,255,255,0.06); animation: modalIn 0.22s ease; }
  @keyframes modalIn { from { opacity: 0; transform: translateY(14px) scale(0.98); } to { opacity: 1; transform: none; } }
  .modal-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 16px 20px; border-bottom: 1px solid #23263a; }
  .modal-head h3 { margin: 0; font-size: 15px; font-weight: 600; }
  .modal-close { background: none; border: none; color: #6b7280; font-size: 20px; cursor: pointer; line-height: 1; padding: 4px 8px; border-radius: 6px; transition: color 0.2s, background 0.2s; }
  .modal-close:hover { color: #e0e0e0; background: #23263a; }
  .modal-body { overflow-y: auto; padding: 12px 20px; flex: 1; min-height: 0; }
  .modal-foot { display: flex; justify-content: flex-end; gap: 8px; padding: 14px 20px; border-top: 1px solid #23263a; }
  /* modal 内模型行 */
  .mrow { display: flex; align-items: center; gap: 12px; padding: 10px 4px; border-bottom: 1px solid #1f2233; }
  .mrow:last-child { border-bottom: none; }
  .mrow.mrow-master { margin-bottom: 2px; padding: 12px 4px; border-bottom: 1px dashed #3b4060; }
  .mrow .mrow-info { flex: 1; min-width: 0; }
  .mrow .mrow-id { font-family: ui-monospace, monospace; font-size: 13px; color: #e0e0e0; overflow-wrap: anywhere; }
  .mrow .mrow-name { font-size: 12px; color: #8b8fa3; margin-top: 2px; }
  .modal-btn { border-radius: var(--radius-sm); padding: 7px 16px; cursor: pointer; font-size: 13px; font-weight: 600; white-space: nowrap; transition: background 0.2s, border-color 0.2s, transform 0.15s; border: 1px solid rgba(148,163,184,0.28); background: transparent; color: var(--text-secondary); }
  .modal-btn:hover { border-color: rgba(34,211,238,0.5); background: rgba(34,211,238,0.08); color: #fff; transform: translateY(-1px); }
  .modal-btn-primary { background: var(--brand-grad); color: #fff; border: none; box-shadow: 0 4px 14px rgba(59,130,246,0.35); }
  .modal-btn-primary:hover { filter: brightness(1.1); box-shadow: 0 6px 20px rgba(34,211,238,0.4); }
  .modal-btn:active { transform: scale(0.98); }
  .modal-msg { font-size: 12.5px; color: #9ca3af; margin-right: auto; align-self: center; }
  .modal-msg.success { color: #4ade80; }
  .modal-msg.danger { color: #f87171; }
  .mrow-all-hint { font-size: 12px; color: #6b7280; margin: 4px 0 8px 0; }
  /* modal 内搜索框 */
  .model-search-wrap { margin-bottom: 10px; }
  .model-search { width: 100%; padding: 8px 12px; background: var(--bg-inset); border: 1px solid var(--border); border-radius: var(--radius-sm); color: var(--text-primary); font-size: 13px; transition: border-color 0.2s; }
  .model-search::placeholder { color: var(--text-tertiary); }
  .model-search:focus { outline: 2px solid rgba(34,211,238,0.35); border-color: var(--border-focus); }

  .model-msg { font-size: 12px; color: #9ca3af; margin-top: 4px; min-height: 18px; }
  .model-msg.ok { color: #4ade80; }
  .model-msg.err { color: #f87171; }

  /* ── 模型编辑：每行删除按钮 + 底部添加行 ── */
  .mrow-del { background: none; border: none; color: var(--text-tertiary); cursor: pointer; font-size: 15px; line-height: 1; padding: 4px 8px; border-radius: 6px; flex-shrink: 0; transition: color 0.2s, background 0.2s; }
  .mrow-del:hover { color: var(--danger); background: rgba(248,113,113,0.12); }
  .mrow-add { display: flex; gap: 8px; margin-top: 12px; align-items: center; }
  .mrow-add-input { flex: 1; min-width: 0; background: var(--bg-inset); border: 1px solid var(--border); border-radius: var(--radius-sm); color: var(--text-primary); padding: 7px 10px; font-size: 12.5px; font-family: var(--font-mono); transition: border-color 0.2s, box-shadow 0.2s; }
  .mrow-add-input:focus { outline: none; border-color: var(--brand-cyan); box-shadow: 0 0 0 2px rgba(34,211,238,0.15); }
  .mrow-add-btn { background: var(--brand-grad); color: #fff; border: none; border-radius: var(--radius-sm); padding: 7px 14px; cursor: pointer; font-size: 12.5px; font-weight: 600; white-space: nowrap; flex-shrink: 0; box-shadow: 0 3px 12px rgba(59,130,246,0.30); transition: filter 0.2s, transform 0.15s; }
  .mrow-add-btn:hover { filter: brightness(1.1); }
  .mrow-add-btn:active { transform: scale(0.98); }

  /* ── 聚合网关 / 转发配置编辑 modal ── */
  .modal-wide { max-width: 860px; }
  .agg-section { margin-bottom: 16px; }
  .agg-section-title { font-size: 11.5px; color: var(--text-tertiary); text-transform: uppercase; letter-spacing: 0.6px; font-weight: 600; margin-bottom: 8px; }
  .agg-hint { font-size: 11.5px; color: var(--text-tertiary); margin: 2px 0 10px; line-height: 1.5; }
  .agg-fields { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 10px; }
  .agg-field { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
  .agg-label { font-size: 11px; color: var(--text-secondary); }
  .agg-input { background: var(--bg-inset); border: 1px solid var(--border); border-radius: var(--radius-sm); color: var(--text-primary); padding: 7px 10px; font-size: 12.5px; font-family: var(--font-mono); transition: border-color 0.2s, box-shadow 0.2s; min-width: 0; }
  .agg-input:focus { outline: none; border-color: var(--brand-cyan); box-shadow: 0 0 0 2px rgba(34,211,238,0.15); }
  .agg-vm { border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 12px 14px; margin-bottom: 12px; background: linear-gradient(180deg, rgba(255,255,255,0.02), rgba(255,255,255,0.004)), var(--bg-inset); }
  /* 虚拟模型成员明细（折叠，默认收起——model-table 为主监控列表，明细避免重复） */
  .agg-vm-detail { border: 1px solid var(--border); border-radius: var(--radius-sm); margin-bottom: 8px; background: var(--bg-inset); }
  .agg-vm-detail summary { cursor: pointer; list-style: none; padding: 8px 12px; font-size: 12.5px; color: var(--text-primary); display: flex; align-items: center; gap: 8px; user-select: none; }
  .agg-vm-detail summary::-webkit-details-marker { display: none; }
  .agg-vm-detail summary .agg-vm-sum { color: var(--text-tertiary); font-size: 11.5px; }
  .agg-vm-detail summary .agg-arrow { margin-left: auto; color: var(--text-tertiary); font-size: 10px; transition: transform 0.2s; }
  .agg-vm-detail[open] summary .agg-arrow { transform: rotate(180deg); }
  .agg-vm-detail .agg-vm-body { padding: 2px 12px 10px; border-top: 1px dashed var(--border); }
  .agg-vm-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
  .agg-vm-id { flex: 1; min-width: 0; }
  .agg-pool { margin: 8px 0 2px 0; }
  .agg-pool-title { font-size: 11.5px; color: var(--text-tertiary); font-weight: 600; margin-bottom: 6px; }
  .agg-pool-row { display: flex; align-items: center; gap: 8px; padding: 5px 0; }
  .agg-mem-port { width: 180px; flex-shrink: 0; }
  .agg-mem-model { flex: 1; min-width: 200px; }
  .agg-mem-weight { width: 84px; flex-shrink: 0; }
  .agg-add-row { margin: 6px 0 10px 0; }
  .agg-vm-retries { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 8px; padding-top: 8px; border-top: 1px dashed var(--border); }
  .agg-vm-retries .agg-field { flex: 1; min-width: 140px; }

  /* ── 三个编辑 modal 共享：作用域提示条（明示"这里改什么、不改什么"）── */
  .mm-scope { display: flex; align-items: flex-start; gap: 8px; font-size: 11.5px; line-height: 1.55; color: var(--text-secondary); background: rgba(34,211,238,0.06); border: 1px solid rgba(34,211,238,0.20); border-left: 3px solid var(--brand-cyan); border-radius: var(--radius-sm); padding: 8px 12px; margin: 0 0 12px; }
  .mm-scope .mm-scope-icon { flex-shrink: 0; opacity: 0.85; }
  .mm-scope b { color: var(--text-primary); font-weight: 600; }
  .mm-scope .mm-scope-neg { color: var(--text-tertiary); }

  /* ── 悬空引用全局警示条（dashboard 顶部）── */
  .dangling-bar { display: none; background: rgba(251,191,36,0.08); border: 1px solid rgba(251,191,36,0.30); border-left: 3px solid var(--warning); border-radius: var(--radius-md); padding: 12px 16px; margin-bottom: 18px; }
  .dangling-bar.show { display: block; }
  .dangling-bar .dg-head { display: flex; align-items: center; gap: 8px; font-size: 13px; font-weight: 600; color: var(--warning); margin-bottom: 6px; }
  .dangling-bar .dg-count { font-size: 11px; font-weight: 500; color: var(--text-tertiary); margin-left: auto; }
  .dangling-bar ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
  .dangling-bar li { font-size: 12px; color: var(--text-secondary); line-height: 1.5; }
  .dangling-bar li code { font-family: var(--font-mono); font-size: 11.5px; color: var(--text-primary); background: rgba(148,163,184,0.12); border-radius: 4px; padding: 1px 5px; margin-right: 6px; }

  /* ── 卡片内联 token 编辑 ── */
  .token-edit { margin-top: 8px; padding-top: 8px; border-top: 1px dashed rgba(148,163,184,0.16); }
  .te-status { font-size: 12px; color: var(--text-secondary); margin-bottom: 6px; }
  .te-row { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
  .te-input { flex: 1; min-width: 120px; background: var(--bg-inset); border: 1px solid var(--border); border-radius: var(--radius-sm); color: var(--text-primary); padding: 6px 8px; font-size: 13px; }
  .te-input:focus { outline: none; border-color: var(--brand-cyan); box-shadow: 0 0 0 2px rgba(34,211,238,0.15); }
  .te-save, .te-recrack { background: var(--brand-grad); color: #fff; border: none; border-radius: var(--radius-sm); padding: 6px 12px; cursor: pointer; font-size: 13px; font-weight: 600; white-space: nowrap; transition: filter 0.2s, transform 0.15s, box-shadow 0.2s; box-shadow: 0 3px 12px rgba(59,130,246,0.30); }
  .te-recrack { background: transparent; color: #b6bdd0; border: 1px solid rgba(148,163,184,0.28); box-shadow: none; }
  .te-recrack:disabled { background: transparent; color: var(--text-tertiary); cursor: not-allowed; border-color: rgba(148,163,184,0.16); transform: none !important; box-shadow: none; }
  .te-recrack:disabled:hover { background: transparent; transform: none; }
  .te-save:hover { filter: brightness(1.1); box-shadow: 0 5px 18px rgba(34,211,238,0.4); transform: translateY(-1px); }
  .te-recrack:hover { border-color: rgba(34,211,238,0.5); background: rgba(34,211,238,0.08); color: #fff; transform: translateY(-1px); }
  .te-save:active, .te-recrack:active { transform: scale(0.98); }

  /* ── 总览栏操作按钮 + 消息 ── */
  .ov-btn { background: transparent; color: #c2cbdc; border: 1px solid rgba(148,163,184,0.28); border-radius: var(--radius-sm); padding: 7px 16px; cursor: pointer; font-size: 13px; font-weight: 600; transition: background 0.2s, border-color 0.2s, transform 0.15s, box-shadow 0.2s; }
  .ov-btn:hover { background: rgba(34,211,238,0.08); border-color: rgba(34,211,238,0.5); color: #fff; transform: translateY(-1px); }
  .ov-btn:active { transform: scale(0.98); }
  .ov-btn-primary { background: var(--brand-grad); color: #fff; border: none; box-shadow: 0 4px 16px rgba(59,130,246,0.35); }
  .ov-btn-primary:hover { background: var(--brand-grad); filter: brightness(1.1); border: none; color: #fff; box-shadow: 0 6px 24px rgba(34,211,238,0.45); }
  .ov-btn:disabled { opacity: 0.6; cursor: not-allowed; transform: none; }
  .ov-msg { font-size: 12.5px; color: var(--text-secondary); margin-left: 4px; flex-basis: 100%; text-align: right; }
  .ov-msg.success { color: var(--success); }
  .ov-msg.danger { color: var(--danger); }

  /* ── 响应式：窄屏 ≤ 768px ── */
  @media (max-width: 768px) {
    body { padding: 16px; }
    .card-toggle { padding: 14px 16px; }
    .card-toggle .ct-name { white-space: normal; overflow: visible; font-size: 14px; }
    .card-toggle .ct-port { font-size: 12px; }
    .card-toggle .ct-summary { display: none; }
    .card-detail { padding: 0 16px; }
    .card-detail.open { padding: 0 16px 14px 16px; }
    .overview-bar { gap: 14px; padding: 14px; flex-direction: column; }
    .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
    .kpi-value { font-size: 26px; }
    .ov-side { flex-direction: row; align-items: center; justify-content: space-between; width: 100%; }
    .kv { grid-template-columns: 1fr 2fr; }
    .stats-block { gap: 12px; }
    .model-table { display: block; overflow-x: auto; -webkit-overflow-scrolling: touch; }
    .te-row { flex-direction: column; align-items: stretch; }
    .te-save, .te-recrack { align-self: flex-start; }
    .model-table { display: block; overflow-x: auto; -webkit-overflow-scrolling: touch; }
  }

  /* ── 动效降级 ── */
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { transition: none !important; animation: none !important; }
  }

  /* ── 破解网关：额度/签到状态展示 ── */
  .crack-status { margin-top: 8px; padding: 8px 10px; background: var(--bg-inset); border: 1px solid rgba(148,163,184,0.12); border-radius: var(--radius-sm); font-size: 12px; line-height: 1.6; }
  .cs-loading { color: #8b93a7; }
  .cs-err { color: #f87171; }
  .cs-head { color: #9aa3b8; margin-bottom: 4px; font-weight: 600; }
  .cs-row { display: flex; justify-content: space-between; gap: 8px; color: #c9d1e3; }
  .cs-row .k { color: #8b93a7; }
  .cs-checkin-ok { color: #34d399; }
  .cs-checkin-no { color: #fbbf24; }
  .cs-never { color: #6b7280; font-style: italic; }
  .cs-quota { border-top: 1px dashed #262a3a; margin-top: 6px; padding-top: 6px; }
  .cs-qrow { display: flex; justify-content: space-between; gap: 8px; color: #b6bfd4; }
  .cs-qrow .qname { color: #8b93a7; }
  .cs-qrow .qexp { color: #6b7280; font-size: 11px; }

  /* ── 8080 聚合卡：虚拟模型/会话/熔断状态展示 ── */
  .agg-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; vertical-align: 1px; flex-shrink: 0; }
  .agg-dot.ok { background: var(--success); box-shadow: 0 0 6px rgba(52,211,153,0.6); }
  .agg-dot.warn { background: var(--warning); box-shadow: 0 0 6px rgba(251,191,36,0.5); }
  .agg-dot.bad { background: var(--danger); box-shadow: 0 0 6px rgba(248,113,113,0.5); }
  .agg-dot.dim { background: #4b5563; }
  .agg-vm { border-top: 1px dashed #262a3a; margin-top: 6px; padding-top: 6px; }
  .agg-vm-head { color: #7dd3fc; font-weight: 600; font-size: 12px; margin-bottom: 3px; }
  .agg-vm-row { display: flex; justify-content: space-between; align-items: center; gap: 8px; color: #b6bfd4; padding: 1px 0; font-size: 12px; }
  .agg-vm-row .m { color: #c9d1e3; font-family: var(--font-mono); font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .agg-vm-row .s { color: #8b93a7; flex-shrink: 0; }
  .agg-brk { display: flex; align-items: center; gap: 6px; color: #c9d1e3; padding: 1px 0; font-size: 12px; }
  .agg-brk .m { font-family: var(--font-mono); font-size: 11px; color: #7dd3fc; }
  .agg-brk .reason { color: #8b93a7; font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }

  /* ── 凭据管理按钮 ── */
  .te-cred-btn { background: transparent; color: #c2cbdc; border: 1px solid rgba(148,163,184,0.28); border-radius: var(--radius-sm); padding: 7px 14px; cursor: pointer; font-size: 13px; font-weight: 600; transition: background 0.2s, border-color 0.2s, transform 0.15s; }
  .te-cred-btn:hover { border-color: rgba(34,211,238,0.5); color: var(--brand-cyan); background: rgba(34,211,238,0.08); transform: translateY(-1px); }

  /* ── 凭据管理弹窗（表单/JSON 双模式）── */
  .cred-modal { position: fixed; inset: 0; background: rgba(5,6,10,0.78); backdrop-filter: blur(5px); display: none; align-items: center; justify-content: center; z-index: 1000; }
  .cred-modal.open { display: flex; }
  .cred-box { background: linear-gradient(180deg, #17172a 0%, #101019 100%); border: 1px solid var(--border-strong); border-radius: 14px; padding: 18px 22px; width: 560px; max-width: 92vw; max-height: 80vh; overflow-y: auto; box-shadow: 0 24px 70px rgba(0,0,0,0.6), inset 0 1px 0 rgba(255,255,255,0.05); }
  .cred-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
  .cred-head h3 { margin: 0; color: var(--text-primary); font-size: 16px; font-weight: 600; }
  .cred-close { background: none; border: none; color: var(--text-secondary); font-size: 20px; cursor: pointer; line-height: 1; }
  .cred-tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--border); margin-bottom: 14px; }
  .cred-tab { background: none; border: none; color: var(--text-secondary); padding: 8px 16px; cursor: pointer; border-bottom: 2px solid transparent; font-size: 13px; transition: color 0.2s; }
  .cred-tab.active { color: var(--brand-cyan); border-bottom-color: var(--brand-cyan); }
  .cred-field { margin-bottom: 12px; }
  .cred-field label { display: block; color: var(--text-primary); font-size: 13px; margin-bottom: 4px; }
  .cred-field input { width: 100%; background: var(--bg-inset); color: #d5dcea; border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 8px 10px; font-size: 13px; box-sizing: border-box; }
  .cred-field input:focus { outline: 2px solid rgba(34,211,238,0.30); border-color: var(--border-focus); }
  .cred-field .cred-hint { display: block; color: var(--text-tertiary); font-size: 11px; margin-top: 3px; }
  .cred-field .cred-field-err { display: block; color: var(--danger); font-size: 11px; min-height: 14px; }
  .cred-req { color: var(--danger); }
  .cred-readonly { color: var(--text-tertiary); font-size: 11px; margin-top: 8px; padding: 6px 8px; background: var(--bg-inset); border-radius: var(--radius-sm); }
  .cred-foot { display: flex; justify-content: flex-end; align-items: center; gap: 8px; margin-top: 14px; }
  .cred-msg { flex: 1; font-size: 12px; }
  .cred-msg.ok { color: #4ade80; }
  .cred-msg.err { color: #f87171; }
  .cred-msg.warn { color: #fbbf24; }
  .cred-cancel { background: transparent; color: #c2cbdc; border: 1px solid rgba(148,163,184,0.28); border-radius: var(--radius-sm); padding: 7px 16px; cursor: pointer; font-weight: 600; transition: border-color 0.2s, background 0.2s; }
  .cred-cancel:hover { border-color: rgba(34,211,238,0.5); background: rgba(34,211,238,0.08); color: #fff; }
  .cred-save { background: var(--brand-grad); color: #fff; border: none; border-radius: var(--radius-sm); padding: 7px 16px; cursor: pointer; font-weight: 600; box-shadow: 0 4px 14px rgba(59,130,246,0.35); transition: filter 0.2s, transform 0.15s; }
  .cred-save:hover { filter: brightness(1.1); transform: translateY(-1px); }
  .cred-pane textarea { width: 100%; height: 150px; background: var(--bg-inset); color: #d5dcea; border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 10px; font-family: monospace; font-size: 12px; box-sizing: border-box; resize: vertical; }

  /* ── 编辑器统一层级：模型 / 凭据复用聚合配置的字段语言 ── */
  .model-editor-summary { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 9px 12px; margin: 0 0 10px; background: linear-gradient(180deg, rgba(34,211,238,0.06), rgba(255,255,255,0.015)), var(--bg-inset); border: 1px solid var(--border); border-radius: var(--radius-sm); }
  .model-editor-summary .mrow-all-hint { margin: 0; color: var(--text-secondary); }
  .model-editor-list { border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg-inset); overflow: hidden; }
  .model-editor-list .mrow { padding: 10px 12px; border-color: var(--border); transition: background 0.2s, border-color 0.2s; }
  .model-editor-list .mrow:hover { background: rgba(34,211,238,0.055); }
  .model-editor-list .mrow.mrow-master { margin: 0; background: rgba(34,211,238,0.045); border-bottom-style: solid; border-bottom-color: rgba(34,211,238,0.22); }
  .model-editor-list .mrow-id { color: var(--text-primary); }
  .model-editor-list .mrow-name { color: var(--text-secondary); }
  .model-editor-add { padding-top: 2px; }
  .mm-row { display: grid; grid-template-columns: minmax(140px, 1fr) minmax(150px, 1fr) 180px minmax(200px, 1.35fr) auto; gap: 8px; align-items: center; padding: 10px; margin-bottom: 8px; background: linear-gradient(180deg, rgba(255,255,255,0.02), rgba(255,255,255,0.004)), var(--bg-inset); border: 1px solid var(--border); border-radius: var(--radius-sm); transition: border-color 0.2s, background 0.2s; }
  .mm-row:hover { border-color: rgba(34,211,238,0.3); background: rgba(34,211,238,0.035); }
  .mm-row .agg-mem-port { width: 100%; }
  .mm-row .agg-mem-model { min-width: 0; }
  .mm-hint { font-size: 11.5px; line-height: 1.55; color: var(--text-tertiary); margin: 0 0 14px; padding: 9px 12px; border-left: 2px solid var(--brand-cyan); background: rgba(34,211,238,0.045); border-radius: 0 var(--radius-sm) var(--radius-sm) 0; }
  .mm-del, .mm-add-btn { border-radius: var(--radius-sm); font-size: 12px; font-weight: 600; cursor: pointer; transition: background 0.2s, border-color 0.2s, transform 0.15s; }
  .mm-del { border: 1px solid transparent; background: transparent; color: var(--text-tertiary); padding: 6px 8px; }
  .mm-del:hover { color: var(--danger); border-color: rgba(248,113,113,0.3); background: rgba(248,113,113,0.1); }
  .mm-add-btn { border: 1px solid rgba(34,211,238,0.28); background: rgba(34,211,238,0.06); color: var(--brand-cyan); padding: 7px 12px; }
  .mm-add-btn:hover { border-color: rgba(34,211,238,0.5); background: rgba(34,211,238,0.12); transform: translateY(-1px); }
  .cred-modal { padding: 20px; }
  .cred-modal .modal { max-width: 560px; max-height: 82vh; }
  .cred-modal .modal-body { padding-top: 14px; }
  .cred-tabs { gap: 0; margin: -2px 0 14px; border-bottom-color: var(--border); }
  .cred-tab { padding: 8px 14px; font-weight: 600; }
  .cred-field { display: flex; flex-direction: column; gap: 4px; margin-bottom: 12px; }
  .cred-field label { display: flex; align-items: center; gap: 4px; color: var(--text-secondary); font-size: 11px; margin: 0; }
  .cred-field input, .cred-pane textarea { color: var(--text-primary); font-family: var(--font-mono); }
  .cred-field input { padding: 7px 10px; }
  .cred-field .cred-hint { margin: 0; line-height: 1.4; }
  .cred-field .cred-field-err { min-height: 0; }
  .cred-readonly { margin-top: 10px; border: 1px solid var(--border); }
  .cred-pane textarea:focus { outline: none; border-color: var(--brand-cyan); box-shadow: 0 0 0 2px rgba(34,211,238,0.15); }
  @media (max-width: 768px) {
    .modal-overlay { padding: 12px; }
    .modal-wide { max-width: 100%; }
    .mm-row { grid-template-columns: 1fr; }
    .mm-row .agg-mem-port, .mm-row .agg-mem-model { width: 100%; }
    .mm-row .mm-del { justify-self: end; }
  }
"""


def _html_escape(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


_LAN_IP_CACHE: Optional[str] = None


def _get_lan_ip() -> str:
    """探测本机局域网 IP（dashboard 展示可粘贴 base_url 用）。

    优先取 UDP 出口探测（能连外网时最准），回退网卡枚举 / hostname。
    结果缓存，避免每次渲染都探测。
    """
    global _LAN_IP_CACHE
    if _LAN_IP_CACHE:
        return _LAN_IP_CACHE
    # 方法1：UDP 出口探测（不实际发包）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            _LAN_IP_CACHE = ip
            return ip
    except Exception:
        pass
    # 方法2：枚举网卡地址
    try:
        for ifname in ("eth0", "ens3", "enp0s3", "enp1s0", "wlan0"):
            try:
                import fcntl, struct as _st
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                addr = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, _st.pack('256s', ifname[:15].encode()))[20:24])
                s.close()
                if addr and not addr.startswith("127."):
                    _LAN_IP_CACHE = addr
                    return addr
            except Exception:
                continue
    except Exception:
        pass
    # 方法3：hostname 解析
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith("127."):
            _LAN_IP_CACHE = ip
            return ip
    except Exception:
        pass
    _LAN_IP_CACHE = "127.0.0.1"
    return _LAN_IP_CACHE



def _format_uptime(started_at_str):
    """ISO 时间串 → 人类可读运行时长（如 '1天2小时' / '35分钟' / '12秒'）。"""
    if not started_at_str:
        return "—"
    try:
        started = datetime.fromisoformat(started_at_str)
        now = datetime.now()
        # Handle naive datetime (no tzinfo) — assume local time
        if started.tzinfo is not None and hasattr(started.tzinfo, 'utcoffset'):
            now = datetime.now(started.tzinfo)
        delta = now - started
        total_seconds = int(delta.total_seconds())
        if total_seconds < 0:
            return "—"
        days = total_seconds // 86400
        hours = (total_seconds % 86400) // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60
        if days > 0:
            return f"{days}天{hours}小时"
        if hours > 0:
            return f"{hours}小时{minutes}分钟"
        if minutes > 0:
            return f"{minutes}分钟{seconds}秒"
        return f"{seconds}秒"
    except Exception:
        return "—"


def _model_details_html(models, model_stats=None, label=None, edit_mode=False, can_prune=False, col_429="429", target_index=-1):
    """模型列表表格（正常态）+ 模型编辑 modal 内容（edit_mode）。

    支持 models 为字符串列表（默认启用）、dict 列表（含 id/display_name/enabled）。
    label: target label，用于编辑入口按钮的 data-label；为 None 时无编辑能力（如 8081 卡片）。
    edit_mode: True 时返回 modal 编辑界面 HTML：全部模型 + 每个模型的 iOS 风格滑动开关（无删除按钮）。
    can_prune: 该网关上游是否支持 /models（copilot 系支持；codebuddy/qclaw/trae-work 不支持，
               不显示"清理过期模型"按钮，避免点击报"上游不可达"）。
    target_index: 该 target 在 targets[] 中的下标（-1 = 未知），用于 data-path 错误回显。
    """
    editable = label is not None
    # 规范化：统一为 [{id, display, enabled, aliases}]
    norm = []
    for m in models or []:
        if isinstance(m, dict):
            mid = m.get('id', '')
            display = m.get('display_name', '') or _humanize_model_name(mid)
            enabled = m.get('enabled', True)
            aliases = m.get('aliases') or []
        else:
            mid = str(m)
            display = _humanize_model_name(mid)
            enabled = True
            aliases = []
        if mid:
            norm.append({"id": mid, "display": display, "enabled": enabled,
                         "aliases": [str(a) for a in aliases]})

    visible = [n for n in norm if n.get("enabled", True)]

    # ── 编辑态（modal 内容）：全部模型 + 滑动开关 + 每行删除 + 底部添加行 ──
    if edit_mode:
        esc_label = _html_escape(label or "")
        enabled_count = sum(1 for n in norm if n.get("enabled", True))
        rows_html = ""
        if not norm:
            rows_html = '<div class="no-models">(暂无模型数据，在下方添加)</div>'
        for i, n in enumerate(norm):
            checked = 'checked' if n.get("enabled", True) else ''
            dp = f' data-path="targets[{target_index}].models[{i}].enabled"' if target_index >= 0 else ''
            rows_html += (
                f'<div class="mrow" data-model="{_html_escape(n["id"])}">'
                f'  <div class="mrow-info">'
                f'    <div class="mrow-id">{_html_escape(n["id"])}</div>'
                f'    <div class="mrow-name">{_html_escape(n["display"])}</div>'
                f'  </div>'
                f'  <label class="switch" title="展示此模型">'
                f'    <input type="checkbox" class="model-show"{dp} data-model="{_html_escape(n["id"])}" {checked}>'
                f'    <span class="switch-slider"></span>'
                f'  </label>'
                f'  <button class="mrow-del" onclick="removeModelRow(this)" title="删除此模型">×</button>'
                f'</div>'
            )
        # 底部添加模型行（自由输入新模型名，保存后进 models 列表）
        add_row = (
            '<div class="mrow-add">'
            '<input type="text" class="mrow-add-input" id="model-add-input" '
            'placeholder="输入新模型名，保存后加入列表…" aria-label="新模型名">'
            '<button class="mrow-add-btn" onclick="addModelRow()">+ 添加模型</button>'
            '</div>'
        )
        hint = f'<div class="mrow-all-hint">共 {len(norm)} 个模型，已开启 {enabled_count} 个</div>' if norm else ''
        # 总开关：全开/全关/部分开（indeterminate），联动所有子开关
        master = ""
        if norm:
            master = (
                '<div class="mrow mrow-master" id="model-master-row">'
                '  <div class="mrow-info">'
                '    <div class="mrow-id">全部模型</div>'
                '    <div class="mrow-name">总开关，一键全开 / 全关</div>'
                '  </div>'
                '  <label class="switch" title="全开/全关">'
                '    <input type="checkbox" class="model-master" '
                + ('checked' if enabled_count == len(norm) else '')
                + '>'
                '    <span class="switch-slider"></span>'
                '  </label>'
                '</div>'
            )
        # 搜索始终置顶，保证较短列表也有一致的编辑入口。
        search = (
            '<div class="model-search-wrap">'
            '<input type="text" class="model-search" placeholder="搜索模型…" '
            'oninput="filterModels(this)" aria-label="搜索模型">'
            '</div>'
        )
        # 作用域提示（docs §2.4.1）：明示本页只是该端口的透传白名单
        scope = (
            '<div class="mm-scope"><span class="mm-scope-icon">i</span><span>'
            f'<b>本页仅控制 {_html_escape(label or "")} 端口的透传白名单（开关=是否对外暴露）。</b>'
            ' <span class="mm-scope-neg">不影响其他端口，也不改变 8081 模型定义与 8080 聚合路由。</span>'
            '</span></div>'
        )
        return (
            f'{scope}{search}<div class="model-editor-summary">{master}{hint}</div>'
            f'<div class="model-editor-list">{rows_html}</div>'
            f'<div class="model-editor-add">{add_row}</div>'
            f'<div class="model-msg" data-label="{esc_label}"></div>'
        )

    # ── 正常态表格（只展示启用模型，无删除按钮）──
    if not visible:
        if not editable:
            return '<div class="no-models">(暂无模型数据)</div>'
        return (
            '<div class="no-models">(暂无展示中的模型，点击「编辑模型」开启)</div>'
            f'<div class="model-ops">'
            f'  <button class="model-edit-toggle" data-label="{_html_escape(label)}" onclick="openModelEditor(this)">✏️ 编辑模型</button>'
            f'</div>'
        )

    has_stats = model_stats is not None
    esc_label = _html_escape(label or "")
    # 别名列：数据源含 aliases 字段即渲染（8081 卡片 models[] 定义始终显示，与编辑视图一致；
    # 空别名显示 —。其他 target 卡片无 aliases 字段则不显示，避免无谓加宽）
    has_alias_col = any("aliases" in n for n in visible)

    rows = []
    for i, n in enumerate(visible, 1):
        mid = n["id"]
        row = (
            f'<tr data-model="{_html_escape(mid)}">'
            f'<td class="num">{i}</td>'
            f'<td class="mid"><code>{_html_escape(mid)}</code></td>'
            f'<td class="name">{_html_escape(n["display"])}</td>'
        )
        if has_alias_col:
            alias_txt = ", ".join(_html_escape(a) for a in n.get("aliases") or [])
            row += f'<td class="alias">{alias_txt or "—"}</td>'
        if has_stats:
            ms = model_stats.get(mid) if mid else None
            if ms:
                total = ms.get("requests", 0)
                ok = ms.get("ok", 0)
                err = ms.get("err", 0)
                tr429 = ms.get("translated429", 0)
                rate = round(ok / total * 100, 1) if total > 0 else 100.0
                row += (
                    f'<td class="mstat">{total}</td>'
                    f'<td class="mstat">{rate}%</td>'
                    f'<td class="mstat err">{err}</td>'
                    f'<td class="mstat warn">{tr429}</td>'
                )
            else:
                row += '<td class="mstat">—</td><td class="mstat">—</td><td class="mstat">—</td><td class="mstat">—</td>'
        row += '</tr>'
        rows.append(row)

    alias_th = '<th>别名</th>' if has_alias_col else ''
    header_extra = f'<th>请求</th><th>成功率</th><th>错误</th><th>{_html_escape(str(col_429))}</th>' if has_stats else ''
    table_html = (
        f'<table class="model-table">'
        f'<thead><tr><th>#</th><th>模型 ID</th><th>名称</th>{alias_th}{header_extra}</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody>'
        f'</table>'
    )

    if not editable:
        return table_html

    # 可编辑：正常态表格 + 编辑入口 + 消息区
    edit_toggle = (
        f'<button class="model-edit-toggle" data-label="{esc_label}" onclick="openModelEditor(this)">✏️ 编辑模型</button>'
    )
    prune_toggle = ""
    if can_prune:
        prune_toggle = (
            f'<button class="model-prune-btn" data-label="{esc_label}" onclick="pruneModels(this)" '
            f'title="对照上游最新模型列表，删除已下线的过期模型（同步配置与内存）">'
            f'🧹 清理过期模型</button>'
        )
    return (
        f'{table_html}'
        f'<div class="model-ops">'
        f'  {edit_toggle}'
        f'  {prune_toggle}'
        f'</div>'
        f'<div class="model-msg" data-label="{esc_label}"></div>'
    )


def _build_card_html(name, note, kind_badge, status_badge, status_badge_class,
                     kv_items, stats_detail=None, models=None, model_stats=None, description="",
                     accent_class="", raw_html="", label=None, port=None, meta_badges=None,
                     can_prune=False, col_429="429"):
    """统一卡片渲染（手风琴折叠）：透传目标和定制服务用同一套视觉风格。

    stats_detail: dict with total/ok/err/translated/success_rate/uptime
    accent_class: CSS class for port-specific accent (e.g., 'accent-8082')
    label: target label，传递给模型编辑组件；None 时不显示编辑按钮
    port: 端口号，显示在卡片头
    meta_badges: 额外的分类标签列表 [("文本", "样式类"), ...]，如 [("破解", "b-crack"), ("免费", "b-free")...]
    can_prune: 上游是否支持 /models 清理（copilot 系 true；codebuddy/qclaw/trae-work false 不显示清理按钮）
    """
    # ── 卡片头 badges（分类 badge 带图标点 + 渐变底；状态 badge 带状态点）──
    kind_badge_class = {"破解": "b-crack", "免费": "b-free", "收费": "b-paid"}.get(str(kind_badge), "b-generic")
    badges = f'<span class="badge {kind_badge_class}"><span class="badge-dot"></span>{_html_escape(str(kind_badge))}</span>'
    # 元数据标签：破解/非破解、免费/收费、稳定性
    for meta_text, meta_cls in (meta_badges or []):
        badges += f' <span class="badge {meta_cls}">{_html_escape(str(meta_text))}</span>'
    if status_badge:
        # status_badge_class 可能是 'purple'/'blue'/'green'/'red'/'orange'/'gray' 等 → 映射为 b-status-*
        st_class = {"blue": "b-st-blue", "green": "b-st-green", "red": "b-st-red",
                    "yellow": "b-st-yellow", "purple": "b-st-purple", "orange": "b-st-orange",
                    "gray": "b-st-gray"}.get(str(status_badge_class), "b-st-gray")
        badges += f' <span class="badge {st_class}">{_html_escape(str(status_badge))}</span>'

    # ── 卡片头摘要（请求数）──
    summary = ""
    if stats_detail and stats_detail.get('alive'):
        total = stats_detail.get('total', 0)
        summary = f'<span class="ct-summary">{total} 请求</span>'

    # ── 卡片头 HTML ──
    port_str = f'<span class="ct-port">:{port}</span>' if port else ''
    # 启动状态灯：绿=运行中/红=离线/黄=未监听，带呼吸动画
    lamp_cls = {"blue": "on", "green": "on", "purple": "on", "orange": "on", "red": "off", "gray": "idle", "yellow": "idle"}.get(str(status_badge_class), "idle")
    lamp = f'<span class="ct-lamp {lamp_cls}" title="{_html_escape(str(status_badge))}"></span>'
    header_html = (
        f'<div class="card-toggle" role="button" tabindex="0" aria-expanded="false">'
        f'  {lamp}'
        f'  <span class="ct-name">{_html_escape(name)}</span>{port_str}'
        f'  <span class="badge-group">{badges}</span>'
        f'{summary}'
        f'  <span class="ct-arrow">▼</span>'
        f'</div>'
    )

    # ── 详情区 kv ──
    kv = "".join(
        f"<div>{_html_escape(str(k))}</div><div><code>{_html_escape(str(v))}</code></div>"
        for k, v in kv_items
    )

    # ── 流量统计块（含进度条）──
    stats_html = ""
    if stats_detail and stats_detail.get('alive'):
        ok = stats_detail.get('ok', 0)
        err = stats_detail.get('err', 0)
        tr = stats_detail.get('translated', 0)
        total = stats_detail.get('total', 0)
        success_rate = stats_detail.get('success_rate', 0)
        uptime = stats_detail.get('uptime', '—')

        # 进度条
        bar_html = ""
        if total > 0:
            ok_pct = round(ok / total * 100, 1)
            tr_pct = round(tr / total * 100, 1)
            err_pct = round(err / total * 100, 1)
            bar_html = (
                f'<div class="rate-bar">'
                + (f'<div class="rate-bar-seg ok" style="width:{ok_pct}%" title="成功 {ok}"></div>' if ok_pct > 0 else '')
                + (f'<div class="rate-bar-seg tr429" style="width:{tr_pct}%" title="429 翻译 {tr}"></div>' if tr_pct > 0 else '')
                + (f'<div class="rate-bar-seg err" style="width:{err_pct}%" title="错误 {err}"></div>' if err_pct > 0 else '')
                + f'</div>'
            )

        stats_html = (
            f'<div class="stats-block">'
            f'<div class="stat-item"><span class="stat-label">总请求</span><span class="stat-value">{total}</span></div>'
            f'<div class="stat-item"><span class="stat-label">成功率</span><span class="stat-value">{success_rate}%</span></div>'
            f'<div class="stat-item"><span class="stat-label">运行时长</span><span class="stat-value">{uptime}</span></div>'
            f'</div>'
            f'{bar_html}'
            f'<div class="mini-stats">'
            f'  <div>正常透传 <b>{ok}</b></div>'
            f'  <div>翻译 429 <b>{tr}</b></div>'
            f'  <div>代理错误 <b>{err}</b></div>'
            f'</div>'
        )

    model_html = _model_details_html(models, model_stats, label, edit_mode=False, can_prune=can_prune, col_429=col_429) if models is not None else ""
    card_class = f'card {accent_class}'.strip()

    return f"""<div class="{card_class}" data-label="{_html_escape(label or '')}" data-port="{port or ''}">
  {header_html}
  <div class="card-detail">
  <div class="kv">{kv}</div>
  {f'<div class="card-desc">{_html_escape(description)}</div>' if description else ""}
  {stats_html}
  {f'<div class="model-section" data-label="{_html_escape(label or "")}">{model_html}</div>' if model_html else ""}
  {raw_html}
  </div>
</div>"""


# ══════════════════════════════════════════════════════════════════════════════
# Task 8: 管理 REST API（dashboard 配置管理）
# ══════════════════════════════════════════════════════════════════════════════




@dashboard_router.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """统一管理面板 — 展示本机所有 LLM 相关服务的架构与状态。"""

    # ── 并行拉取各 asyncio TCP 端口的状态 ──
    async def _fetch(port):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0), trust_env=False) as c:
                info_r = await c.get(f"http://127.0.0.1:{port}/__proxy_info__")
                stats_r = await c.get(f"http://127.0.0.1:{port}/__proxy_stats__")
                info = info_r.json() if info_r.status_code == 200 else {}
                stats = stats_r.json() if stats_r.status_code == 200 else {}
                return {
                    "label": info.get("label", f"port-{port}"),
                    "listenPort": port, "upstream": f"{info.get('targetProtocol','https')}://{info.get('targetHost','?')}:{info.get('targetPort',443)}",
                    "models": info.get("models", []),
                    "total": stats.get("totalRequests", 0),
                    "ok": stats.get("passthroughOk", 0),
                    "translated": stats.get("translated429", 0),
                    "err": stats.get("passthroughError", 0),
                    "alive": info_r.status_code == 200,
                    "startedAt": stats.get("startedAt", ""),
                    "modelStats": stats.get("modelStats", {}),
                }
        except Exception:
            return {"label": f"port-{port}", "listenPort": port, "upstream": "?", "models": [], "total": 0, "ok": 0, "translated": 0, "err": 0, "alive": False, "startedAt": "", "modelStats": {}}

    _dash_ports = [t["listenPort"] for t in _srv._TARGETS if t.get("enabled", True)]
    results = await asyncio.gather(*[_fetch(p) for p in _dash_ports]) if _dash_ports else []
    _result_map = {r["listenPort"]: r for r in results}

    def _make_stats_detail(r):
        """构建增强统计字典（成功率、时长、进度条数据）。"""
        if not r["alive"]:
            return None
        total = r["total"]
        ok = r["ok"]
        err = r["err"]
        tr = r.get("translated", 0)
        success_rate = round(ok / total * 100, 1) if total > 0 else 100.0
        uptime = _format_uptime(r.get("startedAt", ""))
        return {
            "total": total, "ok": ok, "err": err, "translated": tr,
            "success_rate": success_rate, "uptime": uptime, "alive": True,
        }

    # ── 总览数据 ──
    total_requests_all = sum(r["total"] for r in results if r["alive"])
    alive_ports = sum(1 for r in results if r["alive"])
    alive_rate = round(alive_ports / len(results) * 100) if results else 0
    _alive_color = "#34d399" if alive_rate == 100 else ("#fbbf24" if alive_rate >= 50 else "#f87171")

    # ── 局域网 IP（可粘贴 base_url 用）──
    _lan_ip = _get_lan_ip()

    # ── 分组：聚合网关(8081) / 破解网关(crack) / 直连网关(free/paid) ──
    agg_cards, crack_cards, direct_cards = [], [], []

    # ── 8080 流量聚合（AggregatorEngine：虚拟模型路由 + 会话粘性 + 熔断）──
    # 监控视角与其他卡片一致：卡头请求数摘要 + 展开区流量统计块 + 成员级 model-table 单表
    # （虚拟模型 + 成员明细整合为一张表：模型 ID 列=虚拟模型，名称列=成员，列与监控表一致 + 延迟）
    # 配置编辑走「✏️ 编辑配置」进入独立 modal；单表由前端 fetch /api/aggregate/status 每 10s 渲染
    _agg_engine = _srv._AGGREGATOR_ENGINE
    _agg_configured = _agg_engine is not None
    _agg_stats_detail = None
    _agg_vm_list = []
    _agg_member_total = 0
    _agg_started_at = 0
    _agg_pool_cfg_json = "{}"
    if _agg_configured:
        # 虚拟模型列表与池成员数取自配置（总有值），统计取自引擎（无流量时计数为 0）
        _agg_cfg_target = next((t for t in _srv._TARGETS if t.get("handler") == "aggregator"), None)
        _agg_cfg_vms = (_agg_cfg_target or {}).get("virtualModels", {})
        _agg_vm_list = list(_agg_cfg_vms.keys())
        _agg_member_total = sum(
            len(v.get("defaultPool") or []) + len(v.get("fallbackPool") or [])
            for v in _agg_cfg_vms.values()
        )
        _agg_full = _agg_engine.get_stats()
        _agg_vms = _agg_full.get("virtual_models", {})
        _agg_tot = _agg_ok = _agg_err = _agg_tr = 0
        for _vm_id in _agg_vm_list:
            _members = _agg_vms.get(_vm_id, {})
            for _m in _members.values():
                _agg_tot += _m.get("requests", 0)
                _agg_ok += _m.get("ok", 0)
                _agg_err += _m.get("err", 0)
                _agg_tr += _m.get("degraded", 0)
        _agg_started_at = _agg_full.get("started_at", 0)
        _agg_rate = round(_agg_ok / _agg_tot * 100, 1) if _agg_tot > 0 else 100.0
        # _format_uptime 接受 ISO 字符串（与 8082 等透传卡一致）；引擎返回 float timestamp，调用点转换
        _agg_started_iso = datetime.fromtimestamp(_agg_started_at).isoformat() if _agg_started_at else ""
        _agg_stats_detail = {
            "total": _agg_tot, "ok": _agg_ok, "err": _agg_err,
            "translated": _agg_tr, "success_rate": _agg_rate,
            "uptime": _format_uptime(_agg_started_iso), "alive": True,
        }
        # 池配置 JSON 注入前端（供 loadAggregateStatus 渲染池详情折叠）
        _agg_pool_cfg_json = json.dumps(_agg_cfg_vms, ensure_ascii=False)
    agg_cards.append(_build_card_html(
        name="流量聚合",
        note="虚拟模型聚合路由 · 会话粘性 · 熔断降级（OpenAI /v1 入口）",
        kind_badge="聚合网关",
        status_badge="运行中" if _agg_configured else "未配置",
        status_badge_class="green" if _agg_configured else "gray",
        kv_items=[
            ("base_url", f"http://{_lan_ip}:8080"),
            ("监听地址", "http://0.0.0.0:8080"),
            ("协议", "OpenAI /v1（虚拟模型 agg:xxx）"),
            ("路由策略", "权重/会话粘性 · 失败降级 · 熔断摘除"),
            ("虚拟模型", f"{len(_agg_vm_list)} 个"),
            ("池成员", f"{_agg_member_total} 个"),
        ],
        stats_detail=_agg_stats_detail,
        # 模型区整合：虚拟模型 + 成员明细统一由前端渲染单表（loadAggregateStatus），服务端不再输出 model-table
        models=None,
        model_stats=None,
        col_429="降级",
        description="虚拟模型 id（agg:xxx）→ 按权重与会话粘性路由到池内成员端口，故障端口自动熔断并从降级池逃生。",
        accent_class="accent-8080",
        label=None,
        port=8080,
        meta_badges=[("熔断降级", "b-meta-normal"), ("OpenAI 协议", "b-meta-oa")],
        raw_html=(
            '<div class="model-ops">'
            '  <button class="model-edit-toggle" onclick="openAggConfigEditor(this)" '
            '    title="编辑聚合网关虚拟模型 / 池默认值 / 重试策略">✏️ 编辑配置</button>'
            '</div>'
            '<div class="crack-status" id="agg-status" data-ref="aggregate">'
            '  <div class="cs-loading">状态加载中…</div>'
            '</div>'
            f'<script type="application/json" id="agg-pool-data">{_agg_pool_cfg_json}</script>'
        ),
    ))

    # ── 8081 Anthropic（FastAPI，本 App 自身）—— 转发网关 ──
    _8081_total = _ANTHROPIC_STATS.get("totalRequests", 0)
    _8081_ok = _ANTHROPIC_STATS.get("passthroughOk", 0)
    _8081_err = _ANTHROPIC_STATS.get("passthroughError", 0)
    _8081_rate = round(_8081_ok / _8081_total * 100, 1) if _8081_total > 0 else 100.0
    # 8081 卡片关联的 target：modelDefaults.defaultPort 对应端口（dashboard 映射按钮定位用）
    _forward_target = next((t for t in _srv._TARGETS if t.get("listenPort") == _srv._MODELS_CFG["modelDefaults"].get("defaultPort", 8082)), None)
    _forward_label = _forward_target["label"] if _forward_target else None
    _ap_models = _anthropic_port_models()
    agg_cards.append(_build_card_html(
        name="anthropic-compatible",
        note="FastAPI · Anthropic 协议入口 · /v1/messages 翻译为 OpenAI 后内部请求 8082",
        kind_badge="Protocol",
        status_badge="运行中",
        status_badge_class="purple",
        kv_items=[
            ("base_url", f"http://{_lan_ip}:8081"),
            ("监听地址", "http://0.0.0.0:8081"),
            ("内部回调", "http://127.0.0.1:8082/v1/chat/completions"),
            ("协议", "Anthropic /v1/messages → OpenAI 翻译"),
            ("模型数量", f"{len(_ap_models)} 个（models[] 定义）"),
            ("systemd 服务", "anthropic-compatible"),
        ],
        models=_ap_models,
        model_stats=_MODEL_STATS.get("anthropic", {}),
        stats_detail={
            "total": _8081_total, "ok": _8081_ok, "err": _8081_err,
            "translated": 0, "success_rate": _8081_rate,
            "uptime": _format_uptime(_ANTHROPIC_STATS.get("startedAt", "")), "alive": True,
        },
        description="接收 Anthropic 客户端请求，结构化解码后转换为 OpenAI 格式，内部转发到 8082（copilot 透传）。响应译回 Anthropic 格式。",
        label=None,
        port=8081,
        raw_html=(
            '<div class="model-ops">'
            '  <button class="model-edit-toggle" onclick="openModelsEditor(this)" '
            '    title="编辑模型定义（name/别名 → 下游端口+真实模型，可指向聚合虚拟模型 agg:xxx）">✏️ 模型定义</button>'
            '</div>'
        ),
        meta_badges=[("Forward Gateway", "b-meta-agg"), ("Anthropic", "b-meta-normal")],
    ))

    # ── 动态 target 卡片（targets.json 驱动）──
    for t in _srv._TARGETS:
        port = t["listenPort"]
        r = _result_map.get(port)
        if r is None:
            try:
                r = await _fetch(port)
            except Exception:
                r = {"label": t["label"], "listenPort": port, "upstream": "?", "models": [], "total": 0, "ok": 0, "translated": 0, "err": 0, "alive": False, "startedAt": "", "modelStats": {}}
        category = t.get("category", "free")
        badge_map = {"crack": "破解", "free": "免费", "paid": "收费"}
        badge_class_map = {"crack": "blue", "free": "green", "paid": "orange"}
        # ── 模型标签分类：破解/非破解 · 免费/收费（破解默认免费，可被 isFree 覆盖）· 稳定性（破解/收费高，免费低）──
        is_crack = category == "crack"
        # 显式设置 isFree 时以配置为准（如企业版 Copilot isFree=false → 收费）；
        # 未设置时按 category 推断：paid=收费，其余免费
        is_free = t.get("isFree") if t.get("isFree") is not None else (category != "paid")
        is_stable = category in ("crack", "paid")  # 破解与收费服务稳定性高
        # 元数据标签：kind_badge 已显示"破解/免费/收费"，这里只保留稳定性 + 协议，避免语义重复
        meta_badges = [
            ("稳定性高" if is_stable else "稳定性低", "b-meta-stable" if is_stable else "b-meta-unstable"),
        ]
        # 协议标签：gemini-native 是 OpenAI↔Gemini 转换；其余 target（crack/透传/trae-work）客户端均走 OpenAI 协议
        if t.get("handler") == "gemini-native":
            meta_badges.append(("Gemini协议", "b-meta-gemini"))
        else:
            meta_badges.append(("OpenAI 协议", "b-meta-oa"))
        secret = _cfg.resolve_secret(t, _srv._SECRETS)
        # 可粘贴 base_url：局域网 IP + 本机端口 + 后缀（客户端直接可用）
        # - crack 类：我们自己定义 base_url 规范，客户端统一 /v1，代理内部映射到下游
        # - gemini-native：客户端走 OpenAI 协议入口 /v1
        # - free/paid 透传：直接用上游 routePrefix（如 /api/v1）
        if t.get("category") == "crack" or t.get("handler") == "gemini-native":
            _base_suffix = "/v1"
        else:
            _base_suffix = t.get("routePrefix", "")
        _base_url = f"http://{_lan_ip}:{port}{_base_suffix}"
        kv = [
            ("base_url", _base_url),
            ("分类", badge_map.get(category, category)),
            ("handler", t.get("handler", "passthrough")),
            ("上游", f"{t.get('targetProtocol','https')}://{t['targetHost']}:{t.get('targetPort',443)}{t.get('routePrefix','')}"),
        ]
        if t.get("isFree") is not None:
            kv.append(("isFree", "是（免费）" if t["isFree"] else "否（收费）"))
        if t.get("enabled") is False:
            kv.append(("状态", "预留（未监听）"))

        # ── 卡片内联 token 编辑块 ──
        # 直连网关（free/paid）无 secretRef 时退回约定 key f"{label}_token"，
        # 与 config_store.secret_key_for / PUT /api/secrets/{label} 保持一致
        sec_ref = _cfg.secret_key_for(t)
        esc_label = t["label"].replace("'", "\\'")
        # 破解环境检测：不可用则置灰 + title 提示
        recrack_btn = ""
        if t.get('category') == 'crack' and t.get('crackTool'):
            env = _crack_env_check(t)
            if env.get("available"):
                recrack_btn = f'<button class="te-recrack" onclick="recrackCard(\'{esc_label}\', this)">重新破解</button>'
            else:
                recrack_btn = (
                    f'<button class="te-recrack" disabled title="{_html_escape(env.get("reason", "环境依赖缺失"))}">'
                    f'重新破解</button>'
                )
        token_status = "✅ 已配置 " + _cfg.mask_secret(secret) if secret else "⚠️ 缺失"
        input_placeholder = "已配置，输入新值覆盖" if secret else "填写 " + (sec_ref or "token")
        input_value = "******" if secret else ""
        # 破解网关扩展：凭据管理按钮 + 额度/签到状态展示容器
        crack_status_html = ""
        is_crack = t.get('category') == 'crack'
        if is_crack:
            crack_status_html = (
                f'<div class="crack-status" id="cs-{port}" data-label="{esc_label}" '
                f'data-ref="{sec_ref}">'
                f'  <div class="cs-loading">状态加载中…</div>'
                f'</div>'
            )
        if is_crack:
            # crack 类：统一凭据弹窗（schema 驱动），无内联 password 输入
            token_edit = (
                f'<div class="token-edit" id="te-{port}">'
                f'  <div class="te-status">凭据: {token_status}</div>'
                f'  <div class="te-row">'
                f'    <button class="te-cred-btn" onclick="openCredentialModal(\'{esc_label}\', this)">'
                f'      凭据管理</button>'
                f'    {recrack_btn}'
                f'  </div>'
                f'  {crack_status_html}'
                f'</div>'
            )
        else:
            # free/paid 类：保留单字段 token 编辑
            token_edit = (
                f'<div class="token-edit" id="te-{port}">'
                f'  <div class="te-status">token: {token_status}</div>'
                f'  <div class="te-row">'
                f'    <input type="password" class="te-input" data-label="{esc_label}" data-ref="{sec_ref}"'
                f'           placeholder="{input_placeholder}" value="{input_value}">'
                f'    <button class="te-save" onclick="saveCardToken(\'{esc_label}\', this)">保存</button>'
                f'    {recrack_btn}'
                f'  </div>'
                f'  {crack_status_html}'
                f'</div>'
            )

        card = _build_card_html(
            name=f"{t['label']}",
            note="统一透传引擎 · targets.json 驱动",
            kind_badge=badge_map.get(category, category),
            status_badge="运行中" if r["alive"] else ("未监听" if t.get("enabled") is False else "离线"),
            status_badge_class=badge_class_map.get(category, "gray") if r["alive"] else "red",
            kv_items=kv,
            models=t.get("models", []),
            model_stats=r.get("modelStats") if r.get("alive") else None,
            stats_detail=_make_stats_detail(r),
            description=f"category={category} · handler={t.get('handler','passthrough')} · isFree={t.get('isFree')}",
            accent_class=f"accent-{port}",
            raw_html=token_edit,
            label=t["label"],
            port=port,
            meta_badges=meta_badges,
            # 上游是否支持 /models：优先读 ModelRegistry 单一事实源（P2），
            # 其 capabilities[port].can_prune 与 _target_model_source 判据一致（输出不变）。
            # 回退用 _target_model_source（注册表未就绪时的等价逻辑）。
            can_prune=bool(_srv._MODEL_REGISTRY.capabilities.get(port, {}).get("can_prune")
                       if _srv._MODEL_REGISTRY is not None
                       else (_target_model_source(t) == "copilot" or t.get("hasModels") is True)),
        )
        if category == "crack":
            crack_cards.append(card)
        elif category == "aggregate":
            # 聚合网关卡片已手动构建（含状态区/编辑按钮），循环跳过避免重复
            pass
        else:
            direct_cards.append(card)

    def _render_group(title, cards_list):
        if not cards_list:
            return ""
        return (
            f'<div class="section"><div class="section-title">{title}'
            f'<span class="sec-count">{len(cards_list)}</span></div>'
            f'<div class="card-grid">{"".join(cards_list)}</div></div>'
        )

    cards_html = (
        _render_group("聚合网关", agg_cards)
        + _render_group("破解网关", crack_cards)
        + _render_group("直连网关", direct_cards)
    )
    all_models = _build_models_list()
    # 生成概览栏的状态点
    overview_dots = "".join(
        f'<span class="status-dot {"green" if r["alive"] else "red"}" title="端口 {r["listenPort"]}: {"在线" if r["alive"] else "离线"}"></span>'
        for r in results
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LLM Gateway — 管理总览</title>
<style>{DASHBOARD_STYLE}</style>
</head>
<body>
  <h1>🔀 LLM Gateway — 管理总览</h1>
  <div class="sub">8081 Anthropic (FastAPI) → 8082 copilot (透传) → 上游 · 统一 targets.json 驱动 <span class="refresh-time">· 手动刷新 · {datetime.now().strftime("%H:%M:%S")}</span></div>
  <div class="overview-bar">
    <div class="kpi-grid">
      <div class="kpi-card">
        <span class="kpi-label">服务总数</span>
        <span class="kpi-value">{len(results)}</span>
        <span class="kpi-sub">enabled targets</span>
      </div>
      <div class="kpi-card">
        <span class="kpi-label">累计请求</span>
        <span class="kpi-value">{total_requests_all}</span>
        <span class="kpi-sub">所有存活端口</span>
      </div>
      <div class="kpi-card">
        <span class="kpi-label">存活端口</span>
        <span class="kpi-value">{alive_ports}<small>/{len(results)}</small></span>
        <span class="kpi-sub">在线 / 全部端口</span>
      </div>
      <div class="kpi-card">
        <span class="kpi-label">在线率</span>
        <span class="kpi-value accent">{alive_rate}%</span>
        <span class="kpi-sub"><span class="kpi-dot" style="background:{_alive_color}; box-shadow:0 0 6px {_alive_color};"></span>运行健康度</span>
      </div>
    </div>
    <div class="ov-side">
      <div class="ov-dots">{overview_dots}</div>
      <div class="ov-actions">
        <button class="ov-btn" onclick="exportConfig()">📦 导出配置</button>
        <button class="ov-btn" onclick="document.getElementById('import-file-input').click()">📥 导入配置</button>
        <input type="file" id="import-file-input" accept=".json,application/json" style="display:none" onchange="importConfigFile(this)">
        <button class="ov-btn" onclick="doReload()">♻️ 重载配置</button>
        <button class="ov-btn ov-btn-primary" onclick="location.reload()">🔄 刷新状态</button>
      </div>
    </div>
    <span id="ov-msg" class="ov-msg" role="status"></span>
  </div>
  <div class="dangling-bar" id="dangling-bar" role="status" aria-live="polite"></div>
  {cards_html}

  <!-- 模型编辑 modal -->
  <div class="modal-overlay" id="model-modal" role="dialog" aria-modal="true" aria-label="编辑模型展示">
    <div class="modal modal-wide">
      <div class="modal-head">
        <h3 id="model-modal-title">编辑模型</h3>
        <button class="modal-close" onclick="closeModelEditor()" aria-label="关闭">×</button>
      </div>
      <div class="modal-body" id="model-modal-body"></div>
      <div class="modal-foot">
        <span class="modal-msg" id="model-modal-msg"></span>
        <button class="modal-btn" onclick="closeModelEditor()">取消</button>
        <button class="modal-btn modal-btn-primary" id="model-modal-save" onclick="saveModelEditor(this)">保存</button>
      </div>
    </div>
  </div>

  <!-- 模型定义编辑 modal（全局 models[]：name/aliases/target port+model） -->
  <div class="modal-overlay" id="models-modal" role="dialog" aria-modal="true" aria-label="编辑模型定义">
    <div class="modal">
      <div class="modal-head">
        <h3 id="models-modal-title">模型定义</h3>
        <button class="modal-close" onclick="closeModelsEditor()" aria-label="关闭">×</button>
      </div>
      <div class="modal-body" id="models-modal-body"></div>
      <div class="modal-foot">
        <span class="modal-msg" id="models-modal-msg"></span>
        <button class="modal-btn" onclick="closeModelsEditor()">取消</button>
        <button class="modal-btn modal-btn-primary" onclick="saveModelsEditor(this)">保存</button>
      </div>
    </div>
  </div>

  <!-- 聚合网关配置编辑 modal -->
  <div class="modal-overlay" id="agg-modal" role="dialog" aria-modal="true" aria-label="编辑聚合网关配置">
    <div class="modal modal-wide">
      <div class="modal-head">
        <h3 id="agg-modal-title">聚合网关配置</h3>
        <button class="modal-close" onclick="closeAggConfigEditor()" aria-label="关闭">×</button>
      </div>
      <div class="modal-body" id="agg-modal-body"></div>
      <div class="modal-foot">
        <span class="modal-msg" id="agg-modal-msg"></span>
        <button class="modal-btn" onclick="closeAggConfigEditor()">取消</button>
        <button class="modal-btn modal-btn-primary" onclick="saveAggConfig(this)">保存</button>
      </div>
    </div>
  </div>


<script>
// ═══ 三个编辑 modal 共享基础设施（mm* 前缀，docs §2.2）═══
// 统一消息提示：kind ∈ ok | warn | err | info
function mmMsg(el, kind, text) {{
  if (!el) return;
  var K = {{ok: "success", warn: "danger", err: "danger", info: ""}};
  el.textContent = text || "";
  el.className = "modal-msg " + (K[kind] !== undefined ? K[kind] : "");
}}

// 将后端 validate_targets 返回的 path 回显到对应字段；整体消息始终保留作兜底。
function mmShowErrors(msgEl, errors) {{
  document.querySelectorAll('.field-error').forEach(function(el) {{ el.classList.remove('field-error'); }});
  var items = Array.isArray(errors) ? errors : [errors];
  var messages = [];
  items.forEach(function(item) {{
    var path = item && typeof item === 'object' ? item.path : '';
    var text = item && typeof item === 'object' ? item.msg : String(item || '保存失败');
    if (text) messages.push(path ? path + ': ' + text : text);
    if (!path) return;
    document.querySelectorAll('[data-path]').forEach(function(field) {{
      if (field.dataset.path === path) field.classList.add('field-error');
    }});
  }});
  mmMsg(msgEl, 'err', messages.join('；') || '保存失败');
}}

// 统一行插入：永远插在 section 末尾的添加按钮行之前。
// anchorSel 必须是该 section 专属类名，避免嵌套同类按钮撞名（Bug 1 根因）。
function mmInsertRow(section, rowHtml, anchorSel) {{
  if (!section || !rowHtml) return null;
  var anchor = null;
  if (anchorSel) {{
    var cands = section.querySelectorAll(anchorSel);
    for (var i = 0; i < cands.length; i++) {{
      if (mmOwnsNode(section, cands[i], anchorSel)) {{ anchor = cands[i]; break; }}
    }}
  }}
  if (anchor) {{ anchor.insertAdjacentHTML("beforebegin", rowHtml); return anchor.previousElementSibling; }}
  section.insertAdjacentHTML("beforeend", rowHtml);
  return section.lastElementChild;
}}

// node 是否"属于"section 本层：node 与 section 之间不得夹着另一个同类锚点容器。
// 用于 agg-modal 这类嵌套结构（虚拟模型块内还有成员添加行）。
// 注意：嵌套容器类名清单（.agg-vm/.agg-pool）若新增须同步登记，否则会误判归属（Bug 1 复发）。
function mmOwnsNode(section, node, anchorSel) {{
  var p = node.parentNode;
  while (p && p !== section) {{
    if (p.matches && p.matches(".agg-vm, .agg-pool")) return false;
    p = p.parentNode;
  }}
  if (p !== section && anchorSel) {{
    // 找不到本层归属：降级到末尾插入会悄悄插错位置，告警以便排查（不抛错，保持主流程可用）
    console.warn('[mmOwnsNode] 锚点未命中本层归属，已降级插入末尾:', anchorSel);
  }}
  return p === section;
}}

// 作用域提示条：明示本 modal 改什么、不改什么（§2.4.1）。
function mmScope(doesText, notText) {{
  var h = '<div class="mm-scope"><span class="mm-scope-icon">i</span><span><b>';
  h += escHtml(doesText) + '</b>';
  if (notText) h += ' <span class="mm-scope-neg">' + escHtml(notText) + '</span>';
  h += '</span></div>';
  return h;
}}

// ── 悬空引用警示条（§2.4.4）：只读诊断，改名后引用断了要看得见 ──
async function loadDanglingBar() {{
  var bar = document.getElementById('dangling-bar');
  if (!bar) return;
  try {{
    var resp = await fetch('/api/config/dangling');
    if (!resp.ok) return;
    var r = await resp.json();
    var items = (r && r.items) || [];
    if (!items.length) {{ bar.classList.remove('show'); bar.innerHTML = ''; return; }}
    var h = '<div class="dg-head"><span>配置存在悬空引用</span>';
    h += '<span class="dg-count">' + items.length + ' 处</span></div><ul>';
    items.forEach(function(it) {{
      h += '<li><code>' + escHtml(it.path || '') + '</code>' + escHtml(it.msg || '') + '</li>';
    }});
    h += '</ul>';
    bar.innerHTML = h;
    bar.classList.add('show');
  }} catch (e) {{ /* 诊断性功能，失败静默不打扰主流程 */ }}
}}

// ── 保存后局部刷新（§2.4.3）：重拉 dashboard HTML，只替换目标卡片 DOM ──
// 不整页刷新：保留手风琴展开状态与滚动位置，用户能立刻看到"我改的生效了"。
async function refreshCardDom(port, msgEl) {{
  try {{
    var resp = await fetch(location.pathname, {{headers: {{'Cache-Control': 'no-cache'}}}});
    if (!resp.ok) throw new Error('http_' + resp.status);
    var html = await resp.text();
    var doc = new DOMParser().parseFromString(html, 'text/html');
    var sel = '.card[data-port="' + port + '"]';
    var fresh = doc.querySelector(sel);
    var cur = document.querySelector(sel);
    if (!fresh || !cur) throw new Error('card_not_found');
    // 保留当前展开态：新 DOM 是服务端默认（收起）状态
    var wasOpen = !!cur.querySelector('.card-detail.open');
    cur.replaceWith(fresh);
    if (wasOpen) {{
      var d = fresh.querySelector('.card-detail');
      var a = fresh.querySelector('.ct-arrow');
      var t = fresh.querySelector('.card-toggle');
      if (d) d.classList.add('open');
      if (a) a.classList.add('open');
      if (t) t.setAttribute('aria-expanded', 'true');
    }}
    bindCardAccordion(fresh);
    return true;
  }} catch (e) {{
    // 刷新失败：不能让用户以为"卡片已更新"——明确提示手动刷新（§2.4.3 防误导）
    if (msgEl) mmMsg(msgEl, 'warn', '⚠️ 卡片局部刷新失败，请手动刷新页面查看最新状态');
    return false;
  }}
}}

// ── 手风琴交互（互斥，任一时刻只展开一个）──
// 具名函数而非 IIFE：局部刷新替换卡片 DOM 后要能重新绑定。
function bindCardAccordion(scope) {{
  var list = scope ? [scope] : Array.prototype.slice.call(document.querySelectorAll('.card'));
  list.forEach(function(card) {{
    var toggle = card.querySelector('.card-toggle');
    var detail = card.querySelector('.card-detail');
    if (!toggle || !detail || toggle._accBound) return;
    toggle._accBound = true;
    toggle.addEventListener('click', function() {{
      var isOpen = detail.classList.contains('open');
      document.querySelectorAll('.card').forEach(function(c) {{
        var d = c.querySelector('.card-detail');
        var a = c.querySelector('.ct-arrow');
        var t = c.querySelector('.card-toggle');
        if (d) d.classList.remove('open');
        if (a) a.classList.remove('open');
        if (t) t.setAttribute('aria-expanded', 'false');
      }});
      if (!isOpen) {{
        detail.classList.add('open');
        var arrow = toggle.querySelector('.ct-arrow');
        if (arrow) arrow.classList.add('open');
        toggle.setAttribute('aria-expanded', 'true');
      }}
    }});
  }});
}}
bindCardAccordion();

// ── 模型编辑 modal：打开（fetch 编辑态 HTML 填入 modal）──
async function openModelEditor(btn) {{
  var label = btn.dataset.label;
  var overlay = document.getElementById('model-modal');
  var body = document.getElementById('model-modal-body');
  var title = document.getElementById('model-modal-title');
  var msg = document.getElementById('model-modal-msg');
  if (!overlay || !body) return;
  title.textContent = '编辑模型 — ' + label;
  msg.textContent = '';
  body.innerHTML = '<div class="no-models">加载中...</div>';
  overlay.classList.add('open');
  try {{
    var resp = await fetch('/api/targets/' + encodeURIComponent(label) + '/models?edit=1');
    var html = await resp.text();
    if (resp.ok) {{
      body.innerHTML = html;
      bindModelEvents();
    }} else {{
      body.innerHTML = '<div class="no-models">加载失败: ' + html + '</div>';
    }}
  }} catch (e) {{
    body.innerHTML = '<div class="no-models">加载异常: ' + e + '</div>';
  }}
}}

function closeModelEditor() {{
  var overlay = document.getElementById('model-modal');
  if (overlay) overlay.classList.remove('open');
}}

// ── 清理过期模型：对照上游最新列表，删除已下线模型（配置 + 内存）──
async function pruneModels(btn) {{
  var label = btn.dataset.label;
  var msgEl = document.querySelector('.model-msg[data-label="' + label + '"]');
  btn.disabled = true;
  btn.textContent = '清理中...';
  var show = function(t, cls) {{
    if (msgEl) {{ msgEl.textContent = t; msgEl.className = 'model-msg ' + (cls || ''); }}
  }};
  try {{
    var resp = await fetch('/api/targets/' + encodeURIComponent(label) + '/prune-models', {{method: 'POST'}});
    var r = await resp.json();
    if (resp.ok) {{
      if (r.removed && r.removed.length) {{
        show('✅ 已删除 ' + r.removed.length + ' 个过期模型: ' + r.removed.join(', '), 'ok');
        setTimeout(function() {{ location.reload(); }}, 1200);
      }} else {{
        show('✅ 无过期模型（全部与上游一致，共 ' + r.keptCount + ' 个）', 'ok');
        btn.disabled = false;
        btn.textContent = '🧹 清理过期模型';
      }}
    }} else {{
      show('❌ ' + (r.detail || JSON.stringify(r)), 'err');
      btn.disabled = false;
      btn.textContent = '🧹 清理过期模型';
    }}
  }} catch (e) {{
    show('❌ 请求异常: ' + e, 'err');
    btn.disabled = false;
    btn.textContent = '🧹 清理过期模型';
  }}
}}

// 点击遮罩关闭
(function() {{
  var overlay = document.getElementById('model-modal');
  if (overlay) {{
    overlay.addEventListener('click', function(e) {{
      if (e.target === overlay) overlay.classList.remove('open');
    }});
  }}
}})();

// ── 保存模型展示设置（读取 modal 内所有开关 → PUT models）──
async function saveModelEditor(btn) {{
  var overlay = document.getElementById('model-modal');
  var body = document.getElementById('model-modal-body');
  var msg = document.getElementById('model-modal-msg');
  if (!overlay || !body) return;
  var label = document.getElementById('model-modal-title').textContent.replace('编辑模型 — ', '');
  var rows = body.querySelectorAll('.mrow');
  var models = [];
  rows.forEach(function(row) {{
    var idEl = row.querySelector('.mrow-id');
    var sw = row.querySelector('.model-show');
    if (!idEl || !sw) return;  // 跳过总开关行（无子开关 .model-show）
    var mid = idEl.textContent;
    models.push({{id: mid, enabled: sw.checked}});
  }});
  btn.disabled = true; btn.textContent = '保存中...';
  try {{
    var resp = await fetch('/api/targets/' + encodeURIComponent(label), {{
      method: 'PUT', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{models: models}}),
    }});
    var r = await resp.json();
    if (resp.ok) {{
      var _onN = models.filter(function(m) {{ return m.enabled; }}).length;
      mmMsg(msg, 'ok', '✅ 已保存 ' + models.length + ' 个模型（开启 ' + _onN + ' 个）→ ' + label + ' 卡片已更新');
      var _port = (document.querySelector('.card[data-label="' + label + '"]') || {{}}).dataset;
      if (_port && _port.port) await refreshCardDom(_port.port, msg);
      setTimeout(function() {{
        closeModelEditor();
        btn.disabled = false; btn.textContent = '保存';
      }}, 1200);
    }} else {{
      mmShowErrors(msg, r.detail || r);
      btn.disabled = false; btn.textContent = '保存';
    }}
  }} catch (e) {{
    mmMsg(msg, 'err', '❌ 保存异常: ' + e);
    btn.disabled = false; btn.textContent = '保存';
  }}
}}

// ── 总开关：全开/全关/部分开（indeterminate），联动所有子开关 ──
function syncMasterState() {{
  var body = document.getElementById('model-modal-body');
  if (!body) return;
  var master = body.querySelector('.model-master');
  if (!master) return;
  var subs = Array.prototype.slice.call(body.querySelectorAll('.mrow .model-show'));
  if (subs.length === 0) return;
  var on = subs.filter(function(s) {{ return s.checked; }}).length;
  if (on === 0) {{
    master.checked = false; master.indeterminate = false;
  }} else if (on === subs.length) {{
    master.checked = true; master.indeterminate = false;
  }} else {{
    master.checked = false; master.indeterminate = true;
  }}
}}

// ── 绑定模型编辑事件（modal 内开关绑定 + 总开关联动）──
function bindModelEvents() {{
  document.querySelectorAll('.model-show').forEach(function(sw) {{
    if (sw._bound) return;
    sw._bound = true;
    sw.addEventListener('change', syncMasterState);
  }});
  var master = document.querySelector('#model-modal-body .model-master');
  if (master && !master._bound) {{
    master._bound = true;
    master.addEventListener('change', function() {{
      var checked = master.checked;
      document.querySelectorAll('#model-modal-body .mrow .model-show').forEach(function(sw) {{
        sw.checked = checked;
      }});
      syncMasterState();
    }});
  }}
  syncMasterState();
}}

// ── modal 内模型搜索过滤（隐藏不匹配行）──
function filterModels(input) {{
  var q = (input.value || '').toLowerCase().trim();
  var body = document.getElementById('model-modal-body');
  if (!body) return;
  var visible = 0;
  body.querySelectorAll('.mrow').forEach(function(row) {{
    var text = (row.textContent || '').toLowerCase();
    var match = !q || text.indexOf(q) >= 0;
    row.style.display = match ? '' : 'none';
    if (match) visible++;
  }});
  // 无匹配时提示
  var empty = body.querySelector('.no-models');
  if (q && visible === 0) {{
    if (!empty) {{
      empty = document.createElement('div');
      empty.className = 'no-models';
      body.appendChild(empty);
    }}
    empty.textContent = '无匹配模型: ' + input.value;
  }} else if (empty) {{
    empty.remove();
  }}
}}

// ── modal 内模型行：删除（×）──
function removeModelRow(btn) {{
  var row = btn.closest('.mrow');
  if (row) row.remove();
  syncMasterState();
}}

// ── modal 内模型行：底部添加（自由输入新模型名）──
function addModelRow() {{
  var body = document.getElementById('model-modal-body');
  if (!body) return;
  var input = document.getElementById('model-add-input');
  var mid = (input && input.value || '').trim();
  if (!mid) {{
    var msgEl = body.querySelector('.model-msg');
    if (msgEl) {{ msgEl.textContent = '⚠️ 请输入模型名'; msgEl.className = 'model-msg err'; }}
    return;
  }}
  var dup = false;
  body.querySelectorAll('.mrow .mrow-id').forEach(function(idEl) {{
    if (idEl.textContent === mid) dup = true;
  }});
  if (dup) {{
    var msgEl = body.querySelector('.model-msg');
    if (msgEl) {{ msgEl.textContent = '⚠️ 模型已存在: ' + mid; msgEl.className = 'model-msg err'; }}
    return;
  }}
  var html = '<div class="mrow" data-model="' + escHtml(mid) + '">' +
    '<div class="mrow-info">' +
    '  <div class="mrow-id">' + escHtml(mid) + '</div>' +
    '  <div class="mrow-name">' + escHtml(mid) + '</div>' +
    '</div>' +
    '<label class="switch" title="展示此模型">' +
    '  <input type="checkbox" class="model-show" data-model="' + escHtml(mid) + '" checked>' +
    '  <span class="switch-slider"></span>' +
    '</label>' +
    '<button class="mrow-del" onclick="removeModelRow(this)" title="删除此模型">×</button>' +
    '</div>';
  // 列表容器优先：.mrow-add 在 .model-editor-add 内，与行列表不同层
  var list = body.querySelector('.model-editor-list') || body;
  mmInsertRow(list, html, '.mrow-add');
  var nm = body.querySelector('.no-models');
  if (nm) nm.remove();
  if (input) input.value = '';
  bindModelEvents();
}}

// ── 模型定义编辑 modal（全局 models[]：name/aliases/target port+model）──
async function openModelsEditor(btn) {{
  var overlay = document.getElementById('models-modal');
  var body = document.getElementById('models-modal-body');
  var title = document.getElementById('models-modal-title');
  var msg = document.getElementById('models-modal-msg');
  if (!overlay || !body) return;
  title.textContent = '模型定义 — 8081 转发/别名配置';
  msg.textContent = '';
  body.innerHTML = '<div class="no-models">加载中...</div>';
  overlay.classList.add('open');
  try {{
    var results = await Promise.all([fetch('/api/models'), fetch('/api/aggregate/config')]);
    var resp = results[0];
    var portsResp = results[1];
    var r = await resp.json();
    var ports = await portsResp.json();
    if (!resp.ok) {{
      body.innerHTML = '<div class="no-models">加载失败: ' + (r.detail || JSON.stringify(r)) + '</div>';
      return;
    }}
    _aggAvailablePorts = ports.availablePorts || {{}};
    body.innerHTML = buildModelsEditorHtml(r);
  }} catch (e) {{
    body.innerHTML = '<div class="no-models">加载异常: ' + e + '</div>';
  }}
}}

function buildModelsEditorHtml(r) {{
  var models = r.models || [];
  var html = mmScope('本页定义 8081 的模型别名 → 下游端口+真实模型，保存后立即出现在 8081 卡片。',
    '不影响各 target 端口自身的透传白名单，也不改变 8080 聚合路由。');
  html += '<div class="mm-hint">模型定义：name 为主模型名（请求可直接用它），aliases 为额外别名（逗号分隔），target 指定最终下游端口与真实模型（可填聚合虚拟模型 agg:xxx）。未匹配任何定义的模型名将走 modelDefaults.defaultPort 原样透传。</div>';
  html += '<div class="agg-section"><div class="agg-section-title">默认转发端口</div><div class="agg-fields">' +
    '<label class="agg-field">' +
    '  <span class="agg-label">modelDefaults.defaultPort（未命中定义的兜底端口）</span>' +
    '  <input type="number" class="agg-input md-default-port" data-path="modelDefaults.defaultPort" value="' + escHtml(String((r.modelDefaults || {{}}).defaultPort)) + '" aria-label="默认转发端口">' +
    '</label>' +
    '</div></div>';
  html += '<div class="agg-section"><div class="agg-section-title">模型定义列表</div>';
  if (models.length === 0) {{
    html += modelsRowHtml('', '', '', '', 0);
  }} else {{
    models.forEach(function(m, i) {{
      var aliases = (m.aliases || []).join(', ');
      var t = m.target || {{}};
      html += modelsRowHtml(m.name, aliases, t.port, t.model, i);
    }});
  }}
  html += '<div class="agg-add-row"><button class="mm-add-btn" onclick="addModelsRow()">+ 添加模型</button></div></div>';
  return html;
}}

function modelsRowHtml(name, aliases, port, model, index) {{
  var n = (name === undefined || name === null) ? '' : escHtml(String(name));
  var a = (aliases === undefined || aliases === null) ? '' : escHtml(String(aliases));
  var p = (port === undefined || port === null) ? '' : escHtml(String(port));
  var m = (model === undefined || model === null) ? '' : escHtml(String(model));
  return '<div class="mm-row">' +
    '<input type="text" class="agg-input md-name" data-path="models[' + index + '].name" value="' + n + '" placeholder="模型名（如 sonnet）" aria-label="模型名">' +
    '<input type="text" class="agg-input md-aliases" data-path="models[' + index + '].aliases" value="' + a + '" placeholder="别名，逗号分隔" aria-label="别名">' +
    aggPortSelectHtml(p, 'models[' + index + '].target.port') +
    aggModelSelectHtml(p, m, 'models[' + index + '].target.model') +
    '<button class="mm-del" onclick="removeModelsRow(this)" title="删除此行">×</button>' +
    '</div>';
}}

function addModelsRow() {{
  var body = document.getElementById('models-modal-body');
  if (!body) return;
  mmInsertRow(body, modelsRowHtml('', '', '', '', body.querySelectorAll('.mm-row').length), '.agg-add-row');
}}

function removeModelsRow(btn) {{
  var row = btn.closest('.mm-row');
  if (row) row.remove();
}}

function syncModelsPaths(body) {{
  body.querySelectorAll('.mm-row').forEach(function(row, index) {{
    var prefix = 'models[' + index + ']';
    var name = row.querySelector('.md-name');
    var aliases = row.querySelector('.md-aliases');
    var port = row.querySelector('.agg-mem-port');
    var model = row.querySelector('.agg-mem-model');
    if (name) name.dataset.path = prefix + '.name';
    if (aliases) aliases.dataset.path = prefix + '.aliases';
    if (port) port.dataset.path = prefix + '.target.port';
    if (model) model.dataset.path = prefix + '.target.model';
  }});
}}

function closeModelsEditor() {{
  var overlay = document.getElementById('models-modal');
  if (overlay) overlay.classList.remove('open');
}}

async function saveModelsEditor(btn) {{
  var body = document.getElementById('models-modal-body');
  var msg = document.getElementById('models-modal-msg');
  if (!body || !msg) return;
  syncModelsPaths(body);
  var defaultPortEl = body.querySelector('.md-default-port');
  var defaultPort = defaultPortEl ? defaultPortEl.value.trim() : '';
  if (defaultPort === '' || isNaN(Number(defaultPort)) || Number(defaultPort) < 0 || Number(defaultPort) % 1 !== 0) {{
    mmMsg(msg, 'err', '⚠️ defaultPort 必须为非负整数');
    return;
  }}
  var models = [];
  var bad = false;
  body.querySelectorAll('.mm-row').forEach(function(row) {{
    if (bad) return;
    var nEl = row.querySelector('.md-name');
    var aEl = row.querySelector('.md-aliases');
    var pEl = row.querySelector('.agg-mem-port');
    var mEl = row.querySelector('.agg-mem-model');
    var n = (nEl ? nEl.value : '').trim();
    var a = (aEl ? aEl.value : '').trim();
    var p = (pEl ? pEl.value : '').trim();
    var m = (mEl ? mEl.value : '').trim();
    if (!n && !a && !p && !m) return;
    if (!n) {{ mmMsg(msg, 'err', '⚠️ 模型名不能为空'); bad = true; return; }}
    if (p === '' || isNaN(Number(p)) || Number(p) < 0 || Number(p) % 1 !== 0) {{
      mmMsg(msg, 'err', '⚠️ 模型 ' + n + ' 的下游端口必须为非负整数'); bad = true; return;
    }}
    if (!m) {{ mmMsg(msg, 'err', '⚠️ 模型 ' + n + ' 的真实模型不能为空'); bad = true; return; }}
    var aliases = a ? a.split(',').map(function(x) {{ return x.trim(); }}).filter(function(x) {{ return x; }}) : [];
    models.push({{name: n, aliases: aliases, target: {{port: Number(p), model: m}}}});
  }});
  if (bad) return;
  var payload = {{models: models, modelDefaults: {{defaultPort: Number(defaultPort)}}}};
  var resp = await fetch('/api/models', {{
    method: 'PUT',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(payload)
  }});
  var r = await resp.json();
  if (!resp.ok) {{
    mmShowErrors(msg, r.detail || r);
    return;
  }}
  // 生效位置提示（docs §2.4.2）：显示实际保存条目数 + 改动出现在哪
  mmMsg(msg, 'ok', '✅ 已保存 ' + models.length + ' 个模型定义 → 已在 8081 卡片显示');
  // 局部刷新 8081 卡片（§2.4.3）：不整页刷新，用户立刻能对上数字
  await refreshCardDom(8081, msg);
  loadDanglingBar();
  setTimeout(function() {{ closeModelsEditor(); }}, 1200);
}}

// ── 聚合网关（8080）配置编辑 modal ──
function aggNumField(key, labelText, val, placeholder) {{
  var v = (val === undefined || val === null) ? '' : escHtml(String(val));
  return '<label class="agg-field">' +
    '<span class="agg-label">' + labelText + '</span>' +
    '<input type="number" class="agg-input agg-pd-num" data-key="' + key + '" data-path="poolDefaults.' + key + '" value="' + v + '" placeholder="' + (placeholder || '') + '" aria-label="' + labelText + '">' +
    '</label>';
}}

// 聚合可用端口缓存（由 buildAggConfigHtml 在打开编辑器时注入）
var _aggAvailablePorts = {{}};

function aggPortSelectHtml(selectedPort, path) {{
  var attr = path ? ' data-path="' + escHtml(path) + '"' : '';
  var html = '<select class="agg-input agg-mem-port"' + attr + ' aria-label="端口" onchange="onAggPortChange(this)">';
  html += '<option value=""' + (selectedPort ? '' : ' selected') + '>选择端口</option>';
  var keys = Object.keys(_aggAvailablePorts).sort(function(a, b) {{ return Number(a) - Number(b); }});
  keys.forEach(function(pk) {{
    var info = _aggAvailablePorts[pk];
    var label = pk + ' · ' + (info.label || info.handler || '');
    var sel = (String(selectedPort) === pk) ? ' selected' : '';
    html += '<option value="' + pk + '"' + sel + '>' + escHtml(label) + '</option>';
  }});
  if (selectedPort !== undefined && selectedPort !== null && selectedPort !== '' && !_aggAvailablePorts[String(selectedPort)]) {{
    html += '<option value="' + escHtml(String(selectedPort)) + '" selected>' + escHtml(String(selectedPort)) + ' (自定义)</option>';
  }}
  html += '</select>';
  return html;
}}

function aggModelSelectHtml(selectedPort, selectedModel, path) {{
  var attr = path ? ' data-path="' + escHtml(path) + '"' : '';
  var html = '<select class="agg-input agg-mem-model"' + attr + ' aria-label="模型" onchange="onAggModelChange(this)">';
  html += '<option value=""' + (selectedModel ? '' : ' selected') + '>选择模型</option>';
  var models = [];
  if (selectedPort !== undefined && selectedPort !== null && selectedPort !== '' && _aggAvailablePorts[String(selectedPort)]) {{
    models = _aggAvailablePorts[String(selectedPort)].models || [];
  }}
  // 仅显示所选端口的真实上游模型，不追加虚拟模型（agg:xxx）
  var all = models.slice();
  all.sort();
  all.forEach(function(m) {{
    var sel = (m === selectedModel) ? ' selected' : '';
    html += '<option value="' + escHtml(m) + '"' + sel + '>' + escHtml(m) + '</option>';
  }});
  if (selectedModel !== undefined && selectedModel !== null && selectedModel !== '' && all.indexOf(selectedModel) === -1) {{
    html += '<option value="' + escHtml(String(selectedModel)) + '" selected>' + escHtml(String(selectedModel)) + ' (自定义)</option>';
  }}
  html += '</select>';
  return html;
}}

function onAggPortChange(selEl) {{
  var row = selEl.closest('.agg-pool-row, .mm-row');
  if (!row) return;
  var modelSel = row.querySelector('.agg-mem-model');
  if (!modelSel) return;
  var port = selEl.value;
  // 重建模型下拉（不传 poolKey，统一只显示所选端口的真实模型）
  var newHtml = aggModelSelectHtml(port, '', modelSel.dataset.path);
  var tmp = document.createElement('div');
  tmp.innerHTML = newHtml;
  var newSel = tmp.firstChild;
  if (newSel) {{
    modelSel.parentNode.insertBefore(newSel, modelSel);
    modelSel.parentNode.removeChild(modelSel);
  }}
}}

function onAggModelChange(selEl) {{
  // 保留钩子：未来可扩展 agg:xxx 模型的特殊处理
}}

function aggPoolMemberRow(port, model, weight, poolKey, vmid, index) {{
  var w = (weight === undefined || weight === null) ? '' : escHtml(String(weight));
  var prefix = vmid ? 'virtualModels.' + vmid + '.' + (poolKey === 'default' ? 'defaultPool' : 'fallbackPool') + '[' + index + ']' : '';
  return '<div class="agg-pool-row">' +
    aggPortSelectHtml(port, prefix ? prefix + '.port' : '') +
    aggModelSelectHtml(port, model, prefix ? prefix + '.model' : '') +
    '<input type="number" class="agg-input agg-mem-weight"' + (prefix ? ' data-path="' + escHtml(prefix + '.weight') + '"' : '') + ' value="' + w + '" placeholder="权重" aria-label="权重">' +
    '<button class="mm-del" onclick="removeAggPoolMember(this)" title="删除成员">×</button>' +
    '</div>';
}}

function aggVmBlock(id, vm) {{
  var d = (vm && vm.defaultPool) || [];
  var f = (vm && vm.fallbackPool) || [];
  var dr = (vm && vm.defaultRetries !== undefined && vm.defaultRetries !== null) ? escHtml(String(vm.defaultRetries)) : '';
  var fr = (vm && vm.fallbackRetries !== undefined && vm.fallbackRetries !== null) ? escHtml(String(vm.fallbackRetries)) : '';
  var html = '<div class="agg-vm">' +
    '<div class="agg-vm-head">' +
    '  <span class="agg-label">虚拟模型 id</span>' +
    '  <input type="text" class="agg-input agg-vm-id" data-path="virtualModels.' + escHtml(String(id)) + '" value="' + escHtml(String(id)) + '" placeholder="如 agg:sonnet" aria-label="虚拟模型 id">' +
    '  <button class="mm-del" onclick="removeAggVm(this)" title="删除此虚拟模型">🗑</button>' +
    '</div>';
  html += '<div class="agg-pool" data-pool="default">' +
    '<div class="agg-pool-title">默认池 defaultPool</div>';
  if (d.length) {{ d.forEach(function(mem, i) {{ html += aggPoolMemberRow(mem.port, mem.model, mem.weight, 'default', id, i); }}); }}
  else {{ html += aggPoolMemberRow('', '', '', 'default', id, 0); }}
  html += '<div class="agg-add-row"><button class="mm-add-btn" onclick="addAggPoolMember(this, &quot;default&quot;)">+ 添加成员</button></div></div>';
  html += '<div class="agg-pool" data-pool="fallback">' +
    '<div class="agg-pool-title">降级池 fallbackPool</div>';
  if (f.length) {{ f.forEach(function(mem, i) {{ html += aggPoolMemberRow(mem.port, mem.model, mem.weight, 'fallback', id, i); }}); }}
  html += '<div class="agg-add-row"><button class="mm-add-btn" onclick="addAggPoolMember(this, &quot;fallback&quot;)">+ 添加降级成员</button></div></div>';
  html += '<div class="agg-vm-retries">' +
    '<label class="agg-field">' +
    '  <span class="agg-label">defaultRetries（空=继承池默认）</span>' +
    '  <input type="number" class="agg-input agg-vm-dr" data-path="virtualModels.' + escHtml(String(id)) + '.defaultRetries" value="' + dr + '" placeholder="继承" aria-label="defaultRetries">' +
    '</label>' +
    '<label class="agg-field">' +
    '  <span class="agg-label">fallbackRetries（空=继承池默认）</span>' +
    '  <input type="number" class="agg-input agg-vm-fr" data-path="virtualModels.' + escHtml(String(id)) + '.fallbackRetries" value="' + fr + '" placeholder="继承" aria-label="fallbackRetries">' +
    '</label>' +
    '</div>' +
    '</div>';
  return html;
}}

async function openAggConfigEditor(btn) {{
  var overlay = document.getElementById('agg-modal');
  var body = document.getElementById('agg-modal-body');
  var title = document.getElementById('agg-modal-title');
  var msg = document.getElementById('agg-modal-msg');
  if (!overlay || !body) return;
  title.textContent = '聚合网关配置 — 8080';
  msg.textContent = '';
  body.innerHTML = '<div class="no-models">加载中...</div>';
  overlay.classList.add('open');
  try {{
    var resp = await fetch('/api/aggregate/config');
    var r = await resp.json();
    if (!resp.ok) {{
      body.innerHTML = '<div class="no-models">加载失败: ' + (r.detail || JSON.stringify(r)) + '</div>';
      return;
    }}
    if (!r.configured) {{
      body.innerHTML = '<div class="no-models">聚合网关未配置（targets.json 中缺少 handler=aggregator 的 target）</div>';
      return;
    }}
    body.innerHTML = buildAggConfigHtml(r);
  }} catch (e) {{
    body.innerHTML = '<div class="no-models">加载异常: ' + e + '</div>';
  }}
}}

function buildAggConfigHtml(r) {{
  // 注入全局缓存，供 aggPortSelectHtml / aggModelSelectHtml 使用
  _aggAvailablePorts = r.availablePorts || {{}};
  var pd = r.poolDefaults || {{}};
  var html = mmScope('本页配置仅影响 8080 聚合网关的虚拟模型路由，保存后引擎热重载。',
    '不改变 8081 模型列表，也不改变各下游端口自身的模型白名单。');
  html += '<div class="agg-hint">虚拟模型池配置：成员端口指向本地真实网关端口，模型为上游模型名（可填 agg:xxx 链式聚合）。' +
    '权重与重试留空 = 继承池默认值；保存后热生效。</div>';
  html += '<div class="agg-section"><div class="agg-section-title">池默认值 poolDefaults</div><div class="agg-fields">' +
    aggNumField('defaultRetries', 'defaultRetries', pd.defaultRetries, '如 2') +
    aggNumField('fallbackRetries', 'fallbackRetries', pd.fallbackRetries, '如 1') +
    aggNumField('sessionAffinityTtlSeconds', 'sessionAffinityTtlSeconds', pd.sessionAffinityTtlSeconds, '如 3600') +
    aggNumField('probeIntervalSeconds', 'probeIntervalSeconds', pd.probeIntervalSeconds, '如 300') +
    aggNumField('weight', 'weight（成员默认权重）', pd.weight, '如 1') +
    '</div></div>';
  html += '<div class="agg-section" id="agg-vm-section"><div class="agg-section-title">虚拟模型 virtualModels</div>';
  var vms = r.virtualModels || {{}};
  var keys = Object.keys(vms);
  if (keys.length === 0) {{
    html += '<div class="no-models">(暂无虚拟模型，点击下方「+ 新增虚拟模型」添加)</div>';
  }} else {{
    keys.forEach(function(k) {{ html += aggVmBlock(k, vms[k]); }});
  }}
  html += '<button class="mm-add-btn agg-vm-add" onclick="addAggVm()">+ 新增虚拟模型</button></div>';
  return html;
}}

function addAggVm() {{
  var body = document.getElementById('agg-modal-body');
  var section = document.getElementById('agg-vm-section');
  if (!body || !section) return;
  var html = aggVmBlock('', {{defaultPool: [], fallbackPool: []}});
  // 锚点必须用专属类名 agg-vm-add：section 内每个虚拟模型块还含「+ 添加成员」
  // 等同类 .mm-add-btn 按钮，querySelector('.mm-add-btn') 会取到第一个（嵌套插错位置）。
  // mmInsertRow 额外做本层归属校验（mmOwnsNode），双重保险。
  mmInsertRow(section, html, '.agg-vm-add');
  var ids = body.querySelectorAll('.agg-vm-id');
  if (ids.length) ids[ids.length - 1].focus();
}}

function removeAggVm(btn) {{
  var block = btn.closest('.agg-vm');
  if (block) block.remove();
}}

function addAggPoolMember(btn, poolKey) {{
  var pool = btn.closest('.agg-pool');
  if (!pool) return;
  var vm = pool.closest('.agg-vm');
  var idEl = vm ? vm.querySelector('.agg-vm-id') : null;
  var vmid = idEl ? idEl.value.trim() : '';
  var resolvedPool = poolKey || (pool.dataset ? pool.dataset.pool : '');
  var html = aggPoolMemberRow('', '', '', resolvedPool, vmid, pool.querySelectorAll('.agg-pool-row').length);
  mmInsertRow(pool, html, '.agg-add-row');
}}

function removeAggPoolMember(btn) {{
  var row = btn.closest('.agg-pool-row');
  if (row) row.remove();
}}

function syncAggPaths(body) {{
  body.querySelectorAll('.agg-vm').forEach(function(vm) {{
    var idEl = vm.querySelector('.agg-vm-id');
    var vmid = idEl ? idEl.value.trim() : '';
    if (idEl) idEl.dataset.path = 'virtualModels.' + vmid;
    vm.querySelectorAll('.agg-pool').forEach(function(pool) {{
      var key = pool.dataset.pool === 'default' ? 'defaultPool' : 'fallbackPool';
      pool.querySelectorAll('.agg-pool-row').forEach(function(row, index) {{
        var prefix = 'virtualModels.' + vmid + '.' + key + '[' + index + ']';
        var port = row.querySelector('.agg-mem-port');
        var model = row.querySelector('.agg-mem-model');
        var weight = row.querySelector('.agg-mem-weight');
        if (port) port.dataset.path = prefix + '.port';
        if (model) model.dataset.path = prefix + '.model';
        if (weight) weight.dataset.path = prefix + '.weight';
      }});
    }});
    var dr = vm.querySelector('.agg-vm-dr');
    var fr = vm.querySelector('.agg-vm-fr');
    if (dr) dr.dataset.path = 'virtualModels.' + vmid + '.defaultRetries';
    if (fr) fr.dataset.path = 'virtualModels.' + vmid + '.fallbackRetries';
  }});
}}

async function saveAggConfig(btn) {{
  var body = document.getElementById('agg-modal-body');
  var msg = document.getElementById('agg-modal-msg');
  if (!body || !msg) return;
  syncAggPaths(body);
  var poolDefaults = {{}};
  var bad = false;
  body.querySelectorAll('.agg-pd-num').forEach(function(inp) {{
    if (bad) return;
    var key = inp.dataset.key;
    var v = inp.value.trim();
    if (v === '') return;
    var n = Number(v);
    if (isNaN(n) || n < 0) {{
      mmMsg(msg, 'err', '⚠️ poolDefaults.' + key + ' 必须为非负数字');
      bad = true; return;
    }}
    poolDefaults[key] = n;
  }});
  var virtualModels = {{}};
  if (!bad) {{
    body.querySelectorAll('.agg-vm').forEach(function(vm) {{
      if (bad) return;
      var idEl = vm.querySelector('.agg-vm-id');
      var vid = (idEl ? idEl.value : '').trim();
      if (!vid) return;  // 空 id 块忽略
      if (virtualModels[vid]) {{
        mmMsg(msg, 'err', '⚠️ 虚拟模型 id 重复: ' + vid);
        bad = true; return;
      }}
      var entry = {{}};
      ['default', 'fallback'].forEach(function(poolKey) {{
        if (bad) return;
        var list = [];
        var pool = vm.querySelector('.agg-pool[data-pool="' + poolKey + '"]');
        if (pool) {{
          pool.querySelectorAll('.agg-pool-row').forEach(function(row) {{
            if (bad) return;
            var portEl = row.querySelector('.agg-mem-port');
            var modelEl = row.querySelector('.agg-mem-model');
            var wEl = row.querySelector('.agg-mem-weight');
                    var port = (portEl ? portEl.value : '').trim();
            var model = (modelEl ? modelEl.value : '').trim();
            var w = (wEl ? wEl.value : '').trim();
            if (!port && !model && !w) return;  // 空行忽略
            if (port === '' || isNaN(Number(port)) || Number(port) < 0) {{
              mmMsg(msg, 'err', '⚠️ 虚拟模型 ' + vid + ' 的成员端口必须为非负整数');
              bad = true; return;
            }}
            if (!model) {{
              mmMsg(msg, 'err', '⚠️ 虚拟模型 ' + vid + ' 的成员模型名不能为空');
              bad = true; return;
            }}
            var mem = {{port: Number(port), model: model}};
            // 端口已在下拉列表外（自定义）：允许 agg:xxx 等链式聚合，模型也允许自由输入
            if (w !== '') {{
              var wn = Number(w);
              if (isNaN(wn) || wn < 0) {{
                mmMsg(msg, 'err', '⚠️ 虚拟模型 ' + vid + ' 的成员权重必须为非负数字');
                bad = true; return;
              }}
              mem.weight = wn;
            }}
            list.push(mem);
          }});
        }}
        entry[poolKey === 'default' ? 'defaultPool' : 'fallbackPool'] = list;
      }});
      if (bad) return;
      var drEl = vm.querySelector('.agg-vm-dr');
      var frEl = vm.querySelector('.agg-vm-fr');
      var dr = drEl ? drEl.value.trim() : '';
      var fr = frEl ? frEl.value.trim() : '';
      if (dr !== '') {{
        var drn = Number(dr);
        if (isNaN(drn) || drn < 0 || drn % 1 !== 0) {{
          mmMsg(msg, 'err', '⚠️ 虚拟模型 ' + vid + ' 的 defaultRetries 必须为非负整数');
          bad = true; return;
        }}
        entry.defaultRetries = drn;
      }}
      if (fr !== '') {{
        var frn = Number(fr);
        if (isNaN(frn) || frn < 0 || frn % 1 !== 0) {{
          mmMsg(msg, 'err', '⚠️ 虚拟模型 ' + vid + ' 的 fallbackRetries 必须为非负整数');
          bad = true; return;
        }}
        entry.fallbackRetries = frn;
      }}
      virtualModels[vid] = entry;
    }});
  }}
  if (bad) return;
  if (Object.keys(virtualModels).length === 0) {{
    mmMsg(msg, 'err', '⚠️ 至少需要一个虚拟模型');
    return;
  }}
  var payload = {{virtualModels: virtualModels}};
  if (Object.keys(poolDefaults).length) payload.poolDefaults = poolDefaults;
  btn.disabled = true; btn.textContent = '保存中...';
  try {{
    var resp = await fetch('/api/aggregate/config', {{
      method: 'PUT', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(payload),
    }});
    var r = await resp.json();
    if (resp.ok) {{
      var _vmN = Object.keys(virtualModels).length;
      mmMsg(msg, 'ok', '✅ 已保存 ' + _vmN + ' 个虚拟模型 → 聚合路由已热重载（8080 卡片已更新）');
      btn.textContent = '✅ 已保存'; btn.style.background = '#4ade80';
      await refreshCardDom(8080, msg);
      loadDanglingBar();
      setTimeout(function() {{
        closeAggConfigEditor();
        btn.disabled = false; btn.textContent = '保存'; btn.style.background = '';
      }}, 1200);
    }} else {{
      mmShowErrors(msg, r.detail || r);
      btn.disabled = false; btn.textContent = '保存'; btn.style.background = '';
    }}
  }} catch (e) {{
    mmMsg(msg, 'err', '❌ 保存异常: ' + e);
    btn.disabled = false; btn.textContent = '保存'; btn.style.background = '';
  }}
}}

function closeAggConfigEditor() {{
  var overlay = document.getElementById('agg-modal');
  if (overlay) overlay.classList.remove('open');
}}

// 点击遮罩关闭聚合配置弹框
(function() {{
  var overlay = document.getElementById('agg-modal');
  if (overlay) {{
    overlay.addEventListener('click', function(e) {{
      if (e.target === overlay) overlay.classList.remove('open');
    }});
  }}
}})();


// ── 破解 token 重试 ──
async function recrackCard(label, btn) {{
  btn.disabled = true; btn.textContent = '破解中...';
  try {{
    var resp = await fetch('/api/targets/' + label + '/recrack', {{method: 'POST'}});
    var r = await resp.json();
    if (resp.ok) {{
      btn.textContent = '✅ 已破解'; btn.style.background = '#4ade80';
      setTimeout(function() {{ location.reload(); }}, 1200);
    }} else {{
      btn.textContent = '❌ 失败'; btn.style.background = '#ef4444';
      setOvMsg('❌ ' + (r.message || JSON.stringify(r)), 'danger');
      setTimeout(function() {{ btn.disabled = false; btn.textContent = '重新破解'; btn.style.background = ''; }}, 2000);
    }}
  }} catch (e) {{
    btn.textContent = '❌ 失败'; btn.style.background = '#ef4444';
    setOvMsg('❌ 破解异常: ' + e, 'danger');
    setTimeout(function() {{ btn.disabled = false; btn.textContent = '重新破解'; btn.style.background = ''; }}, 2000);
  }}
}}

async function saveCardToken(label, btn) {{
  var row = btn.closest('.token-edit');
  var input = row.querySelector('.te-input');
  // 无 secretRef 的直连网关：后端按约定落到 secrets.json 的 "<label>_token"，无需前端拦截
  var ref = input.dataset.ref || (label + '_token');
  var val = input.value;
  if (!val || val === '******') {{
    showTeStatus(row, '⚠️ 请输入新的 token 值', 'warning');
    return;
  }}
  btn.disabled = true; btn.textContent = '保存中...';
  try {{
    var resp = await fetch('/api/secrets/' + label, {{
      method: 'PUT', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{value: val}}),
    }});
    var r = await resp.json();
    if (resp.ok) {{
      btn.textContent = '✅ 已保存'; btn.style.background = '#4ade80';
      input.value = '******'; input.placeholder = '已配置，输入新值覆盖';
      var savedRef = (r && r.secretRef) ? r.secretRef : ref;
      showTeStatus(row, '✅ 已保存到 secrets.json (' + savedRef + ')，热生效；未带 key 的客户端将用它兜底', 'success');
      setTimeout(function() {{ btn.disabled = false; btn.textContent = '保存'; btn.style.background = ''; }}, 2000);
    }} else {{
      btn.disabled = false; btn.textContent = '保存';
      showTeStatus(row, '❌ 保存失败: ' + JSON.stringify(r.detail || r), 'danger');
    }}
  }} catch (e) {{
    btn.disabled = false; btn.textContent = '保存';
    showTeStatus(row, '❌ 保存异常: ' + e, 'danger');
  }}
}}

function showTeStatus(row, msg, level) {{
  var status = row.querySelector('.te-status');
  if (status) {{
    var colors = {{'success': '#4ade80', 'warning': '#fbbf24', 'danger': '#f87171'}};
    status.textContent = msg;
    status.style.color = colors[level] || '#9ca3af';
    if (level === 'success') {{
      setTimeout(function() {{ status.style.color = '#9ca3af'; }}, 3000);
    }}
  }}
}}

// ── 模型编辑 modal（openModelEditor/saveModelEditor 在上方定义）──

function setOvMsg(msg, level) {{
  var el = document.getElementById('ov-msg');
  if (!el) return;
  el.textContent = msg;
  el.className = 'ov-msg ' + (level || '');
  if (level !== 'danger') {{
    setTimeout(function() {{ el.textContent = ''; }}, 4000);
  }}
}}

async function doReload() {{
  var btn = event.target;
  if (btn) {{ btn.disabled = true; btn.textContent = '重载中...'; }}
  try {{
    var resp = await fetch('/api/reload', {{method: 'POST'}});
    var r = await resp.json();
    if (resp.ok) {{
      setOvMsg('✅ 配置已重载（' + (r.changes ? JSON.stringify(r.changes) : 'ok') + '）', 'success');
      setTimeout(function() {{ location.reload(); }}, 800);
    }} else {{
      setOvMsg('❌ 重载失败: ' + JSON.stringify(r), 'danger');
      if (btn) {{ btn.disabled = false; btn.textContent = '♻️ 重载配置'; }}
    }}
  }} catch (e) {{
    setOvMsg('❌ 重载异常: ' + e, 'danger');
    if (btn) {{ btn.disabled = false; btn.textContent = '♻️ 重载配置'; }}
  }}
}}

// ── 全量配置导出 / 导入 ──
function exportConfig() {{
  var btn = event.target;
  if (btn) {{ btn.disabled = true; btn.textContent = '导出中...'; }}
  fetch('/api/config/export', {{method: 'GET'}})
    .then(function(resp) {{ return resp.ok ? resp.json() : Promise.reject(resp); }})
    .then(function(data) {{
      var blob = new Blob([JSON.stringify(data, null, 2)], {{type: 'application/json'}});
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url;
      a.download = 'proxy-config-' + new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-') + '.json';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      setOvMsg('✅ 已导出完整配置（含全部私密凭据，请妥善保管）', 'success');
    }})
    .catch(function(err) {{
      setOvMsg('❌ 导出失败: ' + err.status + ' ' + err.statusText, 'danger');
    }})
    .finally(function() {{
      if (btn) {{ btn.disabled = false; btn.textContent = '📦 导出配置'; }}
    }});
}}

function importConfigFile(input) {{
  var file = input.files && input.files[0];
  if (!file) return;
  var reader = new FileReader();
  reader.onload = function(e) {{
    importConfigFromText(e.target.result);
    input.value = '';
  }};
  reader.onerror = function() {{
    setOvMsg('❌ 读取文件失败', 'danger');
    input.value = '';
  }};
  reader.readAsText(file);
}}

function importConfigFromText(text) {{
  var data;
  try {{
    data = JSON.parse(text);
  }} catch (e) {{
    setOvMsg('❌ 配置文件不是合法 JSON: ' + e.message, 'danger');
    return;
  }}
  var label = (data && data.version !== undefined) ? ('v' + data.version + ' 配置') : '未知格式';
  if (!confirm('⚠️ 导入 ' + label + ' 将覆盖当前 targets.json（含 server 配置）/ secrets.json（含全部私密凭据）。\\n\\n此操作不可撤销，确定继续？')) return;
  var btn = document.querySelector('.ov-actions .ov-btn-primary') || null;
  fetch('/api/config/import', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(data),
  }})
    .then(function(resp) {{
      return resp.json().then(function(r) {{ return {{ok: resp.ok, status: resp.status, body: r}}; }});
    }})
    .then(function(res) {{
      if (res.ok) {{
        setOvMsg('✅ 已导入 targets=' + res.body.targetsCount + ' secrets=' + res.body.secretsCount + '（' + res.body.message + '）', 'success');
        setTimeout(function() {{ location.reload(); }}, 1500);
      }} else {{
        var detail = typeof res.body.detail === 'string' ? res.body.detail : JSON.stringify(res.body.detail);
        setOvMsg('❌ 导入失败 (' + res.status + '): ' + detail, 'danger');
      }}
    }})
    .catch(function(err) {{
      setOvMsg('❌ 导入异常: ' + err, 'danger');
    }});
}}

// ── 初始化 ──
bindModelEvents();
loadDanglingBar();

// ── 破解网关：凭据管理弹窗（schema 驱动，表单/JSON 双模式）──
var credModal = null;
var credSchema = null;
var credLabel = '';

async function openCredentialModal(label, btn) {{
  credLabel = label;
  if (!credModal) {{
    var div = document.createElement('div');
    div.className = 'modal-overlay cred-modal';
    div.innerHTML =
      '<div class="modal cred-box">' +
      '  <div class="modal-head cred-head">' +
      '    <h3 id="cred-title">凭据管理</h3>' +
      '    <button class="modal-close cred-close" onclick="closeCredModal()" aria-label="关闭">×</button>' +
      '  </div>' +
      '  <div class="modal-body cred-body">' +
      '    <div class="cred-tabs">' +
      '      <button class="cred-tab active" data-mode="form" onclick="switchCredTab(&quot;form&quot;)">表单</button>' +
      '      <button class="cred-tab" data-mode="json" onclick="switchCredTab(&quot;json&quot;)">JSON</button>' +
      '    </div>' +
      '    <div id="cred-form" class="cred-pane active"></div>' +
      '    <div id="cred-json" class="cred-pane" style="display:none">' +
      '      <p class="cred-hint" id="cred-json-hint"></p>' +
      '      <textarea id="cred-json-input" placeholder="粘贴 JSON 凭据..."></textarea>' +
      '    </div>' +
      '  </div>' +
      '  <div class="modal-foot cred-foot">' +
      '    <span class="modal-msg cred-msg" id="cred-msg"></span>' +
      '    <button class="modal-btn cred-cancel" onclick="closeCredModal()">取消</button>' +
      '    <button class="modal-btn modal-btn-primary cred-save" onclick="submitCredential()">保存</button>' +
      '  </div>' +
      '</div>';
    div.addEventListener('click', function(e) {{ if (e.target === div) closeCredModal(); }});
    document.body.appendChild(div);
    credModal = div;
  }}
  document.getElementById('cred-msg').textContent = '';
  document.getElementById('cred-json-input').value = '';
  showCredMsg('加载中...', '');
  try {{
    var resp = await fetch('/api/crack/' + label + '/schema');
    if (!resp.ok) {{ showCredMsg('获取 schema 失败: HTTP ' + resp.status, 'err'); return; }}
    credSchema = await resp.json();
    document.getElementById('cred-title').textContent = '凭据 · ' + (credSchema.displayName || label);
    // 渲染表单
    var formHtml = '';
    credSchema.fields.forEach(function(f) {{
      var masked = (f.type === 'password') ? '留空则不修改' : '可选';
      formHtml += '<div class="cred-field">' +
        '<label>' + f.label + (f.required ? ' <span class="cred-req">*</span>' : '') + '</label>' +
        '<input type="' + f.type + '" data-key="' + f.key + '"' +
        '       placeholder="' + (f.placeholder || masked) + '">' +
        '<span class="cred-hint">' + (f.hint || '') + '</span>' +
        '<span class="cred-field-err"></span>' +
        '</div>';
    }});
    if (credSchema.readonlyFields && credSchema.readonlyFields.length) {{
      formHtml += '<div class="cred-readonly">只读字段（查询结果，不需手动填写）: ' +
        credSchema.readonlyFields.join(', ') + '</div>';
    }}
    document.getElementById('cred-form').innerHTML = formHtml;
    // JSON 模式提示
    var jsonHint = '粘贴 JSON，支持字段: ' + credSchema.fields.map(function(f){{return f.key}}).join(' / ');
    var rawKeys = Object.keys(credSchema.jsonImportMapping || {{}});
    if (rawKeys.length) jsonHint += '（或原始命名: ' + rawKeys.join(' / ') + '）';
    document.getElementById('cred-json-hint').textContent = jsonHint;
    showCredMsg('', '');
    credModal.classList.add('open');
    switchCredTab('form');
  }} catch (e) {{
    showCredMsg('加载异常: ' + e, 'err');
  }}
}}

function switchCredTab(mode) {{
  document.querySelectorAll('.cred-tab').forEach(function(b) {{
    b.classList.toggle('active', b.dataset.mode === mode);
  }});
  document.getElementById('cred-form').style.display = (mode === 'form') ? '' : 'none';
  document.getElementById('cred-json').style.display = (mode === 'json') ? '' : 'none';
}}

function closeCredModal() {{
  if (credModal) credModal.classList.remove('open');
}}

function showCredMsg(text, kind) {{
  var el = document.getElementById('cred-msg');
  if (!el) return;
  el.textContent = text;
  el.className = 'cred-msg' + (kind ? ' ' + kind : '');
}}

async function submitCredential() {{
  if (!credSchema || !credLabel) return;
  var activeMode = document.querySelector('.cred-tab.active').dataset.mode;
  var data = {{}};
  if (activeMode === 'form') {{
    var errors = [];
    credSchema.fields.forEach(function(f) {{
      var input = document.querySelector('#cred-form input[data-key="' + f.key + '"]');
      if (!input) return;
      var val = (input.value || '').trim();
      if (!val) return;  // 留空 = 不修改
      if (f.pattern) {{
        try {{
          var re = new RegExp(f.pattern);
          if (!re.test(val)) {{
            input.closest('.cred-field').querySelector('.cred-field-err').textContent = '格式不符';
            errors.push(f.label + ' 格式不符');
            return;
          }}
        }} catch (e) {{}}
      }}
      input.closest('.cred-field').querySelector('.cred-field-err').textContent = '';
      data[f.key] = val;
    }});
    if (errors.length) {{ showCredMsg(errors.join('; '), 'err'); return; }}
    if (Object.keys(data).length === 0) {{ showCredMsg('没有填写任何字段（留空 = 不修改）', 'warn'); return; }}
  }} else {{
    var raw = document.getElementById('cred-json-input').value.trim();
    if (!raw) {{ showCredMsg('请输入 JSON', 'err'); return; }}
    try {{ data = JSON.parse(raw); }}
    catch (e) {{ showCredMsg('JSON 解析失败: ' + e.message, 'err'); return; }}
    if (typeof data !== 'object' || Array.isArray(data)) {{ showCredMsg('JSON 必须是对象', 'err'); return; }}
  }}
  showCredMsg('保存中...', '');
  try {{
    var resp = await fetch('/api/secrets/' + credLabel + '/bulk', {{
      method: 'PUT',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{data: data}})
    }});
    var r = await resp.json();
    if (resp.ok) {{
      showCredMsg('✅ 已保存 ' + (r.imported || 0) + ' 个字段', 'ok');
      setTimeout(function() {{ closeCredModal(); location.reload(); }}, 800);
    }} else {{
      showCredMsg('保存失败: ' + (r.detail || JSON.stringify(r)), 'err');
    }}
  }} catch (e) {{
    showCredMsg('请求异常: ' + e, 'err');
  }}
}}

// ── 破解网关：额度/签到状态加载 ──
function loadCrackStatus(label, el) {{
  if (!el || !label) return;
  fetch('/api/crack/' + label + '/status')
    .then(function(resp) {{ return resp.json(); }})
    .then(function(r) {{
      if (!r.supported) {{
        el.innerHTML = '<div class="cs-err">该破解网关未接入状态查询</div>';
        return;
      }}
      if (!r.configured) {{
        el.innerHTML = '<div class="cs-err">凭据未配置，无法查询状态</div>';
        return;
      }}
      if (r.error) {{
        el.innerHTML = '<div class="cs-err">状态查询失败: ' + r.error + '</div>';
        return;
      }}
      var caps = r.capabilities || {{}};
      var account = r.account || '—';
      // 标题：网关名 · 账号（让用户知道是哪个账号登录的）
      var title = (r.displayName || label) + ' · ' + account;
      var html = '<div class="cs-head">' + title + '</div>';
      // 签到行（仅该网关有签到机制时显示）
      if (caps.hasCheckin && r.checkin) {{
        var c = r.checkin;
        var ciText = c.checkedIn
          ? '✅ 已签' + (c.credits ? ' (+' + c.credits + ')' : '')
          : '⚠️ 未签';
        var ciClass = c.checkedIn ? 'cs-checkin-ok' : 'cs-checkin-no';
        html += '<div class="cs-row"><span class="k">签到</span>' +
          '<span class="' + ciClass + '">' + ciText + '</span></div>';
      }}
      // token 到期（有值才显示）
      if (r.refresh && r.refresh.tokenExpireAt) {{
        var exp = r.refresh.tokenExpireAt.replace('T', ' ').slice(0, 16);
        html += '<div class="cs-row"><span class="k">token 到期</span><span>' + exp + '</span></div>';
      }}
      // 最后定时刷新（仅需签到/刷 token 的网关显示）
      if (caps.hasCheckin || caps.hasRefresh) {{
        var last = r.lastDailyRun ? r.lastDailyRun.replace('T', ' ').slice(0, 16) : '';
        html += '<div class="cs-row"><span class="k">最后定时刷新</span>' +
          (last ? '<span>' + last + '</span>' : '<span class="cs-never">尚未运行</span>') + '</div>';
      }}
      // 额度明细
      if (r.quota && r.quota.length) {{
        html += '<div class="cs-quota">';
        r.quota.forEach(function(q) {{
          if (q.error) {{ html += '<div class="cs-err">' + q.error + '</div>'; return; }}
          var limit = (q.limit === undefined || q.limit === null) ? '∞' : q.limit;
          var exp = q.expireAt ? (' · ' + q.expireAt.replace('T', ' ').slice(0, 10)) : '';
          html += '<div class="cs-qrow"><span class="qname">' + q.name + '</span>' +
            '<span>' + q.used + ' / ' + limit + '<span class="qexp">' + exp + '</span></span></div>';
        }});
        html += '</div>';
      }}
      el.innerHTML = html;
    }})
    .catch(function(e) {{
      el.innerHTML = '<div class="cs-err">加载失败: ' + e + '</div>';
    }});
}}

// 页面加载后加载所有 crack 卡片的额度/签到状态
function initCrackStatus() {{
  document.querySelectorAll('.crack-status').forEach(function(el) {{
    loadCrackStatus(el.dataset.label, el);
  }});
}}
document.addEventListener('DOMContentLoaded', initCrackStatus);
setTimeout(initCrackStatus, 300);

// ── 聚合网关（8080）：虚拟模型/会话/熔断状态加载 + 10s 自动刷新 ──
function escHtml(s) {{
  return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}}

function aggBreakerInfo(state) {{
  if (state === 'tripped') return {{cls: 'agg-dot bad', label: '已熔断'}};
  if (state === 'probing') return {{cls: 'agg-dot warn', label: '探测中'}};
  return {{cls: 'agg-dot ok', label: '正常'}};
}}

function aggErrorLabel(et) {{
  if (et === '401_auth') return '凭据失效';
  if (et === '402_billing') return '欠费';
  if (et === '403_forbidden') return '禁止访问';
  if (et === '429_rate_limit') return '限流';
  if (et === 'quota_text') return '配额耗尽';
  if (et === 'unclassified') return '未分类';
  if (et === '5xx_persistent') return '5xx 持续';
  if (et.endsWith('_transient')) return '瞬时限流';
  return et;
}}

function formatTs(ts) {{
  if (!ts || ts === 0) return '—';
  try {{ return new Date(ts * 1000).toLocaleString(); }} catch {{ return String(ts); }}
}}

function aggMemberDot(m) {{
  if (!m || m.requests === 0) return 'agg-dot dim';
  var bad = (m.err || 0) + (m.degraded || 0);
  if (bad === 0) return 'agg-dot ok';
  if ((m.err || 0) > 0 && (m.err || 0) >= m.requests * 0.5) return 'agg-dot bad';
  return 'agg-dot warn';
}}

function togglePoolDetail(rowEl) {{
  var vmId = rowEl.getAttribute('data-vmid');
  if (!vmId) return;
  var d = document.getElementById('pool-' + vmId);
  if (d) d.open = !d.open;
}}

async function loadAggregateStatus() {{
  var el = document.getElementById('agg-status');
  if (!el) return;
  // 保留用户当前展开的池详情 id（10s 刷新会重写 innerHTML，需恢复 open 状态）
  var openIds = [];
  el.querySelectorAll('details.agg-vm-detail[open]').forEach(function(d){{ openIds.push(d.id); }});
  try {{
    var resp = await fetch('/api/aggregate/status');
    var r = await resp.json();
    if (!r.configured) {{
      el.innerHTML = '<div class="cs-err">聚合网关未配置（targets.json 中缺少聚合 target）</div>';
      return;
    }}
    var sess = r.session || {{}};
    var hitRate = (sess.hit_rate || 0) * 100;
    var cacheSize = sess.cache_size || 0;
    var html = '<div class="cs-head">🔀 聚合网关 · 命中率 ' + hitRate.toFixed(1) + '% · 粘性缓存 ' + cacheSize + ' 条</div>';
    // 池配置 JSON（注入自服务端 <script id="agg-pool-data">）
    var cfgScript = document.getElementById('agg-pool-data');
    var cfg = {{}};
    try {{ cfg = cfgScript ? JSON.parse(cfgScript.textContent || '{{}}') : {{}}; }} catch(e) {{ cfg = {{}}; }}
    // ── 主表：每虚拟模型一行（配置全貌，含无流量 vm）──
    var vms = r.virtual_models || {{}};
    var vmIds = Object.keys(cfg);
    var vmRowHtml = '';
    var i = 0;
    vmIds.forEach(function(vmId) {{
      i++;
      var cfgVm = cfg[vmId] || {{}};
      var defPool = cfgVm.defaultPool || [];
      var fbPool = cfgVm.fallbackPool || [];
      var membersStats = vms[vmId] || {{}};
      var totReq=0, totOk=0, totErr=0, totDeg=0;
      Object.keys(membersStats).forEach(function(mk){{
        var m = membersStats[mk] || {{}};
        totReq += m.requests||0; totOk += m.ok||0; totErr += m.err||0; totDeg += m.degraded||0;
      }});
      var hasTraf = totReq > 0;
      var rate = hasTraf ? (totOk/totReq*100).toFixed(1) + '%' : '—';
      vmRowHtml += '<tr data-vmid="' + escHtml(vmId) + '" onclick="togglePoolDetail(this)" style="cursor:pointer;" title="点击展开池详情">' +
        '<td class="num">' + i + '</td>' +
        '<td class="mid"><code>' + escHtml(vmId) + '</code></td>' +
        '<td class="name">默认池 ' + defPool.length + ' · 降级池 ' + fbPool.length + '</td>' +
        '<td class="mstat">' + (hasTraf?totReq:'—') + '</td>' +
        '<td class="mstat">' + rate + '</td>' +
        '<td class="mstat err">' + (hasTraf?totErr:'—') + '</td>' +
        '<td class="mstat warn">' + (hasTraf?totDeg:'—') + '</td>' +
        '</tr>';
    }});
    if (vmIds.length === 0) {{
      html += '<div class="no-models">(暂无虚拟模型配置)</div>';
    }} else {{
      html += '<table class="model-table"><thead><tr>' +
        '<th>#</th><th>模型 ID</th><th>名称</th><th>请求</th><th>成功率</th><th>错误</th><th>降级</th>' +
        '</tr></thead><tbody>' + vmRowHtml + '</tbody></table>';
    }}
    // ── 池详情折叠：每个 vm 一个 details（默认收起，点击主表行 toggle）──
    vmIds.forEach(function(vmId) {{
      var cfgVm = cfg[vmId] || {{}};
      var defPool = cfgVm.defaultPool || [];
      var fbPool = cfgVm.fallbackPool || [];
      var membersStats = vms[vmId] || {{}};
      function renderPool(pool, label) {{
        if (pool.length === 0) return '<div class="no-models">(' + label + ' 为空)</div>';
        var rows = '';
        for (var j=0; j<pool.length; j++) {{
          var p = pool[j] || {{}};
          var port = p.port;
          var model = p.model || '';
          var w = p.weight;
          var mk = port + ':' + model;
          var ms = membersStats[mk] || {{}};
          var req = ms.requests || 0;
          var ok = ms.ok || 0;
          var err = ms.err || 0;
          var deg = ms.degraded || 0;
          var lraw = ms.avg_latency_ms || 0;
          var hasTraf = req > 0;
          var rateStr = hasTraf ? (ok/req*100).toFixed(1) + '%' : '—';
          var latStr = hasTraf ? lraw.toFixed(0) + 'ms' : '—';
          var wStr = (w === undefined || w === null) ? '—' : String(w);
          rows += '<tr>' +
            '<td class="num">' + (j+1) + '</td>' +
            '<td class="mid"><code>:' + port + '</code> · <code>' + escHtml(model) + '</code></td>' +
            '<td class="mstat">' + wStr + '</td>' +
            '<td class="mstat">' + (hasTraf?req:'—') + '</td>' +
            '<td class="mstat">' + rateStr + '</td>' +
            '<td class="mstat err">' + (hasTraf?err:'—') + '</td>' +
            '<td class="mstat warn">' + (hasTraf?deg:'—') + '</td>' +
            '<td class="mstat">' + latStr + '</td>' +
            '</tr>';
        }}
        return '<div class="agg-pool-block"><div class="agg-vm-head">' + label + '（' + pool.length + ' 成员）</div>' +
          '<table class="model-table"><thead><tr>' +
          '<th>#</th><th>端口 · 模型</th><th>权重</th><th>请求</th><th>成功率</th><th>错误</th><th>降级</th><th>延迟</th>' +
          '</tr></thead><tbody>' + rows + '</tbody></table></div>';
      }}
      html += '<details class="agg-vm-detail" id="pool-' + escHtml(vmId) + '">' +
        '<summary><span class="agg-vm-head" style="margin:0; font-size:12.5px;">📦 ' + escHtml(vmId) + ' · ' + defPool.length + ' 默认 + ' + fbPool.length + ' 降级</span></summary>' +
        '<div class="agg-vm-body">' +
        renderPool(defPool, '默认池') +
        renderPool(fbPool, '降级池') +
        '</div></details>';
    }});
    // ── ⚠️ 高危事件区：熔断端口 + 错误类型汇总 ──
    var brks = r.breakers || {{}};
    var brkKeys = Object.keys(brks);
    // 汇总所有虚拟模型的 error_types
    var errTypeCounts = {{}};
    var vms = r.virtual_models || {{}};
    Object.keys(vms).forEach(function(vmId) {{
      var members = vms[vmId] || {{}};
      Object.keys(members).forEach(function(mk) {{
        var m = members[mk] || {{}};
        var ets = m.error_types || {{}};
        Object.keys(ets).forEach(function(et) {{
          errTypeCounts[et] = (errTypeCounts[et] || 0) + ets[et];
        }});
      }});
    }});
    var hasBreakers = brkKeys.length > 0;
    var hasErrTypes = Object.keys(errTypeCounts).length > 0;
    if (hasBreakers || hasErrTypes) {{
      html += '<div class="agg-vm"><div class="agg-vm-head">⚠️ 高危事件</div>';
      if (hasBreakers) {{
        html += '<div class="agg-vm-head" style="margin-top:6px; font-size:11px; color:#fbbf24;">熔断端口</div>';
        brkKeys.forEach(function(port) {{
          var b = brks[port] || {{}};
          var info = aggBreakerInfo(b.state);
          var ts = formatTs(b.tripped_at);
          html += '<div class="agg-brk"><span class="' + info.cls + '"></span>' +
            '<span class="m">:' + escHtml(port) + '</span>' +
            '<span>' + info.label + '</span>' +
            (b.reason ? '<span class="reason">' + escHtml(b.reason) + '</span>' : '') +
            (ts !== '—' ? '<span class="reason" style="color:#8b93a7;">' + escHtml(ts) + '</span>' : '') + '</div>';
        }});
      }}
      if (hasErrTypes) {{
        html += '<div class="agg-vm-head" style="margin-top:6px; font-size:11px; color:#7dd3fc;">错误类型统计</div>';
        var etKeys = Object.keys(errTypeCounts).sort(function(a,b) {{ return errTypeCounts[b] - errTypeCounts[a]; }});
        etKeys.forEach(function(et) {{
          var cnt = errTypeCounts[et];
          var label = aggErrorLabel(et);
          html += '<div class="agg-vm-row"><span class="m">' + escHtml(label) + ' <code style="color:#6b7280;font-size:10px;">(' + escHtml(et) + ')</code></span>' +
            '<span class="s" style="color:#f87171;font-weight:600;">' + cnt + '</span></div>';
        }});
      }}
      html += '</div>';
    }}
    el.innerHTML = html;
    // 恢复用户展开的池详情（10s 刷新重写 innerHTML 后 open 状态会丢失）
    openIds.forEach(function(id){{
      var d = document.getElementById(id);
      if (d) d.open = true;
    }});
  }} catch (e) {{
    el.innerHTML = '<div class="cs-err">加载失败: ' + e + '</div>';
  }}
}}

// 聚合网关：页面加载立即拉取一次 + 每 10s 自动刷新（仅 8080 卡片，独立于破解卡片刷新）
(function() {{
  if (document.getElementById('agg-status')) {{
    loadAggregateStatus();
    setInterval(loadAggregateStatus, 10000);
  }}
}})();
</script>
</body>
</html>"""

