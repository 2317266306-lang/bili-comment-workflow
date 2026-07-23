# -*- coding: utf-8 -*-
"""
B站自动评论工作流 - Web 看板后端（多账号版）
功能：
  - 多账号持久化存储（accounts.json）
  - 账号添加/删除/切换/验证
  - 一键粘贴Cookie字符串导入
  - 每个账号独立的评论历史
  - REST API 供前端调用
"""

import json
import os
import re
import sys
import time
import threading
import subprocess
import webbrowser
import uuid
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory

from bili_workflow import BiliWorkflow, BILI_TIDS, HEADERS

app = Flask(__name__, static_folder=".")

# ============================================================
#  全局状态
# ============================================================
# 数据目录：本地开发用当前目录，Docker 用 /app/data
DATA_DIR = os.environ.get("DATA_DIR", ".")
ACCOUNTS_FILE = os.path.join(DATA_DIR, "accounts.json")
COMMENTED_DIR = os.environ.get("COMMENTED_DIR", os.path.join(DATA_DIR, "commented"))
MONITORS_FILE = os.path.join(DATA_DIR, "monitors.json")

# 检测是否为 Linux 环境（云端），内置浏览器功能不可用
IS_LINUX = sys.platform.startswith("linux")

# 当前活跃的 workflow 实例
active_workflow = None
active_account_id = None
current_results = []
comment_task = {"running": False, "progress": 0, "total": 0, "success": 0, "fail": 0, "log": []}

# 监控状态
monitor_state = {"running": False, "thread": None, "log": []}  # 最近日志


# ============================================================
#  账号持久化管理
# ============================================================
def load_accounts():
    """加载所有账号"""
    if not os.path.exists(ACCOUNTS_FILE):
        return {"accounts": [], "active": None}
    with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_accounts(data):
    """保存账号数据"""
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_commented_path(account_id):
    """获取账号专属的评论历史文件路径"""
    os.makedirs(COMMENTED_DIR, exist_ok=True)
    return os.path.join(COMMENTED_DIR, f"{account_id}.json")


