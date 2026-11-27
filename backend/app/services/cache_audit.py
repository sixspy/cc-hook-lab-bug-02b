"""流水页缓存旧态读轨审计。"""
from __future__ import annotations

from app.services import cache as core
from app.services import cache_cutover as cutover
from app.services import cache_dualwrite as dual


def audit_summary() -> dict:
    return {
        "title": "流水页缓存旧态审计",
        "core": core.dashboard(),
        "cutover": cutover.board(),
        "dualwrite": dual.dual_board(),
        "half": core.half_fix_display(),
    }
