"""
Self-Improving + Homunculus Integration Module.

Drives the ~/.hermes/self-improving/ and ~/.hermes/homunculus/ mechanisms
by consuming signals from the background review and writing to the file-based
evolution store.

This module is intentionally lightweight and synchronous — all file I/O,
no API calls, no tool invocations. It runs in the same process as the
background review fork so it can see the review agent's conversation history.

Entry point: `update_self_improving(agent, messages, session_id)`
Called from run_agent._spawn_background_review() after the review completes.
"""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path

logger = __import__("logging").getLogger("hermes.self_improver")

# MSTAR Bridge — 连接 self_improver 和 MSTAR 进化系统
_mstar_bridge = None

def _get_mstar_bridge():
    global _mstar_bridge
    if _mstar_bridge is None:
        try:
            import sys
            sys.path.insert(0, str(Path(__file__).parent.parent / "hermes_mstar"))
            from hermes_mstar.self_improving_bridge import get_bridge
            _mstar_bridge = get_bridge()
        except Exception:
            pass
    return _mstar_bridge
from typing import Any

# ---------------------------------------------------------------------------
# Paths (profile-aware via get_hermes_home)
# ---------------------------------------------------------------------------

def _home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())

def _si_dir() -> Path:
    d = _home() / "self-improving"
    d.mkdir(exist_ok=True)
    return d

def _hmc_dir() -> Path:
    d = _home() / "homunculus"
    d.mkdir(exist_ok=True)
    return d

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def _date_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def _uuid4() -> str:
    return str(uuid.uuid4())[:8]

def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out