def load_monitors():
    """加载所有监控任务"""
    if not os.path.exists(MONITORS_FILE):
        return []
    with open(MONITORS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_monitors(monitors):
    """保存监控任务"""
    with open(MONITORS_FILE, "w", encoding="utf-8") as f:
        json.dump(monitors, f, ensure_ascii=False, indent=2)


def extract_bvid(raw):
    """从用户输入中提取 BVID（支持直接BV号或完整URL）"""
    if not raw:
        return None
    raw = raw.strip()
    # 匹配 BV 号
    m = re.search(r'BV[a-zA-Z0-9]{10}', raw)
    return m.group(0) if m else None


def add_monitor_log(msg):
    """添加监控日志（最多保留50条）"""
    monitor_state["log"].append({
        "time": datetime.now().strftime("%H:%M:%S"),
        "msg": msg,
    })
    if len(monitor_state["log"]) > 50:
        monitor_state["log"] = monitor_state["log"][-50:]


def mask_cookie(value):
    """脱敏显示 Cookie 值"""
    if not value:
        return "(空)"
    if len(value) <= 8:
        return value[:2] + "***"
    return value[:4] + "****" + value[-4:]


def _ensure_buvid(data, acc_dict):
    """确保账号有 buvid3 和 buvid4，缺失时自动生成"""
    if not acc_dict.get("buvid3"):
        acc_dict["buvid3"] = "XY" + uuid.uuid4().hex.upper()[:30]
    if not acc_dict.get("buvid4"):
        acc_dict["buvid4"] = uuid.uuid4().hex + "-" + uuid.uuid4().hex[:8]


def parse_cookie_string(raw):
    """从浏览器 Cookie 字符串中解析出关键字段
    支持两种格式：
    1. Name=Value; Name=Value; ...  （标准Cookie字符串）
    2. JSON 字符串 {"SESSDATA": "...", "bili_jct": "..."}
    """
    if not raw or not raw.strip():
        return None

    raw = raw.strip()

    # 尝试 JSON
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
            return {
                "SESSDATA": obj.get("SESSDATA", ""),
                "bili_jct": obj.get("bili_jct", ""),
                "buvid3": obj.get("buvid3", ""),
                "DedeUserID": str(obj.get("DedeUserID", "")),
            }
        except:
            pass

    # 标准 Cookie 字符串格式
    result = {"SESSDATA": "", "bili_jct": "", "buvid3": "", "DedeUserID": ""}
    pairs = raw.replace("\n", "").replace("\r", "").split(";")
    for pair in pairs:
        pair = pair.strip()
        if "=" in pair:
            key, _, val = pair.partition("=")
            key = key.strip()
            val = val.strip()
            if key in result:
                result[key] = val
    return result if any(result.values()) else None


def verify_cookie(cookie):
    """验证Cookie是否有效，返回 (ok, info_dict)"""
    import requests as req
    session = req.Session()
    session.headers.update(HEADERS)
    for k, v in cookie.items():
        if v:
            session.cookies.set(k, v, domain=".bilibili.com")
    # 自动补全 buvid3/buvid4 用于验证请求
    if not cookie.get("buvid3"):
        session.cookies.set("buvid3", "XY" + uuid.uuid4().hex.upper()[:30], domain=".bilibili.com")
    if not cookie.get("buvid4"):
        session.cookies.set("buvid4", uuid.uuid4().hex + "-" + uuid.uuid4().hex[:8], domain=".bilibili.com")

    try:
        resp = session.get("https://api.bilibili.com/x/web-interface/nav", timeout=10)
        data = resp.json()
        if data["code"] == 0 and data["data"]["isLogin"]:
            return True, {
                "uname": data["data"]["uname"],
                "mid": data["data"]["mid"],
                "face": data["data"].get("face", ""),
                "level": data["data"].get("level_info", {}).get("current_level", 0),
                "vip": data["data"].get("vip", {}).get("status", 0),
            }
        return False, {"error": data.get("message", "登录状态无效")}
    except Exception as e:
        return False, {"error": str(e)}


def get_or_create_workflow(account_id):
    """获取或创建指定账号的 workflow 实例"""
    global active_workflow, active_account_id

    if active_account_id == account_id and active_workflow is not None:
        return active_workflow

    data = load_accounts()
    for acc in data["accounts"]:
        if acc["id"] == account_id:
            config = {
                "cookie": {
                    "SESSDATA": acc.get("SESSDATA", ""),
                    "bili_jct": acc.get("bili_jct", ""),
                    "buvid3": acc.get("buvid3", ""),
                    "buvid4": acc.get("buvid4", ""),
                    "DedeUserID": acc.get("DedeUserID", ""),
                },
                "history_file": get_commented_path(account_id),
                "search": {"keyword": "", "order": "pubdate", "tid": 0,
                           "duration": 0, "page": 1, "page_size": 20},
                "filter": {"min_play": 0, "max_play": 0, "min_fans": 0,
                           "max_fans": 0, "need_official": False, "exclude_commented": True},
                "comment": {"delay_seconds": 15},
            }
            active_workflow = BiliWorkflow(config)
            active_account_id = account_id
            return active_workflow

    return None


# ============================================================
#  API 路由 —— 账号管理
# ============================================================

@app.route("/")
def index():
    return send_from_directory(".", "dashboard.html")


@app.route("/api/accounts", methods=["GET"])
def api_list_accounts():
    """列出所有账号"""
    data = load_accounts()
    accounts = []
    for acc in data["accounts"]:
        accounts.append({
            "id": acc["id"],
            "nickname": acc.get("nickname", "未命名"),
            "uname": acc.get("uname", ""),
            "face": acc.get("face", ""),
            "level": acc.get("level", 0),
            "vip": acc.get("vip", 0),
            "sessdata_masked": mask_cookie(acc.get("SESSDATA", "")),
            "last_verified": acc.get("last_verified", ""),
            "is_active": acc["id"] == data.get("active"),
        })
    return jsonify({"ok": True, "accounts": accounts, "active": data.get("active")})


@app.route("/api/accounts/import", methods=["POST"])
def api_import_account():
    """导入账号 —— 支持分字段输入或 Cookie 字符串"""
    data_req = request.get_json() or {}
    raw = data_req.get("cookie_string", "").strip()
    sessdata = data_req.get("SESSDATA", "").strip()
    bili_jct = data_req.get("bili_jct", "").strip()
    nickname = data_req.get("nickname", "").strip()

    # 优先使用分字段输入，否则尝试解析 Cookie 字符串
    if sessdata or bili_jct:
        cookie = {
            "SESSDATA": sessdata,
            "bili_jct": bili_jct,
            "buvid3": data_req.get("buvid3", "").strip(),
            "buvid4": data_req.get("buvid4", "").strip(),
            "DedeUserID": "",
        }
    elif raw:
        cookie = parse_cookie_string(raw)
        if not cookie:
            return jsonify({"ok": False, "error": "无法解析 Cookie 字符串，请检查格式"})
    else:
        return jsonify({"ok": False, "error": "请输入 SESSDATA 和 bili_jct"})

    if not cookie["SESSDATA"] or not cookie["bili_jct"]:
        return jsonify({"ok": False, "error": "缺少 SESSDATA 或 bili_jct，请检查 Cookie"})

    # 验证Cookie
    ok, info = verify_cookie(cookie)
    if not ok:
        return jsonify({"ok": False, "error": f"Cookie 验证失败: {info.get('error', '未知错误')}"})

    # 检查是否重复（相同 mid）
    data = load_accounts()
    existing_mid = str(info["mid"])
    for acc in data["accounts"]:
        if acc.get("DedeUserID") == existing_mid:
            # 更新已有账号
            acc["SESSDATA"] = cookie["SESSDATA"]
            acc["bili_jct"] = cookie["bili_jct"]
            acc["buvid3"] = cookie.get("buvid3", acc.get("buvid3", ""))
            acc["DedeUserID"] = existing_mid
            acc["uname"] = info["uname"]
            acc["face"] = info.get("face", "")
            acc["level"] = info.get("level", 0)
            acc["vip"] = info.get("vip", 0)
            acc["last_verified"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if nickname:
                acc["nickname"] = nickname
            _ensure_buvid(data, acc)
            save_accounts(data)
            return jsonify({"ok": True, "updated": True, "account": {**acc, "sessdata_masked": mask_cookie(acc["SESSDATA"])}})

    # 新建账号
    account = {
        "id": uuid.uuid4().hex[:12],
        "nickname": nickname or info["uname"],
        "SESSDATA": cookie["SESSDATA"],
        "bili_jct": cookie["bili_jct"],
        "buvid3": cookie.get("buvid3", ""),
        "buvid4": cookie.get("buvid4", ""),
        "DedeUserID": existing_mid,
        "uname": info["uname"],
        "face": info.get("face", ""),
        "level": info.get("level", 0),
        "vip": info.get("vip", 0),
        "last_verified": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _ensure_buvid(data, account)
    data["accounts"].append(account)

    # 如果这是第一个账号，自动设为活跃
    if not data["active"]:
        data["active"] = account["id"]

    save_accounts(data)
    return jsonify({"ok": True, "updated": False, "account": {**account, "sessdata_masked": mask_cookie(account["SESSDATA"])}})


@app.route("/api/accounts/browser-login", methods=["POST"])
def api_browser_login():
    """打开系统浏览器，引导用户登录B站并复制Cookie"""
    try:
        webbrowser.open("https://www.bilibili.com")
        return jsonify({"ok": True, "message": "已打开浏览器，请登录B站后复制 SESSDATA 和 bili_jct"})
    except Exception as e:
        return jsonify({"ok": False, "error": f"无法打开浏览器: {e}"})


@app.route("/api/accounts/builtin-browser-login", methods=["POST"])
def api_builtin_browser_login():
    """
    启动内置浏览器（Chrome 独立 Profile）进行登录
    - Windows: 使用独立 Chrome Profile，登录后自动提取 Cookie
    - Linux（云端）: 不支持，请使用手动输入 Cookie
    """
    if IS_LINUX:
        return jsonify({
            "ok": False,
            "error": "云端环境不支持内置浏览器登录。请在浏览器中登录B站，按 F12 → Application → Cookies → bilibili.com，复制 SESSDATA 和 bili_jct 后手动填入下方输入框。",
        })

    try:
        script_path = Path(__file__).parent / "builtin_browser.py"
        if not script_path.exists():
            return jsonify({"ok": False, "error": "内置浏览器脚本不存在，请联系管理员"})

        # 检查是否已有 Chrome 在运行内置浏览器
        # 如果 browser_profile 目录存在且最近有活动，可能已经登录过
        profile_dir = Path(__file__).parent / "browser_profile"
        if profile_dir.exists():
            # 清除上次的失败标记
            failed_marker = profile_dir / "_login_failed"
            if failed_marker.exists():
                failed_marker.unlink()

        # 启动子进程（非阻塞）
        proc = subprocess.Popen(
            [sys.executable, str(script_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )

        return jsonify({
            "ok": True,
            "message": "内置浏览器已启动，请在浏览器中登录B站，登录完成后关闭浏览器窗口即可自动保存",
            "pid": proc.pid,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": f"启动内置浏览器失败: {e}"})


@app.route("/api/accounts/builtin-browser-status", methods=["GET"])
def api_builtin_browser_status():
    """
    检查内置浏览器的登录状态
    前端轮询此接口，检测是否已完成登录
    """
    if IS_LINUX:
        return jsonify({"ok": True, "status": "unsupported"})

    profile_dir = Path(__file__).parent / "browser_profile"
    failed_marker = profile_dir / "_login_failed"
    cookies_db_new = profile_dir / "Default" / "Network" / "Cookies"
    cookies_db_old = profile_dir / "Default" / "Cookies"

    if failed_marker.exists():
        reason = failed_marker.read_text().strip()
        return jsonify({"ok": True, "status": "failed", "reason": reason})

    if cookies_db_new.exists() or cookies_db_old.exists():
        return jsonify({"ok": True, "status": "profile_exists"})

    return jsonify({"ok": True, "status": "waiting"})


@app.route("/api/accounts/<account_id>/activate", methods=["POST"])
def api_activate_account(account_id):
    """切换活跃账号"""
    global active_workflow, active_account_id, current_results

    data = load_accounts()
    target_acc = None
    for acc in data["accounts"]:
        if acc["id"] == account_id:
            target_acc = acc
            break

    if not target_acc:
        return jsonify({"ok": False, "error": "账号不存在"})

    # 验证 Cookie 是否仍有效
    cookie = {
        "SESSDATA": target_acc["SESSDATA"],
        "bili_jct": target_acc["bili_jct"],
        "buvid3": target_acc.get("buvid3", ""),
        "DedeUserID": target_acc.get("DedeUserID", ""),
    }
    ok, info = verify_cookie(cookie)
    if not ok:
        return jsonify({"ok": False, "error": f"Cookie 已失效: {info.get('error', '')}，请重新导入"})

    # 更新验证信息
    target_acc["uname"] = info["uname"]
    target_acc["face"] = info.get("face", "")
    target_acc["level"] = info.get("level", 0)
    target_acc["vip"] = info.get("vip", 0)
    target_acc["last_verified"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data["active"] = account_id
    save_accounts(data)

    # 切换 workflow
    active_workflow = None
    active_account_id = None
    current_results = []
    wf = get_or_create_workflow(account_id)

    return jsonify({
        "ok": True,
        "uname": info["uname"],
        "face": info.get("face", ""),
        "level": info.get("level", 0),
        "vip": info.get("vip", 0),
    })


@app.route("/api/accounts/<account_id>", methods=["DELETE"])
def api_delete_account(account_id):
    """删除账号"""
    data = load_accounts()
    before = len(data["accounts"])
    data["accounts"] = [a for a in data["accounts"] if a["id"] != account_id]

    if len(data["accounts"]) == before:
        return jsonify({"ok": False, "error": "账号不存在"})

    if data["active"] == account_id:
        data["active"] = data["accounts"][0]["id"] if data["accounts"] else None
        global active_workflow, active_account_id
        active_workflow = None
        active_account_id = None

    save_accounts(data)

    # 删除评论历史文件
    history_path = get_commented_path(account_id)
    if os.path.exists(history_path):
        os.remove(history_path)

    return jsonify({"ok": True, "active": data.get("active")})


@app.route("/api/accounts/<account_id>/verify", methods=["POST"])
def api_verify_account(account_id):
    """验证单个账号Cookie是否有效"""
    data = load_accounts()
    for acc in data["accounts"]:
        if acc["id"] == account_id:
            cookie = {
                "SESSDATA": acc["SESSDATA"],
                "bili_jct": acc["bili_jct"],
                "buvid3": acc.get("buvid3", ""),
                "DedeUserID": acc.get("DedeUserID", ""),
            }
            ok, info = verify_cookie(cookie)
            if ok:
                acc["uname"] = info["uname"]
                acc["face"] = info.get("face", "")
                acc["level"] = info.get("level", 0)
                acc["vip"] = info.get("vip", 0)
                acc["last_verified"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                save_accounts(data)
                return jsonify({"ok": True, "valid": True, "uname": info["uname"]})
            else:
                return jsonify({"ok": True, "valid": False, "error": info.get("error", "验证失败")})

    return jsonify({"ok": False, "error": "账号不存在"})


@app.route("/api/accounts/<account_id>/nickname", methods=["PUT"])
def api_update_nickname(account_id):
    """修改账号昵称"""
    data = load_accounts()
    for acc in data["accounts"]:
        if acc["id"] == account_id:
            req_data = request.get_json() or {}
            new_nickname = req_data.get("nickname", "").strip()
            if not new_nickname:
                return jsonify({"ok": False, "error": "昵称不能为空"})
            acc["nickname"] = new_nickname
            save_accounts(data)
            return jsonify({"ok": True, "nickname": new_nickname})
    return jsonify({"ok": False, "error": "账号不存在"})


@app.route("/api/accounts/verify-all", methods=["POST"])
def api_verify_all():
    """批量验证所有账号的 Cookie 是否有效"""
    data = load_accounts()
    results = []
    for acc in data["accounts"]:
        cookie = {
            "SESSDATA": acc["SESSDATA"],
            "bili_jct": acc["bili_jct"],
            "buvid3": acc.get("buvid3", ""),
            "DedeUserID": acc.get("DedeUserID", ""),
        }
        ok, info = verify_cookie(cookie)
        if ok:
            acc["uname"] = info["uname"]
            acc["face"] = info.get("face", "")
            acc["level"] = info.get("level", 0)
            acc["vip"] = info.get("vip", 0)
            acc["last_verified"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            results.append({
                "id": acc["id"],
                "nickname": acc.get("nickname", ""),
                "valid": True,
                "uname": info["uname"],
                "level": info.get("level", 0),
            })
        else:
            results.append({
                "id": acc["id"],
                "nickname": acc.get("nickname", ""),
                "valid": False,
                "error": info.get("error", "验证失败"),
            })

    save_accounts(data)
    return jsonify({"ok": True, "results": results})


# ============================================================
#  API 路由 —— 搜索 & 评论
# ============================================================

@app.route("/api/search", methods=["POST"])
def api_search():
    """搜索视频"""
    global current_results

    data_req = load_accounts()
    active_id = data_req.get("active")
    if not active_id:
        return jsonify({"ok": False, "error": "请先导入并激活一个账号"})

    wf = get_or_create_workflow(active_id)
    if wf is None:
        return jsonify({"ok": False, "error": "账号未找到，请重新导入"})

    data = request.get_json() or {}
    keyword = data.get("keyword", "")
    tid = int(data.get("tid", 0))
    order = data.get("order", "pubdate")
    duration = int(data.get("duration", 0))
    page = int(data.get("page", 1))
    page_size = int(data.get("page_size", 20))
    min_play = int(data.get("min_play", 0))
    max_play = int(data.get("max_play", 0))
    min_fans = int(data.get("min_fans", 0))
    max_fans = int(data.get("max_fans", 0))
    need_official = bool(data.get("need_official", False))

    if not keyword:
        return jsonify({"ok": False, "error": "请输入搜索关键词"})

    wf.config["search"] = {
        "keyword": keyword, "order": order, "tid": tid,
        "duration": duration, "page": page, "page_size": page_size,
    }
    wf.config["filter"] = {
        "min_play": min_play, "max_play": max_play,
        "min_fans": min_fans, "max_fans": max_fans,
        "need_official": need_official, "exclude_commented": True,
    }

    raw_videos = wf.search_videos()
    if not raw_videos:
        return jsonify({"ok": False, "error": "没有搜索到视频，请更换关键词"})

    filtered = wf.filter_videos(raw_videos)
    if not filtered:
        return jsonify({"ok": False, "error": "没有视频通过筛选，请放宽条件"})

    enriched = wf.enrich_video_data(filtered)
    final = wf.apply_user_filters(enriched)

    if not final:
        return jsonify({"ok": False, "error": "没有视频通过最终筛选，请放宽条件"})

    current_results = final
    return jsonify({
        "ok": True, "total": len(final), "videos": final,
        "tid_name": BILI_TIDS.get(tid, "全部"),
        "page": page, "page_size": page_size,
    })


@app.route("/api/comment", methods=["POST"])
def api_comment():
    """对选中视频发送评论"""
    global current_results, comment_task

    if comment_task["running"]:
        return jsonify({"ok": False, "error": "正在评论中，请等待完成"})

    data_req = load_accounts()
    active_id = data_req.get("active")
    if not active_id:
        return jsonify({"ok": False, "error": "请先激活一个账号"})

    wf = get_or_create_workflow(active_id)
    if wf is None:
        return jsonify({"ok": False, "error": "账号未找到"})

    data = request.get_json() or {}
    indexes = data.get("indexes", [])
    message = data.get("message", "").strip()
    delay = int(data.get("delay", 15))

    if not indexes:
        return jsonify({"ok": False, "error": "请选择要评论的视频"})
    if not message:
        return jsonify({"ok": False, "error": "请输入评论内容"})

    targets = [current_results[i] for i in indexes if 0 <= i < len(current_results)]
    if not targets:
        return jsonify({"ok": False, "error": "没有有效的视频"})

    comment_task = {
        "running": True, "progress": 0, "total": len(targets),
        "success": 0, "fail": 0, "log": [],
    }

    def do_comment():
        global comment_task
        for i, v in enumerate(targets):
            if not comment_task["running"]:
                break
            oid = v["aid"]
            result = wf.post_comment(v["bvid"], oid, message)
            code = result.get("code", -1)

            if code == 0:
                wf._save_history(v["bvid"], message, v["title"])
                comment_task["success"] += 1
                comment_task["log"].append({
                    "idx": i + 1, "bvid": v["bvid"], "title": v["title"][:40],
                    "status": "ok", "msg": f"评论成功",
                })
            elif code == 12002:
                comment_task["fail"] += 1
                comment_task["log"].append({
                    "idx": i + 1, "bvid": v["bvid"], "title": v["title"][:40],
                    "status": "skip", "msg": "评论区已关闭",
                })
            elif code == 12005:
                comment_task["fail"] += 1
                comment_task["log"].append({
                    "idx": i + 1, "bvid": v["bvid"], "title": v["title"][:40],
                    "status": "skip", "msg": "已评论过相同内容",
                })
            else:
                comment_task["fail"] += 1
                comment_task["log"].append({
                    "idx": i + 1, "bvid": v["bvid"], "title": v["title"][:40],
                    "status": "fail", "msg": f"code={code} {result.get('message', '')}",
                })

            comment_task["progress"] = i + 1
            if i < len(targets) - 1:
                time.sleep(delay)

        comment_task["running"] = False

    threading.Thread(target=do_comment, daemon=True).start()
    return jsonify({"ok": True, "total": len(targets), "message": f"开始评论 {len(targets)} 个视频"})


@app.route("/api/task_status", methods=["GET"])
def api_task_status():
    return jsonify(comment_task)


@app.route("/api/stop_task", methods=["POST"])
def api_stop_task():
    global comment_task
    comment_task["running"] = False
    return jsonify({"ok": True})


@app.route("/api/history", methods=["GET"])
def api_history():
    data_req = load_accounts()
    active_id = data_req.get("active")
    if not active_id:
        return jsonify({"ok": True, "history": []})
    wf = get_or_create_workflow(active_id)
    if wf is None:
        return jsonify({"ok": True, "history": []})
    return jsonify({"ok": True, "history": wf.history})


@app.route("/api/tids", methods=["GET"])
def api_tids():
    tids = [{"id": k, "name": v} for k, v in BILI_TIDS.items()]
    return jsonify({"ok": True, "tids": tids})


# ============================================================
#  API 路由 —— 首条评论监控
# ============================================================

def _get_account_dict(account_id):
    """根据账号ID获取 Cookie 字典，用于置顶等操作"""
    data = load_accounts()
    for acc in data["accounts"]:
        if acc["id"] == account_id:
            return {
                "SESSDATA": acc.get("SESSDATA", ""),
                "bili_jct": acc.get("bili_jct", ""),
                "buvid3": acc.get("buvid3", ""),
                "buvid4": acc.get("buvid4", ""),
                "DedeUserID": acc.get("DedeUserID", ""),
            }
    return None


def _try_pin(pin_account_id, oid, rpid, title):
    """尝试用博主账号置顶评论"""
    if not pin_account_id or not rpid:
        return False

    account_dict = _get_account_dict(pin_account_id)
    if not account_dict:
        add_monitor_log(f"[{title[:20]}] 置顶失败: 博主账号 {pin_account_id} 不存在")
        return False

    pin_result = BiliWorkflow.pin_with_account(oid, rpid, account_dict)
    pin_code = pin_result.get("code", -1)
    if pin_code == 0:
        add_monitor_log(f"[{title[:20]}] 置顶成功 ✓")
        return True
    else:
        pin_msg = pin_result.get("message", "未知错误")
        add_monitor_log(f"[{title[:20]}] 置顶失败 code={pin_code} {pin_msg}")
        return False


def _monitor_thread():
    """后台监控线程：定期检查所有活跃监控任务的首条评论"""
    global monitor_state
    add_monitor_log("监控线程已启动")

    while monitor_state["running"]:
        try:
            monitors = load_monitors()
            active_monitors = [m for m in monitors if m.get("status") == "active"]

            if not active_monitors:
                time.sleep(10)
                continue

            data_req = load_accounts()
            active_id = data_req.get("active")
            if not active_id:
                time.sleep(10)
                continue

            wf = get_or_create_workflow(active_id)
            if wf is None:
                time.sleep(10)
                continue

            for m in monitors:
                if not monitor_state["running"]:
                    break
                if m.get("status") != "active":
                    continue

                # 检查是否到了轮询时间
                now = time.time()
                last_check = m.get("last_check_ts", 0)
                interval = m.get("interval", 30)
                if now - last_check < interval:
                    continue

                try:
                    # 获取当前首条评论
                    current = wf.get_first_comment(m["aid"])
                    m["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    m["last_check_ts"] = now

                    if current is None:
                        # 首条评论消失了（可能被删除或置顶变更）
                        if m.get("last_first_content") is not None:
                            add_monitor_log(f"[{m['title'][:20]}] 首条评论消失，触发自动补评")
                            # 自动评论
                            result = wf.post_comment(m["bvid"], m["aid"], m["comment"])
                            code = result.get("code", -1)
                            if code == 0:
                                m["trigger_count"] = m.get("trigger_count", 0) + 1
                                m["last_trigger"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                new_rpid = result["data"].get("rpid") if result.get("data") else None
                                add_monitor_log(f"[{m['title'][:20]}] 补评成功 ✓")
                                wf._save_history(m["bvid"], m["comment"], m["title"])
                                # 置顶
                                if m.get("pin_account_id") and new_rpid:
                                    _try_pin(m["pin_account_id"], m["aid"], new_rpid, m["title"])
                            else:
                                add_monitor_log(f"[{m['title'][:20]}] 补评失败 code={code}")
                            # 更新状态
                            m["last_first_content"] = None
                            m["last_first_rpid"] = None
                    elif current["content"] != m.get("last_first_content"):
                        # 首条评论内容变了
                        old_content = (m.get("last_first_content") or "(空)")[:30]
                        new_content = current["content"][:30]
                        add_monitor_log(f"[{m['title'][:20]}] 首条评论变动: {old_content} → {new_content}")
                        # 自动评论
                        result = wf.post_comment(m["bvid"], m["aid"], m["comment"])
                        code = result.get("code", -1)
                        if code == 0:
                            m["trigger_count"] = m.get("trigger_count", 0) + 1
                            m["last_trigger"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            new_rpid = result["data"].get("rpid") if result.get("data") else None
                            add_monitor_log(f"[{m['title'][:20]}] 补评成功 ✓")
                            wf._save_history(m["bvid"], m["comment"], m["title"])
                            # 置顶
                            if m.get("pin_account_id") and new_rpid:
                                _try_pin(m["pin_account_id"], m["aid"], new_rpid, m["title"])
                        else:
                            add_monitor_log(f"[{m['title'][:20]}] 补评失败 code={code}")
                        # 更新为当前首条评论
                        m["last_first_content"] = current["content"]
                        m["last_first_rpid"] = current["rpid"]
                    # 首条评论没变，不操作

                except Exception as e:
                    add_monitor_log(f"[{m.get('title', '?')[:20]}] 检查异常: {e}")

            save_monitors(monitors)

        except Exception as e:
            add_monitor_log(f"监控线程异常: {e}")

        # 每10秒检查一次（具体每个任务的频率由各自的 interval 控制）
        time.sleep(10)

    add_monitor_log("监控线程已停止")


def start_monitor_thread():
    """启动监控线程（如果未启动）"""
    global monitor_state
    if not monitor_state["running"]:
        monitor_state["running"] = True
        monitor_state["log"] = []
        t = threading.Thread(target=_monitor_thread, daemon=True)
        monitor_state["thread"] = t
        t.start()
        add_monitor_log("监控线程启动中...")


def stop_monitor_thread():
    """停止监控线程"""
    global monitor_state
    monitor_state["running"] = False
    add_monitor_log("监控线程正在停止...")


@app.route("/api/monitors", methods=["GET"])
def api_list_monitors():
    """列出所有监控任务"""
    monitors = load_monitors()
    return jsonify({
        "ok": True,
        "monitors": monitors,
        "monitor_running": monitor_state["running"],
        "monitor_log": monitor_state["log"][-20:],  # 最近20条日志
    })


@app.route("/api/monitors/add", methods=["POST"])
def api_add_monitor():
    """添加监控任务"""
    data_req = request.get_json() or {}
    raw = data_req.get("url", "").strip()
    comment = data_req.get("comment", "").strip()
    interval = int(data_req.get("interval", 30))
    pin_account_id = data_req.get("pin_account_id", "").strip()

    if not raw:
        return jsonify({"ok": False, "error": "请输入视频链接或BV号"})
    if not comment:
        return jsonify({"ok": False, "error": "请输入自动评论内容"})
    if interval < 10:
        return jsonify({"ok": False, "error": "检查间隔不能小于10秒"})

    bvid = extract_bvid(raw)
    if not bvid:
        return jsonify({"ok": False, "error": "无法识别BV号，请输入完整的视频链接或BV号"})

    # 获取活跃账号
    data = load_accounts()
    active_id = data.get("active")
    if not active_id:
        return jsonify({"ok": False, "error": "请先激活一个账号"})

    # 验证博主账号（如果指定了）
    pin_nickname = ""
    if pin_account_id:
        pin_found = False
        for acc in data["accounts"]:
            if acc["id"] == pin_account_id:
                pin_found = True
                pin_nickname = acc.get("nickname", acc.get("uname", ""))
                break
        if not pin_found:
            return jsonify({"ok": False, "error": "指定的博主账号不存在，请重新选择"})

    wf = get_or_create_workflow(active_id)
    if wf is None:
        return jsonify({"ok": False, "error": "账号未找到"})

    # 获取视频信息
    info = wf.get_video_info(bvid)
    if not info:
        return jsonify({"ok": False, "error": "无法获取视频信息，请检查BV号是否正确"})

    aid = info["aid"]
    title = info.get("title", "未知")

    # 检查是否已存在
    monitors = load_monitors()
    for m in monitors:
        if m["bvid"] == bvid:
            return jsonify({"ok": False, "error": f"该视频已在监控列表中: {m['title']}"})

    # 获取当前首条评论
    first = wf.get_first_comment(aid)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    now_ts = time.time()

    monitor = {
        "id": uuid.uuid4().hex[:12],
        "bvid": bvid,
        "aid": aid,
        "title": title,
        "comment": comment,
        "interval": interval,
        "pin_account_id": pin_account_id or None,
        "pin_nickname": pin_nickname or None,
        "last_first_content": first["content"] if first else None,
        "last_first_rpid": first["rpid"] if first else None,
        "last_first_uname": first["uname"] if first else None,
        "last_check": now,
        "last_check_ts": now_ts,
        "status": "active",
        "trigger_count": 0,
        "last_trigger": None,
        "created_at": now,
    }

    monitors.append(monitor)
    save_monitors(monitors)

    # 确保监控线程运行
    start_monitor_thread()

    pin_info = f"，置顶账号: {pin_nickname}" if pin_nickname else ""
    add_monitor_log(f"[{title[:20]}] 新增监控{pin_info}，首条评论: {(first['content'] if first else '(无)')[:30]}")

    return jsonify({"ok": True, "monitor": monitor})


@app.route("/api/monitors/<monitor_id>", methods=["DELETE"])
def api_delete_monitor(monitor_id):
    """删除监控任务"""
    monitors = load_monitors()
    before = len(monitors)
    monitors = [m for m in monitors if m["id"] != monitor_id]
    if len(monitors) == before:
        return jsonify({"ok": False, "error": "监控任务不存在"})
    save_monitors(monitors)
    add_monitor_log(f"监控任务已删除 ({monitor_id})")
    return jsonify({"ok": True})


@app.route("/api/monitors/<monitor_id>/toggle", methods=["POST"])
def api_toggle_monitor(monitor_id):
    """暂停/恢复监控任务"""
    monitors = load_monitors()
    for m in monitors:
        if m["id"] == monitor_id:
            m["status"] = "paused" if m["status"] == "active" else "active"
            save_monitors(monitors)
            add_monitor_log(f"[{m['title'][:20]}] {'已暂停' if m['status'] == 'paused' else '已恢复'}")
            return jsonify({"ok": True, "status": m["status"]})
    return jsonify({"ok": False, "error": "监控任务不存在"})


@app.route("/api/monitors/<monitor_id>/check", methods=["POST"])
def api_manual_check(monitor_id):
    """手动触发一次检查"""
    monitors = load_monitors()
    target = None
    for m in monitors:
        if m["id"] == monitor_id:
            target = m
            break
    if not target:
        return jsonify({"ok": False, "error": "监控任务不存在"})

    data = load_accounts()
    active_id = data.get("active")
    if not active_id:
        return jsonify({"ok": False, "error": "请先激活一个账号"})

    wf = get_or_create_workflow(active_id)
    if wf is None:
        return jsonify({"ok": False, "error": "账号未找到"})

    current = wf.get_first_comment(target["aid"])
    target["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    target["last_check_ts"] = time.time()

    changed = False
    if current is None:
        changed = target.get("last_first_content") is not None
    elif current["content"] != target.get("last_first_content"):
        changed = True

    if changed:
        add_monitor_log(f"[{target['title'][:20]}] 手动检查: 首条评论已变动")
        result = wf.post_comment(target["bvid"], target["aid"], target["comment"])
        code = result.get("code", -1)
        if code == 0:
            target["trigger_count"] = target.get("trigger_count", 0) + 1
            target["last_trigger"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            new_rpid = result["data"].get("rpid") if result.get("data") else None
            wf._save_history(target["bvid"], target["comment"], target["title"])
            add_monitor_log(f"[{target['title'][:20]}] 手动补评成功 ✓")
            # 置顶
            if target.get("pin_account_id") and new_rpid:
                _try_pin(target["pin_account_id"], target["aid"], new_rpid, target["title"])
        else:
            add_monitor_log(f"[{target['title'][:20]}] 手动补评失败 code={code}")

        if current:
            target["last_first_content"] = current["content"]
            target["last_first_rpid"] = current["rpid"]
        else:
            target["last_first_content"] = None
            target["last_first_rpid"] = None
    else:
        add_monitor_log(f"[{target['title'][:20]}] 手动检查: 首条评论未变化")

    save_monitors(monitors)
    return jsonify({
        "ok": True,
        "changed": changed,
        "current_first": current,
        "monitor": target,
    })


@app.route("/api/active_info", methods=["GET"])
def api_active_info():
    """获取当前活跃账号信息"""
    data = load_accounts()
    active_id = data.get("active")
    if not active_id:
        return jsonify({"ok": False, "error": "无活跃账号"})
    for acc in data["accounts"]:
        if acc["id"] == active_id:
            return jsonify({
                "ok": True,
                "uname": acc.get("uname", ""),
                "face": acc.get("face", ""),
                "level": acc.get("level", 0),
                "vip": acc.get("vip", 0),
                "nickname": acc.get("nickname", ""),
                "last_verified": acc.get("last_verified", ""),
            })
    return jsonify({"ok": False, "error": "账号未找到"})


# ============================================================
#  启动
# ============================================================

if __name__ == "__main__":
    os.makedirs(COMMENTED_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)

    # 监控线程（Render 免费版会休眠，仅在本地或付费版有效）
    if not os.environ.get("RENDER"):
        start_monitor_thread()
        monitor_msg = "首条评论监控已启动"
    else:
        monitor_msg = "云端模式（Render）- 监控线程已禁用（免费版不支持长期运行）"

    port = int(os.environ.get("PORT", 5000))
    is_render = os.environ.get("RENDER")

    print("\n" + "=" * 60)
    print("  B站自动评论工作流 - Web 看板 (多账号版)")
    if is_render:
        print(f"  运行平台: Render")
        print(f"  监听端口: {port}")
    else:
        print(f"  打开浏览器访问: http://localhost:{port}")
    print(f"  {monitor_msg}")
    print("=" * 60 + "\n")

    if is_render:
        from waitress import serve
        serve(app, host="0.0.0.0", port=port, threads=4)
    else:
        app.run(host="0.0.0.0", port=port, debug=False)