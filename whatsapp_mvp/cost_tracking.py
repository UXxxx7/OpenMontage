# WhatsApp MVP - LLM token usage ledger + pricing
#
# 补的是这个仓库里此前完全没有的一块：call_llm_chat/call_vision_chat 每次
# 调用的 usage（token 数）从来没被读过，"这条视频到底花了多少 AI 成本"只有
# generation_cost_usd 那一小块（b-roll/背景音乐等生成类工具），LLM 调用本身
# 的开销完全看不见。
#
# 独立成一个模块（不放进 content_planner.py 或 pipeline_runner.py）：前者要
# 往这里写，后者已经有一份姊妹账本（_record_generation_cost/_read_generation_cost，
# 同样的"per-job workdir JSON 文件"设计，避免并发任务互相污染——WA_WORKER_
# CONCURRENCY>1 时模块级变量会被多个任务共享写坏），谁都不该 import 谁。

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 定价来源：公开聚合站点，2026-07-24 抓取，需要定期对照各家官方定价页复核
# （第三方聚合站可能滞后于官方实际调价）。单位：每百万 token 的美元价格。
#   - DeepSeek v4-flash（这个项目 .env 实际配置的主规划模型）：
#     input_miss $0.14/1M，input_hit $0.0028/1M（命中缓存便宜 50 倍），
#     output $0.28/1M。
#   - Gemini flash-latest（VISION_LLM_MODEL，只用于视觉复审，是一个完全
#     独立于主规划的 provider/模型）：input $1.50/1M，output $7.50/1M
#     （没有确认过这条路径存在缓存命中折扣，未提供 input_hit 视为跟
#     input_miss 同价）。
_PRICING: dict[tuple[str, str], dict[str, float]] = {
    ("deepseek", "deepseek-v4-flash"): {
        "input_miss": 0.14, "input_hit": 0.0028, "output": 0.28,
    },
    ("google", "gemini-flash-latest"): {
        "input_miss": 1.50, "input_hit": 1.50, "output": 7.50,
    },
}

_LLM_USAGE_LEDGER_NAME = "_llm_usage.json"


def record_llm_usage(workdir: Path, source: str, provider: str, model: str, usage: Optional[dict]) -> None:
    """把一次 LLM 调用的 token 用量追加进这个 job 的账本文件——跟
    pipeline_runner.py 的 `_record_generation_cost` 同一个套路（per-job
    workdir 文件，不是模块级变量），理由也一样：并发任务不能共享同一份
    内存状态。`usage` 为 None（调用失败/没配置）时直接跳过，不记空账。
    """
    if not usage:
        return
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    cache_hit_tokens = int(usage.get("cache_hit_tokens") or 0)
    if prompt_tokens == 0 and completion_tokens == 0:
        return
    ledger_path = workdir / _LLM_USAGE_LEDGER_NAME
    entries = []
    if ledger_path.exists():
        try:
            entries = json.loads(ledger_path.read_text(encoding="utf-8"))
        except Exception:
            entries = []
    entries.append({
        "source": source,
        "provider": provider,
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cache_hit_tokens": cache_hit_tokens,
    })
    ledger_path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")


def _price_entry(entry: dict) -> float:
    rates = _PRICING.get((entry.get("provider", ""), entry.get("model", "")))
    if rates is None:
        logger.warning(
            f"cost_tracking: 未知的 (provider, model) 组合 "
            f"({entry.get('provider')!r}, {entry.get('model')!r})，无法定价，按 $0 计（不阻断任务）"
        )
        return 0.0
    prompt_tokens = entry.get("prompt_tokens", 0)
    completion_tokens = entry.get("completion_tokens", 0)
    cache_hit_tokens = min(entry.get("cache_hit_tokens", 0), prompt_tokens)
    cache_miss_tokens = prompt_tokens - cache_hit_tokens
    cost = (
        cache_miss_tokens / 1_000_000 * rates["input_miss"]
        + cache_hit_tokens / 1_000_000 * rates["input_hit"]
        + completion_tokens / 1_000_000 * rates["output"]
    )
    return cost


def read_llm_usage(workdir: Path) -> dict:
    """汇总这个 job 账本里的所有条目，返回总 token 数 + 按 `_PRICING` 折算的
    总花费。未知的 (provider, model) 组合按 $0 计（不阻断任务完成），只记
    一条警告日志——换了模型/provider 的当天就会撞上这个分支，必须安全降级。
    """
    ledger_path = workdir / _LLM_USAGE_LEDGER_NAME
    if not ledger_path.exists():
        return {"prompt_tokens": 0, "completion_tokens": 0, "cache_hit_tokens": 0, "cost_usd": 0.0}
    try:
        entries = json.loads(ledger_path.read_text(encoding="utf-8"))
    except Exception:
        return {"prompt_tokens": 0, "completion_tokens": 0, "cache_hit_tokens": 0, "cost_usd": 0.0}
    total_prompt = sum(e.get("prompt_tokens", 0) for e in entries)
    total_completion = sum(e.get("completion_tokens", 0) for e in entries)
    total_cache_hit = sum(e.get("cache_hit_tokens", 0) for e in entries)
    total_cost = round(sum(_price_entry(e) for e in entries), 6)
    return {
        "prompt_tokens": total_prompt,
        "completion_tokens": total_completion,
        "cache_hit_tokens": total_cache_hit,
        "cost_usd": total_cost,
    }