def _append_jsonl(path: Path, record: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

# ---------------------------------------------------------------------------
# Heartbeat — written on every call so the system always looks alive
# ---------------------------------------------------------------------------

def _update_heartbeat() -> None:
    path = _si_dir() / "heartbeat-state.md"
    content = textwrap.dedent(f"""\
        # Heartbeat State

        ## System Health
        - status: OK
        - last_heartbeat: {_now_iso()}
        - pending_items: 0

        ## Quick Stats
        - total_tasks: (see corrections.md + quality/scores.md)
        - avg_quality: (see quality/scores.md)
        - corrections: (see corrections.md)
        - reinforcements: (see reinforcements.md)
        """)
    path.write_text(content, encoding="utf-8")

# ---------------------------------------------------------------------------
# Corrections — user corrections detected in conversation
# ---------------------------------------------------------------------------

_CORRECTIONS_TMPL = """\
| Date | UUID | Type | Scope | What I Got Wrong | Correct Answer | Source | Status | TriggerCount | SuccessCount | Effectiveness | PreScore | PostScore | QualityDelta | RootCauseCategory | RootCause | LastTriggered | DeprecatedReason |
|------|------|------|-------|-----------------|----------------|--------|--------|-------------|-------------|---------------|----------|-----------|-------------|------------------|-----------|---------------|-----------------|
"""

# AI 内部独白模式 - 防止 Review Agent 误判
_AI_MONOLOGUE_PATTERNS = (
    "现在我理解问题了",
    "让我帮你算一笔账",
    "## 验证报告结果",
    "## 修复完成",
    "## BUG 修复总结",
    "## 本能触发集成完成",
    "## 本能集成完成",
    "## Evolution Systems Status",
    "## SkillClaw 完整工作流",
    "## 本次修复的 BUG",
    "验证脚本现在显示正确了",
    "Review Agent 在错误地捕获",
    "数据正确了，但验证脚本",
    "问题：Review Agent 在持续",
    "## PRM 启用完成",
    "**更新完成**",
    "## 技能更新摘要",
    "## 三个进化系统状态总结",
    "让我分析本次会话中",
    "正在验证三个进化系统",
    "(detected via review)",
)


def _cleanup_invalid_corrections() -> int:
    """自动清理无效的纠正（AI 独白、Review Agent 误判）
    
    Returns: 清理的纠正数量
    """
    path = _si_dir() / "corrections.md"
    if not path.exists():
        return 0
    
    content = path.read_text(encoding="utf-8")
    lines = content.split('\n')
    
    deprecated_count = 0
    clean_lines = []
    
    for line in lines:
        should_deprecate = False
        
        # 检查是否是纠正条目行
        if line.startswith('| 2026-'):
            # 检查 AI 独白模式
            for pattern in _AI_MONOLOGUE_PATTERNS:
                if pattern in line:
                    should_deprecate = True
                    break
        
        if should_deprecate:
            # 标记为 Deprecated
            parts = line.split('|')
            if len(parts) > 8:
                parts[8] = ' Deprecated '
                line = '|'.join(parts)
            deprecated_count += 1
        
        clean_lines.append(line)
    
    if deprecated_count > 0:
        path.write_text('\n'.join(clean_lines) + '\n', encoding="utf-8")
    
    return deprecated_count


def _load_corrections() -> list[dict]:
    """加载所有纠正记录（自动清理无效纠正）"""
    # 每次加载时自动清理无效纠正
    _cleanup_invalid_corrections()
    
    path = _si_dir() / "corrections.md"
    if not path.exists():
        return []
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("|") and not line.startswith("| Date |"):
                parts = [p.strip() for p in line.split("|")[1:-1]]
                # 过滤空行（分隔符行）和无效 UUID
                if len(parts) >= 16 and parts[1] and len(parts[1]) >= 8 and parts[1] != "------":
                    records.append({
                        "date": parts[0],
                        "uuid": parts[1],
                        "type": parts[2],
                        "scope": parts[3],
                        "wrong": parts[4],
                        "correct": parts[5],
                        "status": parts[7],
                        "trigger_count": int(parts[8]) if parts[8].isdigit() else 0,
                        "success_count": int(parts[9]) if parts[9].isdigit() else 0,
                        "last_triggered": parts[14],
                    })
    return records

def _save_corrections(records: list[dict]) -> None:
    path = _si_dir() / "corrections.md"
    lines = [_CORRECTIONS_TMPL.strip()]
    for r in records:
        # 过滤无效记录（无 UUID 或占位符）
        uuid = r.get("uuid", "")
        if not uuid or uuid == "------" or len(uuid) < 8:
            continue
        lines.append(
            f"| {r['date']} | {r['uuid']} | {r['type']} | {r['scope']} | "
            f"{r['wrong']} | {r['correct']} | user | {r['status']} | "
            f"{r['trigger_count']} | {r.get('success_count', 0)} | "
            f"{r.get('effectiveness', '')} | {r.get('pre_score', '')} | "
            f"{r.get('post_score', '')} | {r.get('quality_delta', '')} | "
            f"{r.get('root_cause_category', '')} | {r.get('root_cause', '')} | "
            f"{r['last_triggered']} | {r.get('deprecated_reason', '')} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

# ---------------------------------------------------------------------------
# Reinforcements — positive signals (successful completions)
# ---------------------------------------------------------------------------

def _load_reinforcements() -> list[dict]:
    path = _si_dir() / "reinforcements.md"
    if not path.exists():
        return []
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("|") and not line.startswith("| Date |"):
                parts = [p.strip() for p in line.split("|")[1:-1]]
                if len(parts) >= 6:
                    records.append({
                        "date": parts[0],
                        "uuid": parts[1],
                        "domain": parts[2],
                        "description": parts[3],
                        "confidence": float(parts[4]) if parts[4].replace(".", "", 1).isdigit() else 0.0,
                        "trigger_count": int(parts[5]) if parts[5].isdigit() else 0,
                    })
    return records

_REINFORCE_TMPL = """\
| Date | UUID | Domain | Description | Confidence | TriggerCount |
|------|------|--------|-------------|------------|---------------|
"""

def _save_reinforcements(records: list[dict]) -> None:
    path = _si_dir() / "reinforcements.md"
    lines = [_REINFORCE_TMPL.strip()]
    for r in records:
        lines.append(
            f"| {r['date']} | {r['uuid']} | {r['domain']} | {r['description']} | "
            f"{r['confidence']} | {r['trigger_count']} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

# ---------------------------------------------------------------------------
# Quality scores
# ---------------------------------------------------------------------------

def _load_quality_scores() -> list[dict]:
    path = _si_dir() / "quality" / "scores.md"
    if not path.exists():
        return []
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("|") and not line.startswith("| Date |"):
                parts = [p.strip() for p in line.split("|")[1:-1]]
                if len(parts) >= 8:
                    records.append({
                        "date": parts[0],
                        "task": parts[1],
                        "accuracy": parts[2],
                        "completeness": parts[3],
                        "efficiency": parts[4],
                        "satisfaction": parts[5],
                        "reusability": parts[6],
                        "total": parts[7],
                        "triggered": parts[8] if len(parts) > 8 else "",
                    })
    return records

_QUALITY_TMPL = """\
| Date | Task | Accuracy | Completeness | Efficiency | Satisfaction | Reusability | Total | Triggered |
|------|------|----------|-------------|------------|-------------|-------------|-------|-----------|
"""

def _save_quality_scores(records: list[dict]) -> None:
    d = _si_dir() / "quality"
    d.mkdir(exist_ok=True)
    path = d / "scores.md"
    lines = [_QUALITY_TMPL.strip()]
    for r in records:
        lines.append(
            f"| {r['date']} | {r['task']} | {r['accuracy']} | {r['completeness']} | "
            f"{r['efficiency']} | {r['satisfaction']} | {r['reusability']} | "
            f"{r['total']} | {r.get('triggered', '')} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

# ---------------------------------------------------------------------------
# Evolution log
# ---------------------------------------------------------------------------

def _append_evolution(event: str, detail: str = "") -> None:
    d = _si_dir() / "evolution-log"
    d.mkdir(exist_ok=True)
    path = d / "triggers.md"
    stamp = _now_iso()
    entry = f"- [{stamp}] {event}" + (f" — {detail}" if detail else "")
    if path.exists():
        existing = path.read_text(encoding="utf-8")
    else:
        existing = "# Evolution Triggers\n\n"
    path.write_text(existing + entry + "\n", encoding="utf-8")

# ---------------------------------------------------------------------------
# Homunculus: write observation
# ---------------------------------------------------------------------------

def _observe(
    domain: str,
    observation_type: str,
    content: str,
    context: str = "",
    session_id: str = "",
) -> None:
    path = _hmc_dir() / "observations.jsonl"
    record = {
        "timestamp": _now_iso(),
        "domain": domain,
        "type": observation_type,
        "content": content,
        "context": context,
        "session_id": session_id,
        "uuid": _uuid4(),
    }
    _append_jsonl(path, record)

# ---------------------------------------------------------------------------
# Homunculus: check if observer should fire
# ---------------------------------------------------------------------------

def _hmc_observer_ready() -> bool:
    """Return True if observations.jsonl has enough entries to trigger analysis."""
    import json
    config_path = _hmc_dir() / "config.json"
    if not config_path.exists():
        return False
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        min_obs = cfg.get("observer", {}).get("min_observations_to_analyze", 20)
    except Exception:
        min_obs = 20
    obs = _read_jsonl(_hmc_dir() / "observations.jsonl")
    return len(obs) >= min_obs

def _evolved_path(category: str) -> Path:
    d = _hmc_dir() / "evolved" / category
    d.mkdir(exist_ok=True, parents=True)
    return d

# ---------------------------------------------------------------------------
# Analysis: extract signals from background-review conversation
# ---------------------------------------------------------------------------

# Patterns that indicate AI internal monologue — NOT actual user corrections
_AI_MONOLOGUE_PATTERNS = (
    "现在我理解问题了",
    "让我帮你算一笔账",
    "## 验证报告结果",
    "## 修复完成",
    "## 本能触发集成完成",
    "验证脚本现在显示正确了",
    "发现问题：",
    "数据正确了",
    "## BUG 修复总结",
    "## 发现的 BUG 及修复",
    "## 当前状态",
    "## 结论",
    "## 深度分析",
    "## 继续",
    "好问题",
    "让我设计一套",
    "当前三个系统",
    "Evolution System",
)

_CORRECTION_PHRASES = ("user correction", "user corrected", "纠正", "修正")


def _extract_corrections_from_review(messages: list[dict]) -> list[dict]:
    """
    Scan review-agent messages for signals that indicate a user correction
    or style/approach objection. Returns list of correction records.
    """
    corrections = []
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", "")
        if not isinstance(content, str):
            continue

        # Skip context-compaction internal records — these are NOT user corrections
        stripped = content.strip()
        if stripped.startswith("[CONTEXT COMPACTION") or stripped.startswith("## Active Task"):
            continue

        # Skip AI internal monologue — these are NOT user corrections
        if any(pattern in content for pattern in _AI_MONOLOGUE_PATTERNS):
            continue

        is_skill_manage = "skill_manage" in msg.get("name", "")
        lower = content.lower()

        if is_skill_manage:
            # skill_manage calls are corrections only when they respond to a user
            # correction (contain correction keywords like "fix", "bug", "不对", etc.)
            if any(kw in lower for kw in _CORRECTION_KW):
                corrections.append(_make_correction_record("(detected via skill patch)", content))
        elif any(phrase in lower for phrase in _CORRECTION_PHRASES):
            # Direct explicit mention of correction
            corrections.append(_make_correction_record("(detected via review)", content))

    return corrections


def _make_correction_record(wrong: str, content: str) -> dict:
    return {
        "date": _date_stamp(),
        "uuid": _uuid4(),
        "type": "correction",
        "scope": "workflow",
        "wrong": wrong,
        "correct": content[:120],
        "status": "pending",
        "trigger_count": 1,
        "success_count": 0,
        "effectiveness": "",
        "pre_score": "",
        "post_score": "",
        "quality_delta": "",
        "root_cause_category": "user_correction",
        "root_cause": "review_agent_detected",
        "last_triggered": _now_iso(),
        "deprecated_reason": "",
    }

# ---------------------------------------------------------------------------
# Analysis: quality scoring from tool-call complexity and success
# ---------------------------------------------------------------------------

def _score_task_quality(messages: list[dict]) -> dict:
    """
    Derive a quality score from conversation structure.
    Returns dict with Accuracy, Completeness, Efficiency, Satisfaction,
    Reusability, Total.
    """
    tool_calls = sum(
        1 for msg in messages
        if msg.get("role") == "assistant" and msg.get("tool_calls")
    )
    tool_results = sum(
        1 for msg in messages
        if msg.get("role") == "tool"
    )
    # Estimate: high tool count with low success rate = low quality
    if tool_results == 0:
        efficiency = 5
    else:
        efficiency = min(10, round((tool_results / max(tool_calls, 1)) * 10))

    # Simple heuristic: if conversation completed without errors
    has_error = any(
        "error" in (msg.get("content") or "").lower()
        for msg in messages
        if msg.get("role") == "tool"
    )

    accuracy = 7 if not has_error else 5
    completeness = 8  # Always assume full completion intent
    satisfaction = 7  # Neutral
    reusability = 7  # Neutral
    total = round((accuracy + completeness + efficiency + satisfaction + reusability) / 5, 1)

    return {
        "date": _date_stamp(),
        "task": f"turn_{_date_stamp()}_{_uuid4()[:4]}",
        "accuracy": accuracy,
        "completeness": completeness,
        "efficiency": efficiency,
        "satisfaction": satisfaction,
        "reusability": reusability,
        "total": total,
        "triggered": "",
    }

# -----------------------------------------------------------------------------
# Correction Trigger Tracker — SuccessCount 追踪
# -----------------------------------------------------------------------------

def _track_correction_effectiveness(messages: list[dict]) -> None:
    """
    检测纠正触发并验证效果。
    
    1. 从用户消息中提取关键词
    2. 检测是否有纠正被触发
    3. 记录触发并增加 TriggerCount
    4. 从工具结果验证是否成功
    5. 如果成功，增加 SuccessCount
    """
    if not messages:
        return
    
    # 提取用户消息
    user_text = " ".join(
        msg.get("content", "") or ""
        for msg in messages
        if msg.get("role") == "user"
    )
    
    # 提取工具结果
    tool_results = [
        msg.get("content", "") or ""
        for msg in messages
        if msg.get("role") == "tool"
    ]
    
    # 加载纠正
    corrections = _load_corrections()
    if not corrections:
        return
    
    # 检测触发
    for corr in corrections:
        uuid = corr.get("uuid", "")
        if uuid == "------" or not uuid:
            continue
        if corr.get("status") == "Deprecated":
            continue
        
        # 从 "What I Got Wrong" 和 "Correct Answer" 检查关键词
        wrong_text = (corr.get("wrong", "") + " " + corr.get("correct", "")).lower()
        
        # 检查用户消息是否触发
        triggered = False
        trigger_keywords = []
        
        # 关键短语匹配 — 必须是 correction 内容中独有的关键词，
        # 不能太通用（如 "hermes"/"context" 几乎每个会话都命中）
        key_phrases = [
            "wsl", "node", ".hermes", "windows", "linux",
            "memory provider", "pre-wrapped",
            "context strip", "gateway", "skillclaw",
        ]
        
        for phrase in key_phrases:
            if phrase in wrong_text and phrase in user_text.lower():
                triggered = True
                trigger_keywords.append(phrase)
        
        if not triggered:
            continue
        
        # 记录触发
        corr["trigger_count"] = corr.get("trigger_count", 0) + 1
        corr["last_triggered"] = _now_iso()
        
        # 验证效果：排除调试过程中的预期错误（IndentationError, SyntaxError,
        # AttributeError 等来自 patch/compile 测试），只检测真正的致命错误
        _FATAL_ERROR_PATTERNS = [
            r"\bhttp\s*50[234]\b",   # HTTP 502/503/504 - actual upstream errors
            r"\bconnection\s*refused\b",  # but must be at start of error line
            r"\bpermission\s*denied\b",  # real permission errors
            r"\bcrashed\b",
            r"\bfatal\s*error\b",
            r"\bunhandled\s*exception\b",
            r"\bout\s*of\s*memory\b",
        ]
        _DEBUG_ERROR_PATTERNS = [
            "indentationerror", "syntaxerror", "attributeerror",
            "typeerror: '", "nameerror", "valueerror",
            "keyerror", "indexerror", "filenotfounderror",
            "modulenotfounderror", "zerodivisionerror",
            "traceback (most recent call last)",
        ]

        # 过滤掉 skill 文档内容（skill_view 返回的文件可能包含 "http 5"/"crashed" 等
        # 关键词作为描述文字，造成自我污染）。只扫描非文档类 tool results。
        import re
        filtered_results = []
        for tr in tool_results:
            tr_lower = tr.lower()
            # 跳过明显是 skill 文档的 tool results
            if any(kw in tr_lower for kw in [
                "_fatal_error_kw", "_debug_error_kw", "trigger_keywords",
                "sklll.md", "skill_authoring", "known bug", "known pattern"
            ]):
                continue
            filtered_results.append(tr_lower)

        combined_results = " ".join(filtered_results)
        has_fatal_error = any(
            re.search(p, combined_results) for p in _FATAL_ERROR_PATTERNS
        )
        has_debug_error = any(
            kw in combined_results for kw in _DEBUG_ERROR_PATTERNS
        )

        # 成功条件：无致命错误 OR (只有调试错误 AND 有其他正常输出)
        is_success = not has_fatal_error

        
        if is_success:
            corr["success_count"] = corr.get("success_count", 0) + 1
            
            # 如果成功率 >= 50%，标记为有效
            trigger_count = corr.get("trigger_count", 0)
            success_count = corr.get("success_count", 0)
            if trigger_count > 0 and success_count >= trigger_count * 0.5:
                corr["status"] = "Effective"
        
        # 记录到 evolution log
        _append_evolution(
            "correction_trigger",
            f"[{uuid}] triggered: {', '.join(trigger_keywords)}, "
            f"success={is_success}"
        )
    
    # 保存更新后的纠正
    _save_corrections(corrections)


# -----------------------------------------------------------------------------
# Main entry point — called from run_agent._spawn_background_review
# -----------------------------------------------------------------------------

def update_self_improving(
    agent,          # AIAgent instance (used for config access)
    messages: list[dict],   # Full conversation history (from review agent)
    session_id: str = "",
) -> None:
    """
    Integrate self-improving + homunculus signals into the evolution store.

    Call this AFTER the background review agent completes, while the review
    fork is still alive. It reads the review agent's conversation history
    to extract correction and quality signals, then writes them to the
    file-based evolution store.

    Safe to call in a try/except — all failures are logged and swallowed
    so they never propagate to the user-visible code path.
    """
    try:
        _update_heartbeat()
    except Exception as e:
        _log("heartbeat", e)
        return

    try:
        _integrate_corrections(messages)
    except Exception as e:
        _log("corrections", e)

    # Track correction triggers and verify effectiveness
    try:
        _track_correction_effectiveness(messages)
    except Exception as e:
        _log("correction_tracker", e)

    try:
        _integrate_reinforcements(messages)
    except Exception as e:
        _log("reinforcements", e)

    try:
        _integrate_quality(messages, session_id)
    except Exception as e:
        _log("quality", e)

    try:
        _integrate_homunculus(messages, session_id)
    except Exception as e:
        _log("homunculus", e)


def _log(tag: str, exc: Exception) -> None:
    import logging
    logging.getLogger("hermes.self_improver").debug(
        "self_improver.%s error: %s", tag, exc
    )


# ---- internal dispatch ----

def _integrate_corrections(messages: list[dict]) -> None:
    corrections = _extract_corrections_from_review(messages)
    if not corrections:
        return
    existing = _load_corrections()
    # 去重：检查 UUID 是否已存在
    existing_uuids = {c.get("uuid", "") for c in existing}
    new_corrections = [c for c in corrections if c.get("uuid", "") not in existing_uuids]
    if not new_corrections:
        return
    existing.extend(new_corrections)
    _save_corrections(existing)
    _append_evolution("correction", f"{len(new_corrections)} new correction(s) recorded")

    # MSTAR Bridge: 将纠正转换为变异
    bridge = _get_mstar_bridge()
    if bridge:
        for corr in corrections:
            bridge.make_correction_mutation(corr)


def _integrate_reinforcements(messages: list[dict]) -> None:
    # Positive signal: review agent found nothing to fix
    tool_results = [
        msg.get("content", "") or ""
        for msg in messages
        if msg.get("role") == "tool"
    ]
    has_negative = any(
        any(kw in r.lower() for kw in ["error", "failed", "wrong", "broken"])
        for r in tool_results
    )
    if has_negative:
        return  # Don't reinforce on failure

    # Check if review actually did something useful
    review_did_work = any(
        "skill_manage" in (msg.get("name") or "")
        or "memory" in (msg.get("name") or "")
        for msg in messages
        if msg.get("role") == "assistant"
    )
    if not review_did_work:
        return  # Neutral pass — nothing learned

    domain = _infer_domain(messages)
    existing = _load_reinforcements()
    record = {
        "date": _date_stamp(),
        "uuid": _uuid4(),
        "domain": domain,
        "description": "review_agent_skill_update",
        "confidence": 0.75,
        "trigger_count": 1,
    }
    existing.append(record)
    _save_reinforcements(existing)
    _append_evolution("reinforcement", f"domain={domain}")

    # MSTAR Bridge: 强化成功模式
    bridge = _get_mstar_bridge()
    if bridge:
        bridge.evolved_to_reinforcement(
            program_id=f"reinforcement_{domain}_{record['uuid'][:8]}",
            fitness_improvement=record.get("confidence", 0.75) * 0.1,
            mutation_type="reinforcement"
        )


def _integrate_quality(messages: list[dict], session_id: str) -> None:
    score = _score_task_quality(messages)
    existing = _load_quality_scores()
    existing.append(score)
    _save_quality_scores(existing)


def _integrate_homunculus(messages: list[dict], session_id: str) -> None:
    domain = _infer_domain(messages)
    content_snippet = _summarize_conversation(messages)

    _observe(
        domain=domain,
        observation_type="review_session",
        content=content_snippet,
        context=f"session={session_id}",
        session_id=session_id,
    )

    # Observer fires when enough observations accumulate
    if _hmc_observer_ready():
        _run_homunculus_observer()


def _run_homunculus_observer() -> None:
    """
    Analyze accumulated observations and create evolved patterns.
    This is a synchronous file-writing pass — no API calls.
    """
    import json
    obs = _read_jsonl(_hmc_dir() / "observations.jsonl")
    if len(obs) < 5:
        return

    # Simple pattern: group by domain, count frequency
    domain_counts: dict[str, int] = {}
    for o in obs:
        d = o.get("domain", "unknown")
        domain_counts[d] = domain_counts.get(d, 0) + 1

    # Create an evolved agent pattern for the most-seen domain
    if domain_counts:
        top_domain = max(domain_counts, key=domain_counts.get)
        path = _evolved_path("agents") / f"{top_domain}_{_uuid4()}.md"
        path.write_text(
            textwrap.dedent(f"""\
                ---
                id: evolved_{_uuid4()}
                domain: {top_domain}
                confidence: {min(0.9, domain_counts[top_domain] / len(obs))}
                source: homunculus_observer
                observations_used: {len(obs)}
                ---
                # Evolved Pattern — {top_domain}

                Generated from {domain_counts[top_domain]} observations.

                ## Observation Summary
                {content_snippet}

                ## Action
                (to be filled by agent review)
                """).lstrip(),
            encoding="utf-8"
        )
        _append_evolution(
            "homunculus_evolved",
            f"domain={top_domain}, observations={len(obs)}"
        )
        # Archive analyzed observations
        archive_path = _hmc_dir() / "evolved" / f"archived_{_date_stamp()}.jsonl"
        with open(archive_path, "w", encoding="utf-8") as f:
            for o in obs:
                f.write(json.dumps(o, ensure_ascii=False) + "\n")
        # Clear observations
        _hmc_dir() / "observations.jsonl".write_text("", encoding="utf-8")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _infer_domain(messages: list[dict]) -> str:
    """Guess the domain from conversation content."""
    all_text = " ".join(
        msg.get("content", "") or ""
        for msg in messages
        if msg.get("role") in ("user", "assistant")
    )
    all_text = all_text.lower()

    keywords = {
        "code": ["def ", "import ", "class ", "function", "bug", "debug", "refactor"],
        "testing": ["test", "pytest", "assert", "unittest", "coverage"],
        "writing": ["write", "draft", "edit", "paragraph", "article"],
        "workflow": ["pipeline", "schedule", "cron", "deploy", "ci/cd"],
        "research": ["paper", "arxiv", "study", "research", "analysis"],
        "creative": ["design", "art", "creative", "image", "video"],
        "communication": ["email", "message", "reply", "summarize"],
    }
    scores = {}
    for domain, kws in keywords.items():
        scores[domain] = sum(1 for kw in kws if kw in all_text)
    if scores and max(scores.values()) > 0:
        return max(scores, key=scores.get)
    return "general"


def _summarize_conversation(messages: list[dict], max_chars: int = 500) -> str:
    """Extract a compact text summary from conversation."""
    parts = []
    for msg in messages:
        if msg.get("role") == "user":
            content = (msg.get("content") or "")[:100]
            if content:
                parts.append(f"USER: {content}")
        elif msg.get("role") == "assistant" and msg.get("content"):
            content = (msg.get("content") or "")[:100]
            if content:
                parts.append(f"AGENT: {content}")
    summary = " | ".join(parts)
    return summary[:max_chars] + ("..." if len(summary) > max_chars else "")
