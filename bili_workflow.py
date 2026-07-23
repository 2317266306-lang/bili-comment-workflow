# -*- coding: utf-8 -*-
"""
B站自动评论工作流 - 核心脚本（多账号版）
功能：按关键词/话题/播放量/达人类型筛选视频 → 用户输入评论 → 自动发送
支持从 accounts.json 读取多账号配置，兼容旧版 CONFIG 字典方式
"""

import requests
import json
import time
import re
import os
import sys
import hashlib
import urllib.parse
import argparse
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# ============================================================
#  配置区（请根据实际情况修改）
# ============================================================
CONFIG = {
    # --- B站 Cookie（必填！从浏览器 F12 → Application → Cookies 复制）---
    "cookie": {
        "SESSDATA": "",      # 你的 SESSDATA
        "bili_jct": "",      # 你的 bili_jct（同时也是 CSRF Token）
        "buvid3": "",        # 你的 buvid3
        "DedeUserID": "",    # 你的 UID
    },

    # --- 搜索配置 ---
    "search": {
        "keyword": "",       # 搜索关键词（必填）
        "order": "pubdate",  # 排序: totalrank(综合) click(最多播放) pubdate(最新发布) stow(最多收藏) dm(最多弹幕)
        "tid": 0,            # 分区ID: 0=全部 4=游戏 36=知识 160=生活 188=科技 等（见下方分区对照表）
        "duration": 0,       # 时长: 0=全部 1=<10分钟 2=10-30分钟 3=30-60分钟 4=>60分钟
        "page": 1,           # 搜索页码
        "page_size": 20,     # 每页数量（最大50）
    },

    # --- 筛选规则 ---
    "filter": {
        "min_play": 0,       # 最低播放量（0=不过滤）
        "max_play": 0,       # 最高播放量（0=不过滤）
        "min_fans": 0,       # UP主最低粉丝数（0=不过滤）
        "max_fans": 0,       # UP主最高粉丝数（0=不过滤）
        "need_official": False,  # 只要认证UP主（个人认证/机构认证）
        "exclude_commented": True,  # 排除已评论过的视频
    },

    # --- 评论配置 ---
    "comment": {
        "delay_seconds": 15,  # 每条评论间隔（秒），建议≥10秒，避免风控
    },

    # --- 已评论记录文件 ---
    "history_file": "commented_videos.json",
}

# ============================================================
#  B站分区ID对照表
# ============================================================
BILI_TIDS = {
    0: "全部", 1: "动画", 3: "音乐", 4: "游戏", 5: "娱乐",
    11: "电视剧", 13: "番剧", 17: "国创", 23: "电影",
    36: "知识", 119: "鬼畜", 129: "舞蹈", 155: "时尚",
    160: "生活", 165: "美食", 181: "汽车", 188: "科技",
    202: "资讯", 217: "动物圈", 234: "运动",
}

# API 端点
API = {
    "search": "https://api.bilibili.com/x/web-interface/search/type",
    "video_info": "https://api.bilibili.com/x/web-interface/view",
    "user_info": "https://api.bilibili.com/x/space/acc/info",
    "reply_add": "https://api.bilibili.com/x/v2/reply/add",
    "nav": "https://api.bilibili.com/x/web-interface/nav",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
}


