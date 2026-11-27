"""流水页缓存旧态双写两轨：旧成败归档，新读集合挂 live_flag 对外。"""
from __future__ import annotations

import json

from app.db import 连接
from app.services.时间 import 此刻
from app.services import cache as core

_SLUG = "cache"
_TITLE = "流水页缓存旧态"
_OK = "缓存已亮"


def apply_dualwrite(note: str = "") -> dict:
    conn = 连接()
    old_track = None
    try:
        core._ensure(conn)
        core._set(conn, f"{_SLUG}_preferred_form", core._LIVE)
        core._set(conn, f"{_SLUG}_dual_chip", _OK)
        core._set(conn, f"{_SLUG}_dual_note", note or _TITLE)
        shadow_rows, shadow_fp = core._memory_shadow()
        if shadow_rows:
            old_track = {
                "fingerprint": shadow_fp,
                "top": shadow_rows[0],
                "rows": len(shadow_rows),
            }
            core._set(
                conn,
                f"{_SLUG}_dual_old_track",
                json.dumps(old_track, ensure_ascii=False, default=str),
            )
        conn.commit()
    finally:
        conn.close()

    rep = core.reconcile_display(form=core._LIVE)

    conn = 连接()
    try:
        core._ensure(conn)
        new_track = {
            "fingerprint": rep["truth_fingerprint"],
            "read_set": "投递尝试",
            "read_ref": rep["read_ref"],
            "current": rep.get("current"),
            "时刻": 此刻(),
        }
        core._set(conn, f"{_SLUG}_dual_truth", _OK)
        core._set(conn, f"{_SLUG}_dual_new_track", json.dumps(new_track, ensure_ascii=False, default=str))
        core._set(conn, f"{_SLUG}_panel_chip", _OK)
        conn.commit()
    finally:
        conn.close()

    core.paint_ok()
    return {
        "ok": True,
        "aligned": rep["aligned"],
        "账实一致": rep["账实一致"],
        "dual_chip": _OK,
        "truth": core.truth_flag(),
        "active_read": core.active_read_ref(),
        "archive_count": len(core.archive_rows()),
        "live_flag": core.live_flag(),
        "old_track": old_track,
        "new_track": new_track,
        "当前尝试号": rep.get("当前尝试号"),
        "当前状态": rep.get("当前状态"),
    }


def dual_board() -> dict:
    applied = apply_dualwrite()
    dash = core.dashboard()
    return {
        **applied,
        "archive": core.archive_rows(),
        "dash": dash,
        "fate": "dualwrite",
        "panel_pack_mismatch": not dash["aligned"],
        "title": f"{_TITLE}双写面",
    }
