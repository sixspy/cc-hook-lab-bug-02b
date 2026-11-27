"""流水页缓存旧态读轨审计。"""
from __future__ import annotations

from app.services import cache as core
from app.services import cache_cutover as cutover
from app.services import cache_dualwrite as dual


def audit_summary() -> dict:
    reconcile = core.reconcile_display()
    core_board = core.dashboard()
    cutover_board = cutover.board()
    dual_board = dual.dual_board()
    half = core.half_fix_display()
    return {
        "title": "流水页缓存旧态审计",
        "reconcile": reconcile,
        "对账": reconcile,
        "core": core_board,
        "cutover": cutover_board,
        "dualwrite": dual_board,
        "half": half,
        "aligned": bool(core_board.get("aligned")),
    }


audit_summary_zh = audit_summary
审计汇总 = audit_summary
audit = audit_summary
