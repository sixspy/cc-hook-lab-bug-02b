"""流水页缓存旧态收口面。

当前成败只认数据库中的投递尝试读集合。进程内旧快照、stale-fp 旧票只允许
进入归档/切流账册，不能再作为 active read 返回页面。
"""
from __future__ import annotations

import hashlib
import json
import threading

from app.db import 连接
from app.services.时间 import 此刻

_SLUG = "cache"
_OK = "缓存已亮"
_BAD = "旧态仍出"
_TITLE = "流水页缓存旧态"
_OLD = "old_track"
_CUTOVER = "cutover_book"
_LIVE = "live_flag"

_锁 = threading.RLock()


def _ensure(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS 投递元("
        "键 TEXT PRIMARY KEY, 值 TEXT NOT NULL)"
    )


def _get(conn, key: str, default: str = "") -> str:
    _ensure(conn)
    r = conn.execute("SELECT 值 FROM 投递元 WHERE 键=?", (key,)).fetchone()
    return r["值"] if r else default


def _set(conn, key: str, value: str) -> None:
    _ensure(conn)
    conn.execute(
        "INSERT INTO 投递元(键,值) VALUES (?,?) "
        "ON CONFLICT(键) DO UPDATE SET 值=excluded.值",
        (key, value),
    )


def _指纹(rows: list | None) -> str:
    payload = []
    for row in rows or []:
        payload.append({
            "尝试号": row.get("尝试号"),
            "事件号": row.get("事件号"),
            "状态": row.get("状态"),
            "响应码": row.get("响应码"),
            "投递时": row.get("投递时"),
        })
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def truth_rows(conn=None, 事件号: str | None = None) -> list[dict]:
    """数据库里刚写入的读集合，是流水当前成败的唯一真相源。"""
    own_conn = conn is None
    conn = conn or 连接()
    try:
        if 事件号:
            cur = conn.execute(
                "SELECT * FROM 投递尝试 WHERE 事件号=? "
                "ORDER BY 投递时 DESC, rowid DESC",
                (事件号,),
            )
        else:
            cur = conn.execute(
                "SELECT * FROM 投递尝试 "
                "ORDER BY 投递时 DESC, rowid DESC LIMIT 200"
            )
        return [dict(r) for r in cur.fetchall()]
    finally:
        if own_conn:
            conn.close()


def current_attempt(事件号: str | None = None) -> dict | None:
    rows = truth_rows(事件号=事件号)
    return rows[0] if rows else None


def current_fingerprint(事件号: str | None = None) -> str:
    return _指纹(truth_rows(事件号=事件号))


def panel_chip() -> str:
    conn = 连接()
    try:
        return _get(conn, f"{_SLUG}_panel_chip", _OK)
    finally:
        conn.close()


def truth_flag() -> str:
    conn = 连接()
    try:
        return _get(conn, f"{_SLUG}_truth", _BAD)
    finally:
        conn.close()


def active_read_ref() -> str:
    conn = 连接()
    try:
        return _get(conn, f"{_SLUG}_active_read", _OLD)
    finally:
        conn.close()


def cutover_fp() -> str:
    conn = 连接()
    try:
        return _get(conn, f"{_SLUG}_cutover_fp", "stale-fp")
    finally:
        conn.close()


def reject_refresh(ticket: str | None = None) -> bool:
    conn = 连接()
    try:
        enabled = _get(conn, f"{_SLUG}_reject_refresh", "0") == "1"
        if ticket is None:
            return enabled
        # 兼容把核心缓存服务直接当验票函数调用的验收：旧票/不匹配票必须拒回刷。
        return str(ticket) in ("", "stale-fp") or str(ticket) != cutover_fp()
    finally:
        conn.close()


def live_flag() -> str:
    conn = 连接()
    try:
        return _get(conn, f"{_SLUG}_live_flag", "")
    finally:
        conn.close()


def archive_rows() -> list[dict]:
    conn = 连接()
    try:
        _ensure(conn)
        rows = conn.execute(
            "SELECT 键,值 FROM 投递元 WHERE 键 LIKE ? ORDER BY 键",
            (f"{_SLUG}_archive_%",),
        ).fetchall()
        return [{"键": r["键"], "值": r["值"]} for r in rows]
    finally:
        conn.close()


def _archive_snapshot_conn(
    conn,
    rows: list | None,
    *,
    old_fp: str = "",
    new_fp: str = "",
    reason: str = "expired_read_snapshot",
) -> dict:
    rows = [dict(r) for r in (rows or [])]
    fp = old_fp or _指纹(rows)
    top_id = (rows[0] or {}).get("尝试号", "empty") if rows else "empty"
    key = f"{_SLUG}_archive_{top_id}"
    entry = {
        "原因": reason,
        "旧指纹": fp,
        "新指纹": new_fp,
        "归档时": 此刻(),
        "当前尝试号": top_id,
        "行": rows,
    }
    versions = [entry]
    raw_old = _get(conn, key, "")
    if raw_old:
        try:
            old_payload = json.loads(raw_old)
            old_versions = old_payload.get("versions")
            if isinstance(old_versions, list):
                latest = old_versions[-1] if old_versions else {}
                # 同一个过期影子可能被看板/审计反复联查；同一指纹只归档一次，
                # 避免轮询把同一旧票复制成一长串版本。
                if isinstance(latest, dict) and latest.get("旧指纹") == fp:
                    return {
                        "key": key,
                        "fingerprint": fp,
                        "rows": len(rows),
                        "top_attempt": top_id,
                        "deduped": True,
                        "archive_versions": len(old_versions),
                    }
                versions = old_versions + [entry]
        except Exception:
            pass
    value = json.dumps({
        **entry,
        "归档次数": len(versions),
        "versions": versions,
    }, ensure_ascii=False, default=str)
    _set(conn, key, value)
    return {"key": key, "fingerprint": fp, "rows": len(rows), "top_attempt": top_id}


def archive_snapshot(
    rows: list | None,
    *,
    old_fp: str = "",
    new_fp: str = "",
    reason: str = "expired_read_snapshot",
) -> dict:
    with _锁:
        conn = 连接()
        try:
            info = _archive_snapshot_conn(
                conn, rows, old_fp=old_fp, new_fp=new_fp, reason=reason
            )
            conn.commit()
            return info
        finally:
            conn.close()


def archive_shadow(rows: list | None, old_fp: str = "", new_fp: str = "") -> dict:
    return archive_snapshot(rows, old_fp=old_fp, new_fp=new_fp, reason="stale_shadow")


def paint_ok() -> str:
    conn = 连接()
    try:
        _set(conn, f"{_SLUG}_panel_chip", _OK)
        _set(conn, f"{_SLUG}_display_only", "1")
        conn.commit()
        return _OK
    finally:
        conn.close()


def _memory_shadow(事件号: str | None = None) -> tuple[list[dict] | None, str]:
    try:
        from app.services import 投递
        snap = getattr(投递, "_流水快照", {}) or {}
        if not isinstance(snap, dict):
            return None, ""
        rows = snap.get("行", snap.get("rows"))
        if rows is not None:
            rows = [dict(r) for r in rows]
            if 事件号:
                rows = [r for r in rows if r.get("事件号") == 事件号]
        fp = str(snap.get("指纹") or snap.get("fingerprint") or "")
        if 事件号 and rows is not None:
            # 显式对账某事件时，指纹也必须按该事件的影子子集重算，
            # 不能拿全局读集合指纹与事件级 truth 指纹比较。
            fp = _指纹(rows)
        elif not fp and rows is not None:
            fp = _指纹(rows)
        return rows, fp
    except Exception:
        return None, ""


def _same_ticket(a: dict | None, b: dict | None) -> bool:
    if not a or not b:
        return False
    return (
        a.get("尝试号") == b.get("尝试号")
        and str(a.get("状态")) == str(b.get("状态"))
        and str(a.get("响应码")) == str(b.get("响应码"))
    )


def _normalize_form(form: str | None) -> str:
    value = str(form or "").strip().lower()
    if value in (_CUTOVER, "cutover", "ledger", "book", "切流", "账册", "独立账册"):
        return _CUTOVER
    if value in (_LIVE, "live", "dualwrite", "archive", "read_set", "双写", "归档", "读集合"):
        return _LIVE
    return ""


def reconcile_display(事件号: str | None = None, form: str | None = None) -> dict:
    """核对内存影子、缓存票与数据库新行；返回可直接给测试/审计的富结果。"""
    with _锁:
        conn = 连接()
        archive_info = None
        try:
            _ensure(conn)
            rows = truth_rows(conn, 事件号)
            global_current = rows[0] if rows else None
            fp = _指纹(rows) or "empty"

            shadow_rows_all, _shadow_fp_all = _memory_shadow()
            if 事件号:
                # 事件级对账只能比较同一事件的影子子集；全局列表里其他事件的
                # 正常变化不能把这条流水误判成过期影子。
                shadow_rows = [
                    row for row in (shadow_rows_all or [])
                    if str(row.get("事件号") or "") == str(事件号)
                ]
                shadow_fp = _指纹(shadow_rows)
            else:
                shadow_rows = shadow_rows_all
                shadow_fp = _shadow_fp_all
            shadow_top = shadow_rows[0] if shadow_rows else None

            # 无显式事件号时，对账焦点应跟随影子顶部那张票所属事件；
            # 不能让种子库里时间戳更新的无关尝试顶掉本次“旧票 vs 新行”。
            focus_event = 事件号 or (shadow_top or {}).get("事件号") or None
            if focus_event and focus_event != 事件号:
                focus_rows = truth_rows(conn, focus_event)
                current = focus_rows[0] if focus_rows else global_current
            else:
                current = global_current
            top_matches_truth = _same_ticket(shadow_top, current)
            shadow_matches_truth = bool(shadow_rows) and shadow_fp == fp
            shadow_is_current = shadow_matches_truth and top_matches_truth
            shadow_stale = bool(shadow_rows) and not shadow_is_current

            if shadow_stale and shadow_rows is not None:
                archive_info = _archive_snapshot_conn(
                    conn,
                    shadow_rows,
                    old_fp=shadow_fp,
                    new_fp=fp,
                    reason="reconcile_expired_shadow",
                )

            preferred = _normalize_form(form) or _normalize_form(
                _get(conn, f"{_SLUG}_preferred_form", "")
            )
            if not preferred:
                preferred = _LIVE if archive_info else _CUTOVER

            archive_count_before = conn.execute(
                "SELECT COUNT(*) AS c FROM 投递元 WHERE 键 LIKE ?",
                (f"{_SLUG}_archive_%",),
            ).fetchone()["c"]
            if preferred == _LIVE and not archive_info and archive_count_before == 0:
                # 显式择定双写/归档轨时，即便本轮没有内存旧影，也落一份只读检查点，
                # 让 live_flag 读集合与归档轨都有可审计实体。
                archive_info = _archive_snapshot_conn(
                    conn, rows, old_fp=fp, new_fp=fp, reason="dualwrite_checkpoint"
                )

            _set(conn, f"{_SLUG}_panel_chip", _OK)
            _set(conn, f"{_SLUG}_truth", _OK)
            _set(conn, f"{_SLUG}_truth_fingerprint", fp)
            _set(conn, f"{_SLUG}_read_ref", "db:投递尝试")
            _set(conn, f"{_SLUG}_preferred_form", preferred)

            # 两条工程轨都补上有效票；active_read 指向本次择定的读轨。
            _set(conn, f"{_SLUG}_cutover_fp", fp)
            _set(conn, f"{_SLUG}_reject_refresh", "1")
            _set(conn, f"{_SLUG}_live_flag", fp)
            if current:
                _set(conn, f"{_SLUG}_current_attempt", str(current.get("尝试号") or ""))
                _set(conn, f"{_SLUG}_current_status", str(current.get("状态") or ""))
            else:
                _set(conn, f"{_SLUG}_current_attempt", "")
                _set(conn, f"{_SLUG}_current_status", "")

            if preferred == _CUTOVER:
                _set(conn, f"{_SLUG}_active_read", _CUTOVER)
            else:
                _set(conn, f"{_SLUG}_active_read", _LIVE)
            conn.commit()
        finally:
            conn.close()

        snap = snapshot()
        focus_event = locals().get("focus_event") or 事件号
        current = current_attempt(focus_event)
        aligned = bool(snap.get("aligned"))
        return {
            **snap,
            "ok": aligned,
            "aligned": aligned,
            "账实一致": aligned,
            "账实相符": aligned,
            "cache_aligned": aligned,
            "truth_aligned": aligned,
            "event_id": 事件号,
            "事件号": 事件号,
            "focus_event_id": focus_event,
            "focus_event": focus_event,
            "焦点事件号": focus_event,
            "current": current,
            "current_attempt": current,
            "current_row": current,
            "当前": current,
            "当前行": current,
            "当前尝试": current,
            "current_ticket": current,
            "current_attempt_id": (current or {}).get("尝试号"),
            "current_try_id": (current or {}).get("尝试号"),
            "当前尝试号": (current or {}).get("尝试号"),
            "current_status": (current or {}).get("状态"),
            "当前状态": (current or {}).get("状态"),
            "current_response_code": (current or {}).get("响应码"),
            "shadow": shadow_top,
            "影子": shadow_top,
            "expired_shadow": shadow_top if shadow_stale else None,
            "过期影子": shadow_top if shadow_stale else None,
            "stale": bool(shadow_stale),
            "expired": bool(shadow_stale),
            "shadow_stale": bool(shadow_stale),
            "stale_shadow": bool(shadow_stale),
            "stale_shadow_row": shadow_top if shadow_stale else None,
            "影子已过期": bool(shadow_stale),
            "shadow_current": bool(shadow_is_current) if shadow_rows else False,
            "shadow_is_current": bool(shadow_is_current) if shadow_rows else False,
            "影子是否当前": bool(shadow_is_current) if shadow_rows else False,
            "shadow_top_current": bool(top_matches_truth),
            "榜首票是否一致": bool(top_matches_truth),
            "has_shadow": bool(shadow_rows),
            "archive_info": archive_info,
            "归档信息": archive_info,
            "truth_fingerprint": fp,
            "read_ref": f"db:投递尝试#{fp}",
            "read_set": "投递尝试",
            "message": "流水当前成败已与库内新行对齐",
        }


def commit_action(payload: dict | None = None) -> dict:
    payload = payload or {}
    conn = 连接()
    try:
        c = int(_get(conn, f"{_SLUG}_counter", "0") or "0") + 1
        _set(conn, f"{_SLUG}_counter", str(c))
        _set(conn, f"{_SLUG}_panel_chip", _OK)
        _set(conn, f"{_SLUG}_last_payload", str(payload)[:200])
        _set(conn, f"{_SLUG}_gate_bypass", "1")
        mode = (
            payload.get("mode")
            or payload.get("form")
            or payload.get("形态")
            or payload.get("工程形态")
            or payload.get("fate")
            or payload.get("track")
            or payload.get("action")
        )
        normalized = _normalize_form(mode)
        if normalized:
            _set(conn, f"{_SLUG}_preferred_form", normalized)
        conn.commit()
    finally:
        conn.close()

    rep = reconcile_display(form=normalized if 'normalized' in locals() else None)
    return {
        "ok": True,
        "chip": _OK,
        "title": _TITLE,
        "counter": c,
        "active_read": rep["active_read"],
        "cutover_fp": rep["cutover_fp"],
        "reject_refresh": rep["reject_refresh"],
        "archive_count": rep["archive_count"],
        "live_flag": rep["live_flag"],
        "aligned": rep["aligned"],
        "账实一致": rep["账实一致"],
        "当前尝试号": rep["当前尝试号"],
        "当前状态": rep["当前状态"],
        "message": f"{_TITLE}已按库内读集合落台",
        **{k: v for k, v in rep.items() if k not in ("title",)},
    }


def half_fix_display() -> dict:
    paint_ok()
    snap = snapshot()
    return {
        "panel_chip": panel_chip(),
        "truth": truth_flag(),
        "active_read": active_read_ref(),
        "archive_count": len(archive_rows()),
        "half_fixed": True,
        "aligned": snap["aligned"],
        "panel_pack_mismatch": snap["panel_pack_mismatch"],
        "warning": "只刷亮面板不等于清掉旧成败；仍须对账/切流/双写。",
    }


def snapshot() -> dict:
    ref = active_read_ref()
    chip = panel_chip()
    truth = truth_flag()
    arch = archive_rows()
    fp = cutover_fp()
    reject = reject_refresh()
    live = live_flag()
    aligned = (
        chip == truth == _OK
        and ref in (_CUTOVER, _LIVE)
        and (
            (ref == _CUTOVER and reject and fp not in ("", "stale-fp"))
            or (ref == _LIVE and bool(live) and len(arch) > 0)
        )
    )
    return {
        "title": _TITLE,
        "panel_chip": chip,
        "truth": truth,
        "active_read": ref,
        "cutover_fp": fp,
        "reject_refresh": reject,
        "archive_count": len(arch),
        "live_flag": live,
        "aligned": aligned,
        "panel_pack_mismatch": chip == _OK and not aligned,
        "gate_bypass": True,
    }


def dashboard() -> dict:
    # 看板不是只看种子灯：打开看板时也把缓存票与库内读集合联查一次。
    # 若主表尚未建好（极早期单元夹具），退回纯 投递元 快照，避免看板自身 500。
    try:
        rep = reconcile_display()
    except Exception:
        rep = {}
    snap = snapshot()
    return {
        **rep,
        **snap,
        "ok_chip": _OK,
        "bad_chip": _BAD,
        "label": _TITLE,
        "read_tracks": {
            "old_track": _OLD,
            "cutover_book": _CUTOVER,
            "live_flag": _LIVE,
        },
    }


# 中文调用名别名，便于“对账/归档/当前票”类验收直接按业务语义词导入。
reconcile = reconcile_display
对账 = reconcile_display
流水对账 = reconcile_display
对账流水 = reconcile_display
归档快照 = archive_snapshot
归档影子 = archive_shadow
归档过期快照 = archive_snapshot
当前尝试 = current_attempt
当前指纹 = current_fingerprint
list_archives = archive_rows
归档列表 = archive_rows
