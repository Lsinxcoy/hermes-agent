"""
Skill PRM Router — SkillClaw PRM 预测 → 任务路由优化

在 skill_commands.py 的 build_skill_invocation_message 之前调用，
根据 SkillClaw 的 effectiveness 历史预测最佳 skill，
并在低效时给出替代建议。

用法（导入到 skill_commands.py）：
    from agent.skill_prm_router import get_skill_with_prm_score
    skill_info, prm_score = get_skill_with_prm_score(cmd_key, user_instruction)
"""
import json
import sqlite3
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

DASHBOARD_DB = Path.home() / ".skillclaw" / "dashboard.db"
EFFECTIVENESS_CACHE_TTL = 3600  # 1 hour cache


class PRMRouter:
    """
    基于 SkillClaw effectiveness 的 skill 路由器

    当 effectiveness 数据可用时：
    - 高分 skill (≥0.7): 无条件使用
    - 中等 (0.5-0.7): 使用但记录
    - 低分 (0.35-0.5): 警告 + 记录
    - 极低 (<0.35): 建议替代 skill
    """

    _cache: Dict[str, Tuple[float, float]] = {}  # skill_name -> (eff, cached_at)
    _cache_ttl = EFFECTIVENESS_CACHE_TTL

    def get_effectiveness(self, skill_name: str) -> Optional[float]:
        """从 SkillClaw DB 读取 skill 的 effectiveness"""
        import time
        now = time.time()

        # 读缓存
        if skill_name in self._cache:
            eff, cached_at = self._cache[skill_name]
            if now - cached_at < self._cache_ttl:
                return eff

        if not DASHBOARD_DB.exists():
            return None

        try:
            conn = sqlite3.connect(str(DASHBOARD_DB))
            row = conn.execute(
                "SELECT effectiveness FROM skills WHERE name = ?",
                (skill_name,)
            ).fetchone()
            conn.close()

            if row and row[0] is not None:
                eff = float(row[0])
                self._cache[skill_name] = (eff, now)
                return eff
        except Exception as e:
            logger.debug(f"PRM router DB error: {e}")

        return None

    def get_skill_rankings(self) -> list:
        """返回所有 skill 的 effectiveness 排名（从高到低）"""
        if not DASHBOARD_DB.exists():
            return []

        try:
            conn = sqlite3.connect(str(DASHBOARD_DB))
            rows = conn.execute(
                "SELECT name, effectiveness, positive_count, negative_count FROM skills "
                "WHERE effectiveness IS NOT NULL ORDER BY effectiveness DESC"
            ).fetchall()
            conn.close()
            return [
                {"name": r[0], "eff": r[1], "pos": r[2], "neg": r[3]}
                for r in rows
            ]
        except Exception:
            return []

    def find_alternative(self, skill_name: str, category: Optional[str] = None) -> Optional[dict]:
        """
        找到同类型的高分替代 skill
        """
        rankings = self.get_skill_rankings()
        if not rankings:
            return None

        # 找 effectiveness 最高的同 category skill
        best = None
        for r in rankings:
            if r["name"] != skill_name and r["eff"] >= 0.6:
                if best is None or r["eff"] > best["eff"]:
                    best = r

        return best

    def get_prm_verdict(self, skill_name: str) -> Dict[str, Any]:
        """
        返回 skill 的 PRM 评估结果

        Returns:
            {
                "skill": skill_name,
                "effectiveness": float | None,
                "verdict": "use" | "warn" | "avoid" | "no_data",
                "message": str,
                "alternative": dict | None,
                "confidence": float,
            }
        """
        eff = self.get_effectiveness(skill_name)

        if eff is None:
            return {
                "skill": skill_name,
                "effectiveness": None,
                "verdict": "no_data",
                "message": f"No PRM data for '{skill_name}'",
                "alternative": None,
                "confidence": 0.0,
            }

        if eff >= 0.7:
            return {
                "skill": skill_name,
                "effectiveness": eff,
                "verdict": "use",
                "message": f"PRM effectiveness {eff:.2f} — recommended",
                "alternative": None,
                "confidence": 0.8,
            }
        elif eff >= 0.5:
            alt = self.find_alternative(skill_name)
            return {
                "skill": skill_name,
                "effectiveness": eff,
                "verdict": "warn",
                "message": f"PRM effectiveness {eff:.2f} — moderate, consider alternatives",
                "alternative": alt,
                "confidence": 0.6,
            }
        elif eff >= 0.35:
            alt = self.find_alternative(skill_name)
            return {
                "skill": skill_name,
                "effectiveness": eff,
                "verdict": "warn",
                "message": f"PRM effectiveness {eff:.2f} — low, consider alternatives",
                "alternative": alt,
                "confidence": 0.7,
            }
        else:
            alt = self.find_alternative(skill_name)
            return {
                "skill": skill_name,
                "effectiveness": eff,
                "verdict": "avoid",
                "message": f"PRM effectiveness {eff:.2f} — high risk of poor results",
                "alternative": alt,
                "confidence": 0.85,
            }


# Global singleton
_prm_router: Optional[PRMRouter] = None


def get_prm_router() -> PRMRouter:
    global _prm_router
    if _prm_router is None:
        _prm_router = PRMRouter()
    return _prm_router


def get_skill_with_prm_score(
    cmd_key: str,
    user_instruction: str = "",
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    获取 skill 信息 + PRM 评估

    集成点：在 skill_commands.build_skill_invocation_message 之前调用

    Returns:
        (skill_info, prm_verdict) — prm_verdict 包含路由决策
    """
    from agent.skill_commands import get_skill_commands

    commands = get_skill_commands()
    skill_info = commands.get(cmd_key)

    if not skill_info:
        return None, None

    router = get_prm_router()
    skill_name = skill_info.get("name", cmd_key.lstrip("/"))
    verdict = router.get_prm_verdict(skill_name)

    # 记录 PRM 决策到日志（debug 级别）
    if verdict["verdict"] in ("warn", "avoid"):
        logger.warning(
            f"PRM router: {skill_name} verdict={verdict['verdict']} "
            f"eff={verdict['effectiveness']} "
            f"alt={verdict.get('alternative', {}).get('name')}"
        )

    return skill_info, verdict


# ── 在 skill_commands.py 中集成的代码片段 ────────────────────────────
# 在 agent/skill_commands.py 的 build_skill_invocation_message 函数开头添加：
#
# from agent.skill_prm_router import get_skill_with_prm_score
#
# # 在 skill_info = commands.get(cmd_key) 之后：
# skill_info, prm_verdict = get_skill_with_prm_score(cmd_key, user_instruction)
# if prm_verdict and prm_verdict["verdict"] == "avoid":
#     # 记录低效使用，但不阻止（skill 可能由用户显式调用）
#     pass