class BiliWorkflow:
    def __init__(self, config):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._set_cookies()
        self.history = self._load_history()

    @classmethod
    def from_accounts_json(cls, accounts_path="accounts.json", account_id=None):
        """从 accounts.json 创建 workflow 实例

        Args:
            accounts_path: accounts.json 文件路径
            account_id: 指定账号ID，为 None 则使用活跃账号

        Returns:
            (BiliWorkflow, account_info) 或 (None, error_msg)
        """
        if not os.path.exists(accounts_path):
            return None, f"账号文件不存在: {accounts_path}"

        with open(accounts_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not data.get("accounts"):
            return None, "账号列表为空，请先在 Web 看板中导入账号"

        # 确定使用哪个账号
        target = None
        if account_id:
            for acc in data["accounts"]:
                if acc["id"] == account_id:
                    target = acc
                    break
            if not target:
                return None, f"未找到指定账号: {account_id}"
        else:
            active_id = data.get("active")
            if active_id:
                for acc in data["accounts"]:
                    if acc["id"] == active_id:
                        target = acc
                        break
            if not target:
                target = data["accounts"][0]
                print(f"  [INFO] 未设置活跃账号，使用第一个: {target.get('nickname', '未命名')}")

        # 构建配置
        # 自动补全 buvid3/buvid4（B站反爬需要）
        if not target.get("buvid3"):
            target["buvid3"] = "XY" + uuid.uuid4().hex.upper()[:30]
        if not target.get("buvid4"):
            target["buvid4"] = uuid.uuid4().hex + "-" + uuid.uuid4().hex[:8]

        config = {
            "cookie": {
                "SESSDATA": target.get("SESSDATA", ""),
                "bili_jct": target.get("bili_jct", ""),
                "buvid3": target.get("buvid3", ""),
                "buvid4": target.get("buvid4", ""),
                "DedeUserID": target.get("DedeUserID", ""),
            },
            "history_file": os.path.join("commented", f"{target['id']}.json"),
            "search": {
                "keyword": "", "order": "pubdate", "tid": 0,
                "duration": 0, "page": 1, "page_size": 20,
            },
            "filter": {
                "min_play": 0, "max_play": 0,
                "min_fans": 0, "max_fans": 0,
                "need_official": False, "exclude_commented": True,
            },
            "comment": {"delay_seconds": 15},
        }

        wf = cls(config)
        return wf, target

    @classmethod
    def list_accounts(cls, accounts_path="accounts.json"):
        """列出所有账号（用于命令行选择）"""
        if not os.path.exists(accounts_path):
            return []
        with open(accounts_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("accounts", [])

    def _set_cookies(self):
        """设置 Cookie"""
        for key, value in self.config["cookie"].items():
            if value:
                self.session.cookies.set(key, value, domain=".bilibili.com")

    def _load_history(self):
        """加载已评论记录"""
        path = self.config["history_file"]
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        return []

    def _save_history(self, bvid, comment, title):
        """保存评论记录"""
        self.history.append({
            "bvid": bvid,
            "comment": comment,
            "title": title,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        with open(self.config["history_file"], "w", encoding="utf-8") as f:
            json.dump(self.history, f, ensure_ascii=False, indent=2)

    def check_login(self):
        """验证登录状态"""
        try:
            resp = self.session.get(API["nav"], timeout=10)
            data = resp.json()
            if data["code"] == 0 and data["data"]["isLogin"]:
                uname = data["data"]["uname"]
                print(f"  [OK] 已登录: {uname}")
                return True
            else:
                print(f"  [FAIL] 未登录，请检查 Cookie 配置")
                return False
        except Exception as e:
            print(f"  [FAIL] 登录验证失败: {e}")
            return False

    def search_videos(self):
        """搜索视频"""
        params = {
            "search_type": "video",
            "keyword": self.config["search"]["keyword"],
            "order": self.config["search"]["order"],
            "duration": self.config["search"]["duration"],
            "tids": self.config["search"]["tid"],
            "page": self.config["search"]["page"],
            "page_size": self.config["search"]["page_size"],
        }
        try:
            resp = self.session.get(API["search"], params=params, timeout=15)
            if resp.status_code != 200:
                print(f"  [FAIL] 搜索请求被拦截 (HTTP {resp.status_code})，可能是缺少 buvid3/buvid4")
                return []
            data = resp.json()
            if data["code"] != 0:
                print(f"  [FAIL] 搜索失败: {data.get('message', '未知错误')}")
                return []
            return data["data"].get("result", [])
        except ValueError as e:
            print(f"  [FAIL] 搜索响应解析失败 (非JSON): {e}")
            return []
        except Exception as e:
            print(f"  [FAIL] 搜索请求失败: {e}")
            return []

    def get_video_info(self, bvid):
        """获取视频详细信息"""
        try:
            resp = self.session.get(API["video_info"], params={"bvid": bvid}, timeout=10)
            data = resp.json()
            if data["code"] == 0:
                return data["data"]
        except:
            pass
        return None

    def get_user_info(self, mid):
        """获取UP主信息"""
        try:
            resp = self.session.get(API["user_info"], params={"mid": mid}, timeout=10)
            data = resp.json()
            if data["code"] == 0:
                return data["data"]
        except:
            pass
        return None

    def post_comment(self, bvid, oid, message):
        """发送评论"""
        csrf = self.config["cookie"]["bili_jct"]
        form_data = {
            "oid": oid,
            "type": 1,       # 1=视频评论
            "message": message,
            "plat": 1,
            "csrf": csrf,
        }
        headers = {
            **HEADERS,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            resp = self.session.post(API["reply_add"], data=form_data, headers=headers, timeout=15)
            data = resp.json()
            return data
        except Exception as e:
            return {"code": -1, "message": str(e)}

    def pin_comment(self, oid, rpid, action=1):
        """置顶/取消置顶评论
        action=1 置顶, action=0 取消置顶
        注意：只有视频所有者（博主）账号才能置顶评论
        """
        csrf = self.config["cookie"]["bili_jct"]
        form_data = {
            "oid": oid,
            "type": 1,
            "rpid": rpid,
            "action": action,
            "csrf": csrf,
        }
        headers = {
            **HEADERS,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            resp = self.session.post(
                "https://api.bilibili.com/x/v2/reply/action",
                data=form_data, headers=headers, timeout=15
            )
            data = resp.json()
            return data
        except Exception as e:
            return {"code": -1, "message": str(e)}

    @staticmethod
    def pin_with_account(oid, rpid, account_dict, action=1):
        """使用指定账号置顶评论（不依赖当前 workflow 实例）
        account_dict: {"SESSDATA": "...", "bili_jct": "...", "buvid3": "...", "buvid4": "..."}
        """
        import requests as req
        import uuid as _uuid
        session = req.Session()
        session.headers.update(HEADERS)
        for k, v in account_dict.items():
            if v:
                session.cookies.set(k, str(v), domain=".bilibili.com")
        # 自动补全 buvid3/buvid4
        if not account_dict.get("buvid3"):
            session.cookies.set("buvid3", "XY" + _uuid.uuid4().hex.upper()[:30], domain=".bilibili.com")
        if not account_dict.get("buvid4"):
            session.cookies.set("buvid4", _uuid.uuid4().hex + "-" + _uuid.uuid4().hex[:8], domain=".bilibili.com")

        csrf = account_dict.get("bili_jct", "")
        form_data = {
            "oid": oid,
            "type": 1,
            "rpid": rpid,
            "action": action,
            "csrf": csrf,
        }
        headers = {
            **HEADERS,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            resp = session.post(
                "https://api.bilibili.com/x/v2/reply/action",
                data=form_data, headers=headers, timeout=15
            )
            return resp.json()
        except Exception as e:
            return {"code": -1, "message": str(e)}

    def get_first_comment(self, aid, mode=3):
        """获取视频第一条评论（按热度排序 mode=3，按时间排序 mode=2）
        返回 {"rpid": ..., "content": "...", "uname": "...", "ctime": ...} 或 None
        """
        params = {
            "oid": aid,
            "type": 1,
            "mode": mode,
            "ps": 1,  # 只取1条
            "next": 0,
        }
        try:
            resp = self.session.get(
                "https://api.bilibili.com/x/v2/reply/main",
                params=params, timeout=10
            )
            data = resp.json()
            if data["code"] == 0 and data["data"].get("replies"):
                first = data["data"]["replies"][0]
                return {
                    "rpid": first["rpid"],
                    "content": first["content"]["message"],
                    "uname": first["member"]["uname"],
                    "ctime": first["ctime"],
                }
            return None
        except Exception as e:
            print(f"  [WARN] 获取首条评论失败: {e}")
            return None

    def filter_videos(self, videos):
        """按规则筛选视频"""
        filtered = []
        cfg = self.config["filter"]

        for v in videos:
            bvid = v.get("bvid", "")
            play = v.get("play", 0)
            mid = v.get("mid", 0)
            author = v.get("author", "未知")
            title = re.sub(r'<[^>]+>', '', v.get("title", ""))

            # 排除已评论
            if cfg["exclude_commented"] and bvid in [h["bvid"] for h in self.history]:
                continue

            # 播放量筛选
            if cfg["min_play"] > 0 and play < cfg["min_play"]:
                continue
            if cfg["max_play"] > 0 and play > cfg["max_play"]:
                continue

            filtered.append({
                "bvid": bvid,
                "aid": v.get("aid", 0),
                "title": title,
                "author": author,
                "mid": mid,
                "play": play,
                "danmaku": v.get("video_review", 0),
                "comment_count": v.get("comment", 0),
                "favorites": v.get("favorites", 0),
                "duration": v.get("duration", ""),
                "pubdate": v.get("pubdate", 0),
                "description": v.get("description", ""),
                "tag": v.get("tag", ""),
                "pic": f"https:{v.get('pic', '')}" if v.get('pic') else "",
            })

        return filtered

    def enrich_video_data(self, videos):
        """补充视频详细信息（UP主粉丝数、认证状态等），使用并行请求加速"""
        if not videos:
            return []

        def fetch_one(v):
            info = self.get_video_info(v["bvid"])
            if info:
                v["aid"] = info.get("aid", v["aid"])
                v["owner_name"] = info.get("owner", {}).get("name", v["author"])
                owner_mid = info.get("owner", {}).get("mid", v["mid"])
                v["mid"] = owner_mid

                user = self.get_user_info(owner_mid)
                if user:
                    v["fans"] = user.get("follower", 0)
                    v["official"] = user.get("official", {}).get("title", "")
                    v["official_type"] = user.get("official", {}).get("type", -1)
                else:
                    v["fans"] = 0
                    v["official"] = ""
                    v["official_type"] = -1
            return v

        enriched = []
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(fetch_one, v): i for i, v in enumerate(videos)}
            for future in as_completed(futures):
                enriched.append((futures[future], future.result()))
        # 保持原始顺序
        enriched.sort(key=lambda x: x[0])
        return [v for _, v in enriched]

    def apply_user_filters(self, videos):
        """应用UP主级别的筛选规则"""
        cfg = self.config["filter"]
        result = []

        for v in videos:
            # 粉丝数筛选
            if cfg["min_fans"] > 0 and v.get("fans", 0) < cfg["min_fans"]:
                continue
            if cfg["max_fans"] > 0 and v.get("fans", 0) > cfg["max_fans"]:
                continue

            # 认证筛选
            if cfg["need_official"] and v.get("official_type", -1) not in [0, 1]:
                continue

            result.append(v)

        return result

    def print_video_list(self, videos):
        """打印视频列表"""
        print("\n" + "=" * 80)
        print(f"  筛选结果: 共 {len(videos)} 个视频")
        print("=" * 80)

        for i, v in enumerate(videos):
            pubdate_str = datetime.fromtimestamp(v.get("pubdate", 0)).strftime("%Y-%m-%d") if v.get("pubdate") else "未知"
            duration = v.get("duration", "未知")
            # 格式化播放量
            play_str = self._format_num(v.get("play", 0))
            fans_str = self._format_num(v.get("fans", 0))
            official = v.get("official", "")
            official_tag = f" [{official}]" if official else ""

            print(f"\n  [{i + 1}] {v['title'][:60]}")
            print(f"      BV号: {v['bvid']}  |  播放: {play_str}  |  UP主: {v['author']}{official_tag}")
            print(f"      粉丝: {fans_str}  |  弹幕: {v.get('danmaku', 0)}  |  评论: {v.get('comment_count', 0)}")
            print(f"      时长: {duration}  |  发布: {pubdate_str}")
            if v.get("tag"):
                print(f"      标签: {v['tag']}")

    @staticmethod
    def _format_num(num):
        """格式化数字"""
        if num >= 10000:
            return f"{num / 10000:.1f}万"
        return str(num)

    def run_interactive(self):
        """交互式运行"""
        print("\n" + "=" * 60)
        print("  B站自动评论工作流")
        print("=" * 60)

        # 1. 验证登录
        print("\n[1/5] 验证登录状态...")
        if not self.check_login():
            print("\n请先配置 Cookie 后再运行！")
            print("获取方法: 浏览器登录B站 → F12 → Application → Cookies → bilibili.com")
            print("需要复制: SESSDATA, bili_jct, buvid3, DedeUserID")
            return

        # 2. 搜索视频
        keyword = self.config["search"]["keyword"]
        tid = self.config["search"]["tid"]
        tid_name = BILI_TIDS.get(tid, "全部")
        print(f"\n[2/5] 搜索视频...")
        print(f"      关键词: {keyword}")
        print(f"      分区: {tid_name} (tid={tid})")
        print(f"      排序: {self.config['search']['order']}")

        raw_videos = self.search_videos()
        if not raw_videos:
            print("  [FAIL] 没有搜索到任何视频，请检查关键词或网络")
            return
        print(f"  [OK] 搜索到 {len(raw_videos)} 个视频")

        # 3. 筛选
        print(f"\n[3/5] 应用筛选规则...")
        filtered = self.filter_videos(raw_videos)
        print(f"  [OK] 初步筛选后剩余 {len(filtered)} 个视频")

        if not filtered:
            print("  没有视频通过筛选，请放宽筛选条件")
            return

        # 4. 补充详细信息（获取UP主粉丝数等）
        print(f"\n[4/5] 获取UP主详细信息（需要一些时间）...")
        enriched = self.enrich_video_data(filtered)
        final = self.apply_user_filters(enriched)
        print(f"  [OK] 最终筛选结果: {len(final)} 个视频")

        if not final:
            print("  没有视频通过最终筛选，请放宽筛选条件")
            return

        # 5. 展示结果 & 交互评论
        self.print_video_list(final)

        print("\n" + "=" * 60)
        print("  [5/5] 输入评论")
        print("=" * 60)
        print("  输入视频序号发送评论，多个序号用逗号分隔（如: 1,3,5）")
        print("  输入 'all' 评论所有视频")
        print("  输入 'q' 退出")
        print("  输入 'list' 重新显示列表")

        while True:
            print()
            choice = input("  >>> ").strip()

            if choice.lower() == 'q':
                print("  已退出")
                break

            if choice.lower() == 'list':
                self.print_video_list(final)
                continue

            # 解析序号
            indexes = []
            if choice.lower() == 'all':
                indexes = list(range(len(final)))
            else:
                try:
                    parts = [x.strip() for x in choice.split(',')]
                    indexes = [int(p) - 1 for p in parts if p.isdigit()]
                    indexes = [i for i in indexes if 0 <= i < len(final)]
                except:
                    print("  输入格式错误，请重新输入")
                    continue

            if not indexes:
                print("  没有选中任何视频")
                continue

            # 输入评论内容
            print(f"\n  已选中 {len(indexes)} 个视频:")
            for idx in indexes:
                v = final[idx]
                print(f"    [{idx + 1}] {v['title'][:50]} (BV: {v['bvid']})")

            comment = input("\n  请输入评论内容: ").strip()
            if not comment:
                print("  评论内容不能为空")
                continue

            # 确认
            print(f"\n  即将对 {len(indexes)} 个视频评论: \"{comment}\"")
            confirm = input("  确认? (y/n): ").strip().lower()
            if confirm != 'y':
                print("  已取消")
                continue

            # 发送评论
            success = 0
            fail = 0
            delay = self.config["comment"]["delay_seconds"]

            for idx in indexes:
                v = final[idx]
                oid = v["aid"]
                print(f"\n  [{idx + 1}] 正在评论: {v['title'][:50]} ...")

                result = self.post_comment(v["bvid"], oid, comment)
                code = result.get("code", -1)

                if code == 0:
                    print(f"       [OK] 评论成功! rpid={result['data'].get('rpid', '?')}")
                    self._save_history(v["bvid"], comment, v["title"])
                    success += 1
                elif code == 12002:
                    print(f"       [SKIP] 评论区已关闭")
                    fail += 1
                elif code == 12005:
                    print(f"       [SKIP] 已评论过相同内容（防重复）")
                    fail += 1
                elif code == -101:
                    print(f"       [FAIL] 账号未登录或Cookie过期")
                    fail += 1
                elif code == -111:
                    print(f"       [FAIL] CSRF校验失败，请检查bili_jct")
                    fail += 1
                else:
                    msg = result.get("message", "未知错误")
                    print(f"       [FAIL] code={code} {msg}")
                    fail += 1

                # 间隔延迟
                if idx != indexes[-1]:
                    print(f"       等待 {delay} 秒...")
                    time.sleep(delay)

            print(f"\n  --- 完成: 成功 {success} 条, 失败 {fail} 条 ---")

            # 询问是否继续
            more = input("\n  继续评论其他视频? (y/n): ").strip().lower()
            if more != 'y':
                print("  已退出")
                break


def main():
    parser = argparse.ArgumentParser(
        description="B站自动评论工作流",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  python bili_workflow.py                          # 自动从 accounts.json 读取活跃账号
  python bili_workflow.py -a abc123def456          # 使用指定账号ID
  python bili_workflow.py --list                   # 列出所有已导入账号
  python bili_workflow.py -k "新品测评" -t 188     # 使用活跃账号搜索科技区
  python bili_workflow.py --legacy                 # 使用旧版 CONFIG 字典配置
        """
    )
    parser.add_argument("-a", "--account", help="指定账号ID（默认使用活跃账号）")
    parser.add_argument("--list", action="store_true", help="列出所有已导入账号")
    parser.add_argument("--legacy", action="store_true", help="使用旧版 CONFIG 字典配置")
    parser.add_argument("-k", "--keyword", help="搜索关键词（覆盖默认配置）")
    parser.add_argument("-t", "--tid", type=int, help="分区ID（覆盖默认配置）")
    args = parser.parse_args()

    # --list 模式
    if args.list:
        accounts = BiliWorkflow.list_accounts()
        if not accounts:
            print("\n  暂无账号，请先在 Web 看板中导入账号\n")
            print("  启动 Web 看板: python bili_server.py")
            return
        print("\n" + "=" * 60)
        print("  已导入账号列表")
        print("=" * 60)
        for i, acc in enumerate(accounts):
            active_mark = " [活跃]" if acc["id"] == _get_active_id() else ""
            print(f"  [{i+1}] {acc.get('nickname', '未命名')} ({acc.get('uname', '')})")
            print(f"      ID: {acc['id']}{active_mark}")
            print(f"      Lv{acc.get('level', 0)}  验证: {acc.get('last_verified', '未验证')}")
        print(f"\n  使用指定账号: python bili_workflow.py -a <账号ID>")
        return

    # --legacy 模式
    if args.legacy:
        workflow = BiliWorkflow(CONFIG)
        cookie = CONFIG["cookie"]
        if not cookie["SESSDATA"] or not cookie["bili_jct"]:
            print("\n  [配置提示] 请先在 CONFIG['cookie'] 中填入 Cookie 信息")
            return
        if not CONFIG["search"]["keyword"] and not args.keyword:
            print("\n  请设置搜索关键词: CONFIG['search']['keyword'] 或使用 -k 参数")
            return
        if args.keyword:
            workflow.config["search"]["keyword"] = args.keyword
        if args.tid is not None:
            workflow.config["search"]["tid"] = args.tid
        workflow.run_interactive()
        return

    # 默认模式：从 accounts.json 读取
    wf, account = BiliWorkflow.from_accounts_json(account_id=args.account)
    if wf is None:
        print(f"\n  [ERROR] {account}")  # account is error message here
        print("\n  获取 Cookie 步骤:")
        print("  1. 浏览器登录 B站 → F12 → Application → Cookies → bilibili.com")
        print("  2. 复制 SESSDATA, bili_jct, buvid3, DedeUserID")
        print("  3. 启动 Web 看板导入: python bili_server.py")
        return

    print(f"\n  使用账号: {account.get('nickname', '未命名')} ({account.get('uname', '')})")
    print(f"  Lv{account.get('level', 0)}  验证: {account.get('last_verified', '未验证')}")

    if not wf.check_login():
        print("\n  Cookie 可能已过期，请在 Web 看板中重新导入")
        return

    if args.keyword:
        wf.config["search"]["keyword"] = args.keyword
    if args.tid is not None:
        wf.config["search"]["tid"] = args.tid

    if not wf.config["search"]["keyword"]:
        print("\n  请设置搜索关键词: 使用 -k 参数 或 在 Web 看板中配置")
        return

    wf.run_interactive()


def _get_active_id(accounts_path="accounts.json"):
    """获取当前活跃账号ID"""
    if not os.path.exists(accounts_path):
        return None
    with open(accounts_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("active")


if __name__ == "__main__":
    main()