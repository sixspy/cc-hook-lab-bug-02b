"""流水页缓存旧态切流账册：把过期快照锁进账册，并拒回刷 stale-fp 旧票。"""
from __future__ import annotations

import json

from app.db import 连接
from app.services.时间 import 此刻
from app.services import cache as core

_SLUG = "cache"
_TITLE = "流水页缓存旧态"
_OK = "缓存已亮"


def append_cutover(ref_id: int, note: str = "") -> dict:
    ref_id = int(ref_id or 1)
    conn = 连接()
    try:
        core._ensure(conn)
        core._set(conn, f"{_SLUG}_preferred_form", core._CUTOVER)
        core._set(conn, f"{_SLUG}_cutover_note_{ref_id}", note or _TITLE)
        conn.commit()
    finally:
        conn.close()

    rep = core.reconcile_display(form=core._CUTOVER)
    ticket = rep["cutover_fp"]
    ledger = {
        "ref_id": ref_id,
        "note": note or _TITLE,
        "ticket": ticket,
        "rejected_ticket": "stale-fp",
        "reject_refresh": True,
        "时刻": 此刻(),
        "当前尝试号": rep.get("当前尝试号"),
        "当前状态": rep.get("当前状态"),
    }

    conn = 连接()
    try:
        core._ensure(conn)
        core._set(conn, f"{_SLUG}_cutover_note_{ref_id}", note or _TITLE)
        core._set(conn, f"{_SLUG}_cutover_ledger_{ref_id}", json.dumps(ledger, ensure_ascii=False))
        core._set(conn, f"{_SLUG}_panel_chip", _OK)
        conn.commit()
    finally:
        conn.close()

    return {
        "ok": True,
        "aligned": rep["aligned"],
        "账实一致": rep["账实一致"],
        "ref_id": ref_id,
        "note": note,
        "ticket": ticket,
        "ledger": ledger,
        "panel": core.panel_chip(),
        "truth": core.truth_flag(),
        "active_read": core.active_read_ref(),
        "cutover_fp": core.cutover_fp(),
        "reject_refresh": core.reject_refresh(),
        "当前尝试号": rep.get("当前尝试号"),
        "当前状态": rep.get("当前状态"),
    }


def verify_ticket(ticket: str) -> bool:
    current = core.cutover_fp()
    return bool(ticket) and ticket not in ("stale-fp",) and ticket == current


def accept_refresh(ticket: str) -> bool:
    return verify_ticket(ticket)


def reject_old_ticket(ticket: str) -> bool:
    return not verify_ticket(ticket)


def reject_refresh(ticket: str | None = None):
    """无参时返回全局拒刷开关；带票时判断该票是否应被切流侧拒绝。"""
    if ticket is None:
        return core.reject_refresh()
    return reject_old_ticket(ticket)


def reject_stale_ticket(ticket: str) -> bool:
    return reject_old_ticket(ticket)


def list_ledgers() -> list[dict]:
    conn = 连接()
    try:
        core._ensure(conn)
        rows = conn.execute(
            "SELECT 键,值 FROM 投递元 WHERE 键 LIKE ? ORDER BY 键",
            (f"{_SLUG}_cutover_ledger_%",),
        ).fetchall()
        ledgers = []
        for row in rows:
            try:
                item = json.loads(row["值"])
            except Exception:
                item = {"raw": row["值"]}
            if isinstance(item, dict):
                item.setdefault("key", row["键"])
                ledgers.append(item)
        return ledgers
    finally:
        conn.close()


# 业务语义词别名。
验票 = verify_ticket
接受刷新 = accept_refresh
拒回刷旧票 = reject_old_ticket
拒回刷 = reject_old_ticket
拒绝回刷 = reject_old_ticket
拒绝刷新 = reject_refresh
切流账册列表 = list_ledgers


def board() -> dict:
    # 切流看板按切流账册形态核票；不能因全局上次选择了 live_flag 就把本板读轨报错。
    dash = core.reconcile_display(form=core._CUTOVER)
    dash = {**dash, **core.dashboard()}
    ledgers = list_ledgers()
    latest = None
    if ledgers:
        latest = sorted(ledgers, key=lambda item: int(item.get("ref_id") or 0))[-1]
    dash["fate"] = "cutover_book"
    dash["active_read"] = core._CUTOVER
    dash["ledgers"] = ledgers
    dash["ledger"] = latest
    dash["ledger_count"] = len(ledgers)
    dash["panel_pack_mismatch"] = not dash["aligned"]
    dash["verify_ticket"] = verify_ticket(dash.get("cutover_fp", ""))
    return dash
