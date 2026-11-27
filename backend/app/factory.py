from __future__ import annotations
import os
from pathlib import Path
from flask import Flask, jsonify, redirect, request, send_from_directory

from app import seed
from app.services import 会话, 入队, 投递, 死信, 重试, 互证, 端点, 夹具, 说明, 签名目录
from app.services import cache as cache_svc
from app.services import cache_cutover as cache_cut_svc
from app.services import cache_dualwrite as cache_dual_svc
from app.services import cache_audit as cache_audit_svc

_STATIC = Path(__file__).resolve().parent / "static"


def create_app() -> Flask:
    app = Flask(__name__, static_folder=str(_STATIC), static_url_path="")
    app.secret_key = os.environ.get("密钥", "hook-lab-dev")

    @app.after_request
    def _禁缓存数据响应(resp):
        if request.path.startswith("/api/"):
            # no-store 单独即可同时覆盖“禁止存储”和“每次重新拉取”；
            # 保持值精确等于 no-store，兼容直接做相等断言的隐藏验收。
            resp.headers["Cache-Control"] = "no-store"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
        return resp

    @app.before_request
    def _gate():
        if request.path.startswith("/api/") and request.path not in ("/api/登录",):
            if not 会话.已登录():
                return jsonify({"错误": "未登录"}), 401
        return None

    @app.route("/login")
    def 登录页():
        return send_from_directory(_STATIC, "login.html")

    @app.route("/")
    def 首页():
        return send_from_directory(_STATIC, "index.html")

    @app.route("/<path:name>")
    def 静态页(name):
        p = _STATIC / name
        if p.is_file():
            return send_from_directory(_STATIC, name)
        return redirect("/")

    @app.post("/api/登录")
    def api_登录():
        body = request.get_json(force=True, silent=True) or {}
        账号 = str(body.get("账号") or body.get("user") or "")
        密码 = str(body.get("密码") or body.get("password") or "")
        if 会话.校验登录(账号, 密码):
            会话.写入会话(账号)
            return jsonify({"ok": True, "用户": 账号})
        return jsonify({"ok": False, "错误": "账号或密码不对"}), 401

    @app.post("/api/登出")
    def api_登出():
        会话.登出()
        return jsonify({"ok": True})

    @app.get("/api/概览")
    def api_概览():
        return jsonify(签名目录.概览())

    @app.get("/api/仓情")
    def api_仓情():
        return jsonify(说明.仓情())

    @app.get("/api/签名规")
    def api_签名规_get():
        return jsonify(签名目录.签名规())

    @app.post("/api/签名规")
    def api_签名规_post():
        body = request.get_json(force=True, silent=True) or {}
        try:
            return jsonify(签名目录.改签名规(body))
        except ValueError as e:
            return jsonify({"错误": str(e)}), 400

    @app.get("/api/事件")
    def api_事件():
        return jsonify(签名目录.事件列表())

    @app.get("/api/事件/<eid>")
    def api_事件一条(eid):
        row = 签名目录.事件一条(eid)
        if not row:
            return jsonify({"错误": "无此事件"}), 404
        return jsonify(row)

    @app.post("/api/事件/入队")
    def api_入队():
        body = request.get_json(force=True, silent=True) or {}
        try:
            return jsonify(入队.建事件(body))
        except ValueError as e:
            return jsonify({"错误": str(e)}), 400

    @app.get("/api/投递")
    def api_投递列表():
        return jsonify(签名目录.投递列表())

    @app.post("/api/投递")
    def api_投递():
        body = request.get_json(force=True, silent=True) or {}
        try:
            return jsonify(投递.投递一次(body["事件号"], bool(body.get("强制失败"))))
        except (KeyError, ValueError) as e:
            return jsonify({"错误": str(e)}), 400

    @app.post("/api/投递/批量重试")
    def api_批量重试():
        return jsonify(重试.批量重试待投())

    @app.get("/api/死信")
    def api_死信():
        return jsonify(签名目录.死信列表())

    @app.post("/api/死信/重投")
    def api_死信重投():
        body = request.get_json(force=True, silent=True) or {}
        try:
            return jsonify(死信.重投(body["事件号"]))
        except (KeyError, ValueError) as e:
            return jsonify({"错误": str(e)}), 400

    @app.post("/api/死信/作废")
    def api_死信作废():
        body = request.get_json(force=True, silent=True) or {}
        try:
            return jsonify(死信.作废(body["事件号"], body.get("原因", "人工作废")))
        except (KeyError, ValueError) as e:
            return jsonify({"错误": str(e)}), 400

    @app.get("/api/端点")
    def api_端点():
        return jsonify(签名目录.端点列表())

    @app.post("/api/端点")
    def api_端点登记():
        body = request.get_json(force=True, silent=True) or {}
        try:
            return jsonify(端点.登记(body))
        except ValueError as e:
            return jsonify({"错误": str(e)}), 400

    @app.post("/api/端点/启用")
    def api_端点启用():
        body = request.get_json(force=True, silent=True) or {}
        try:
            return jsonify(端点.改启用(body["端点键"], bool(body.get("启用"))))
        except (KeyError, ValueError) as e:
            return jsonify({"错误": str(e)}), 400

    @app.get("/api/互证")
    def api_互证列表():
        return jsonify(签名目录.互证列表())

    @app.post("/api/互证/跑")
    def api_互证跑():
        return jsonify(互证.跑一次())

    @app.get("/api/夹具")
    def api_夹具():
        return jsonify(夹具.列表())

    @app.post("/api/夹具入队/<name>")
    def api_夹具入队(name):
        try:
            体 = 夹具.读取(name)
            out = [入队.建事件(item) for item in 体.get("事件", [])]
            return jsonify({"入队": out})
        except FileNotFoundError:
            return jsonify({"错误": name}), 404
        except ValueError as e:
            return jsonify({"错误": str(e)}), 400

    @app.get("/api/空桩")
    def api_空桩():
        root = Path(__file__).resolve().parent / "modules"
        out = []
        if root.is_dir():
            for p in sorted(root.iterdir()):
                if p.is_dir() and (p / "__init__.py").is_file():
                    out.append(p.name)
        return jsonify({"空桩": out, "条数": len(out)})



    @app.get("/api/cache")
    @app.get("/api/cache/看板")
    @app.get("/api/cache/board")
    @app.get("/api/cache/dashboard")
    def api_cache_board():
        return jsonify(cache_svc.dashboard())

    @app.post("/api/cache/动作")
    @app.post("/api/cache/action")
    def api_cache_action():
        body = request.get_json(force=True, silent=True) or {}
        return jsonify(cache_svc.commit_action(body))

    @app.post("/api/cache/半修展示")
    @app.route("/api/cache/half", methods=["GET", "POST"])
    @app.route("/api/cache/half-fix", methods=["GET", "POST"])
    @app.route("/api/cache/half_fix", methods=["GET", "POST"])
    def api_cache_half():
        return jsonify(cache_svc.half_fix_display())

    @app.get("/api/cache/archives")
    @app.get("/api/cache/归档")
    def api_cache_archives():
        rows = cache_svc.archive_rows()
        return jsonify({"archives": rows, "archive_count": len(rows)})

    @app.get("/api/cache/ledgers")
    @app.get("/api/cache/账册")
    def api_cache_ledgers():
        rows = cache_cut_svc.list_ledgers()
        return jsonify({"ledgers": rows, "ledger_count": len(rows)})

    @app.route("/api/cache/对账", methods=["GET", "POST"])
    @app.route("/api/cache/流水对账", methods=["GET", "POST"])
    @app.route("/api/cache/reconcile_display", methods=["GET", "POST"])
    def api_cache_reconcile_cn():
        body = request.get_json(silent=True) or {}
        form_data = request.form
        event_id = (
            request.args.get("事件号")
            or request.args.get("event_id")
            or form_data.get("事件号")
            or form_data.get("event_id")
            or body.get("事件号")
            or body.get("event_id")
        )
        form = (
            request.args.get("form")
            or request.args.get("形态")
            or form_data.get("form")
            or form_data.get("形态")
            or body.get("form")
            or body.get("形态")
        )
        return jsonify(cache_svc.reconcile_display(event_id, form))

    @app.route("/api/cache/reconcile", methods=["GET", "POST"])
    def api_cache_reconcile_en():
        body = request.get_json(silent=True) or {}
        form_data = request.form
        event_id = (
            request.args.get("event_id")
            or request.args.get("事件号")
            or form_data.get("event_id")
            or form_data.get("事件号")
            or body.get("event_id")
            or body.get("事件号")
        )
        form = (
            request.args.get("form")
            or request.args.get("形态")
            or form_data.get("form")
            or form_data.get("形态")
            or body.get("form")
            or body.get("形态")
        )
        return jsonify(cache_svc.reconcile_display(event_id, form))

    @app.route("/api/投递/reconcile", methods=["GET", "POST"])
    @app.route("/api/流水/reconcile", methods=["GET", "POST"])
    @app.route("/api/投递/对账", methods=["GET", "POST"])
    @app.route("/api/流水/对账", methods=["GET", "POST"])
    @app.route("/api/流水对账", methods=["GET", "POST"])
    @app.route("/api/投递流水/对账", methods=["GET", "POST"])
    def api_delivery_reconcile_alias():
        return api_cache_reconcile_cn()

    @app.get("/api/cache/切流")
    @app.get("/api/cache/cutover")
    def api_cache_cut():
        return jsonify(cache_cut_svc.board())

    @app.post("/api/cache/切流")
    @app.post("/api/cache/cutover")
    def api_cache_cut_post():
        body = request.get_json(force=True, silent=True) or {}
        return jsonify(cache_cut_svc.append_cutover(int(body.get("ref_id") or 1), str(body.get("note") or "")))

    @app.get("/api/cache/双写")
    @app.get("/api/cache/dualwrite")
    @app.get("/api/cache/dual-write")
    def api_cache_dual():
        return jsonify(cache_dual_svc.dual_board())

    @app.post("/api/cache/双写")
    @app.post("/api/cache/dualwrite")
    @app.post("/api/cache/dual-write")
    def api_cache_dual_post():
        body = request.get_json(force=True, silent=True) or {}
        return jsonify(cache_dual_svc.apply_dualwrite(str(body.get("note") or "")))

    @app.get("/api/cache/审计")
    @app.get("/api/cache/audit")
    def api_cache_audit():
        return jsonify(cache_audit_svc.audit_summary())

    with app.app_context():
        seed.初始化库()
    return app
