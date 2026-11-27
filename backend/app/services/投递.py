from __future__ import annotations
import hashlib
import hmac
import json
import threading
import uuid
from app.db import 连接
from app.services.时间 import 此刻
from app.services import 签名目录

_流水锁 = threading.RLock()
# 仅保留上一轮读集合用于识别/归档过期影子；它永远不能覆盖本轮从库里读出的 rows。
_流水快照 = {"行": None, "指纹": ""}


def _流水指纹(rows: list | None) -> str:
    payload = []
    for row in rows or []:
        get = row.get if isinstance(row, dict) else lambda key, default=None: row[key] if key in row.keys() else default
        payload.append({
            "尝试号": get("尝试号"),
            "事件号": get("事件号"),
            "状态": get("状态"),
            "响应码": get("响应码"),
            "投递时": get("投递时"),
        })
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def 清流水快照() -> None:
    with _流水锁:
        _流水快照["行"] = None
        _流水快照["指纹"] = ""


# 常见缓存语义别名。
清缓存 = 清流水快照
失效流水缓存 = 清流水快照


def _签名(密钥版本: str, 事件号: str, 摘要: str) -> str:
    raw = f"{密钥版本}|{事件号}|{摘要}".encode("utf-8")
    key = f"hook-secret-{密钥版本}".encode("utf-8")
    return hmac.new(key, raw, hashlib.sha256).hexdigest()[:24]


def 投递一次(事件号: str, 强制失败: bool = False) -> dict:
    """对事件执行一次签名投递。本地模拟：默认成功；强制失败或端点停用则记失败。"""
    t = 此刻()
    尝试号 = f"TRY-{uuid.uuid4().hex[:10]}"
    conn = 连接()
    try:
        ev = conn.execute("SELECT * FROM 事件 WHERE 事件号=?", (事件号,)).fetchone()
        if not ev:
            raise ValueError("无此事件")
        if ev["状态"] in ("已投递", "死信"):
            raise ValueError(f"事件状态为{ev['状态']}，不可再投")
        ep = conn.execute("SELECT * FROM 投递端点 WHERE 端点键=?", (ev["端点键"],)).fetchone()
        if not ep:
            raise ValueError("端点缺失")
        if int(ep["启用"]) != 1:
            raise ValueError("端点停用")
        规 = 签名目录.签名规()
        最大 = int(规.get("最大重试") or 3)
        sig = _签名(ep["密钥版本"], 事件号, ev["载荷摘要"])
        失败次数 = conn.execute(
            "SELECT COUNT(*) AS c FROM 投递尝试 WHERE 事件号=? AND 状态='失败'", (事件号,)
        ).fetchone()["c"]
        成功 = (not 强制失败) and (失败次数 < 1000)
        # 模拟：事件类型含 .fail 或 body 强制 → 失败
        if 强制失败 or str(ev["事件类型"]).endswith(".fail"):
            成功 = False
        if 成功:
            conn.execute(
                "INSERT INTO 投递尝试(尝试号,事件号,端点键,签名摘要,状态,响应码,说明,投递时) VALUES (?,?,?,?,?,?,?,?)",
                (尝试号, 事件号, ev["端点键"], sig, "成功", 200, "投递成功", t),
            )
            conn.execute("UPDATE 事件 SET 状态=?, 更新时=? WHERE 事件号=?", ("已投递", t, 事件号))
            conn.execute("INSERT INTO 操作留痕(时刻,操作,事件号,详情) VALUES (?,?,?,?)",
                         (t, "投递成功", 事件号, 尝试号))
            conn.commit()
            return {"尝试号": 尝试号, "状态": "成功", "事件状态": "已投递", "签名摘要": sig}
        # 失败
        conn.execute(
            "INSERT INTO 投递尝试(尝试号,事件号,端点键,签名摘要,状态,响应码,说明,投递时) VALUES (?,?,?,?,?,?,?,?)",
            (尝试号, 事件号, ev["端点键"], sig, "失败", 500, "投递失败", t),
        )
        新失败 = 失败次数 + 1
        if 新失败 >= 最大:
            dl = f"DL-{uuid.uuid4().hex[:10]}"
            if not conn.execute("SELECT 1 FROM 死信 WHERE 事件号=?", (事件号,)).fetchone():
                conn.execute(
                    "INSERT INTO 死信(死信号,事件号,端点键,失败次数,末次原因,状态,入匣时) VALUES (?,?,?,?,?,?,?)",
                    (dl, 事件号, ev["端点键"], 新失败, "重试耗尽", "待处置", t),
                )
            conn.execute("UPDATE 事件 SET 状态=?, 更新时=? WHERE 事件号=?", ("死信", t, 事件号))
            事件状态 = "死信"
        else:
            conn.execute("UPDATE 事件 SET 状态=?, 更新时=? WHERE 事件号=?", ("投递中", t, 事件号))
            事件状态 = "投递中"
        conn.execute("INSERT INTO 操作留痕(时刻,操作,事件号,详情) VALUES (?,?,?,?)",
                     (t, "投递失败", 事件号, f"失败{新失败}/{最大}"))
        conn.commit()
        return {"尝试号": 尝试号, "状态": "失败", "事件状态": 事件状态, "失败次数": 新失败, "签名摘要": sig}
    finally:
        conn.close()

def 读流水或旧(rows: list) -> list:
    """把本轮 SQL 读集合作为唯一当前票；旧快照只归档，不再回刷列表。"""
    fresh = [dict(x) for x in rows]
    fresh_fp = _流水指纹(fresh)
    with _流水锁:
        if not isinstance(_流水快照, dict):
            globals()["_流水快照"] = {"行": None, "指纹": ""}
        old = _流水快照.get("行")
        old_fp = _流水快照.get("指纹") or _流水指纹(old)
        if old is not None and old_fp and old_fp != fresh_fp:
            # 延迟导入避免服务层初始化环。旧账失败不能把“读库成功”拖成 500。
            try:
                from app.services import cache as _cache
                _cache.archive_snapshot(old, old_fp=old_fp, new_fp=fresh_fp)
            except Exception:
                pass
        _流水快照["行"] = fresh
        _流水快照["指纹"] = fresh_fp
        return [dict(x) for x in fresh]
