# -*- coding: utf-8 -*-
"""
内置浏览器登录 - 使用 Chrome 自定义 Profile 实现持久化独立登录

原理：
  1. 用 subprocess 启动一个独立的 Chrome 实例（使用自定义 user-data-dir）
  2. 该 Chrome 实例拥有完全独立的 Cookie 存储，与外层浏览器互不影响
  3. 用户在独立 Chrome 中登录 B站
  4. 关闭 Chrome 后，本脚本自动读取 Profile 中的 Cookies 数据库
  5. 使用 Windows DPAPI + AES-256-GCM 解密 Cookie 值
  6. 提取 SESSDATA / bili_jct / buvid3 / buvid4 / DedeUserID
  7. 调用 Flask API 保存账号

优势：
  - 独立 Profile，外层浏览器退出登录不影响此处的会话
  - Cookie 持久化保存在 browser_profile 目录中
  - 下次打开内置浏览器时，会话自动恢复（无需重新登录）
  - 全自动提取，无需手动复制粘贴
"""

import os
import sys
import json
import sqlite3
import subprocess
import time
import base64
import shutil
import ctypes
from ctypes import wintypes
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

# ============================================================
#  配置
# ============================================================
SCRIPT_DIR = Path(__file__).parent
PROFILE_DIR = SCRIPT_DIR / "browser_profile"
API_BASE = "http://localhost:5000"

# 常见的 Chrome / Edge 安装路径
BROWSER_PATHS = [
    # Chrome
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"),
    # Edge (Chromium 内核，与 Chrome 使用相同的 Cookie 格式)
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    os.path.expandvars(r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe"),
    os.path.expandvars(r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe"),
]


# ============================================================
#  Windows DPAPI 解密（通过 ctypes 调用 crypt32.dll）
# ============================================================
class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def dpapi_decrypt(encrypted_data):
    """使用 Windows DPAPI 解密数据"""
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    blob_in = DATA_BLOB()
    blob_in.cbData = len(encrypted_data)
    blob_in.pbData = ctypes.cast(
        ctypes.create_string_buffer(encrypted_data, len(encrypted_data)),
        ctypes.POINTER(ctypes.c_char),
    )

    blob_out = DATA_BLOB()

    if crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None, None, None, None,
        0,
        ctypes.byref(blob_out),
    ):
        result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        kernel32.LocalFree(blob_out.pbData)
        return result

    return None


# ============================================================
#  Chrome Cookie 解密
# ============================================================
def get_chrome_aes_key(local_state_path):
    """从 Chrome Local State 文件中提取并解密 AES 主密钥"""
    try:
        with open(local_state_path, "r", encoding="utf-8") as f:
            local_state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None

    encrypted_key_b64 = local_state.get("os_crypt", {}).get("encrypted_key")
    if not encrypted_key_b64:
        return None

    # Base64 解码
    encrypted_key = base64.b64decode(encrypted_key_b64)
    # 去掉 "DPAPI" 前缀（5 字节）
    if encrypted_key[:5] != b"DPAPI":
        return None
    encrypted_key = encrypted_key[5:]

    # DPAPI 解密得到 AES 密钥
    aes_key = dpapi_decrypt(encrypted_key)
    return aes_key


def decrypt_chrome_cookie(encrypted_value, aes_key):
    """
    解密 Chrome Cookie 值
    Chrome v80+ 使用 AES-256-GCM 加密
    格式: "v10" (3 bytes) + nonce (12 bytes) + ciphertext_with_tag

    Chrome 130+ 解密后会在值前面加 32 字节的 integrity hash 前缀，
    需要剥离后才能得到真正的 Cookie 值。
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not encrypted_value or not aes_key:
        return None

    # v10/v11/v20 格式
    if encrypted_value[:3] in (b"v10", b"v11", b"v20"):
        nonce = encrypted_value[3:15]       # 12 字节 nonce
        ciphertext = encrypted_value[15:]    # ciphertext + 16 字节 tag
        try:
            aesgcm = AESGCM(aes_key)
            decrypted = aesgcm.decrypt(nonce, ciphertext, None)
            # Chrome 130+ 在解密后的值前面加了 32 字节 prefix
            # 检测方法：如果解密后长度 > 32 且前几字节不是可打印 ASCII，
            # 则剥离前 32 字节
            if len(decrypted) > 32:
                # 检查前几个字节是否是可打印字符
                first_bytes = decrypted[:8]
                if not all(32 <= b < 127 for b in first_bytes):
                    decrypted = decrypted[32:]
            return decrypted.decode("utf-8", errors="replace")
        except Exception:
            return None

    # 旧版 DPAPI 格式（直接解密）
    return None


def extract_cookies_from_profile(profile_dir):
    """
    从 Chrome/Edge Profile 目录中提取 B站 相关 Cookie
    返回 {"SESSDATA": "...", "bili_jct": "...", ...}
    """
    # 兼容新旧两种 Cookies 路径
    # 新版本 Edge/Chrome: Default/Network/Cookies
    # 旧版本 Chrome:       Default/Cookies
    cookies_db_candidates = [
        profile_dir / "Default" / "Network" / "Cookies",
        profile_dir / "Default" / "Cookies",
    ]
    local_state = profile_dir / "Local State"

    cookies_db = None
    for candidate in cookies_db_candidates:
        if candidate.exists():
            cookies_db = candidate
            break

    if not cookies_db:
        print(f"  [FAIL] Cookies 数据库不存在，已检查:")
        for c in cookies_db_candidates:
            print(f"    - {c}")
        return None

    if not local_state.exists():
        print(f"  [FAIL] Local State 不存在: {local_state}")
        return None

    # 获取 AES 密钥
    aes_key = get_chrome_aes_key(str(local_state))
    if not aes_key:
        print("  [FAIL] 无法获取 AES 解密密钥")
        return None

    # 复制数据库文件（因为 Chrome 可能锁定原文件）
    temp_db = profile_dir / "_cookies_temp.db"
    shutil.copy2(str(cookies_db), str(temp_db))

    try:
        conn = sqlite3.connect(str(temp_db))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # 查询 bilibili.com 域下的关键 Cookie
        target_keys = ["SESSDATA", "bili_jct", "buvid3", "buvid4", "DedeUserID"]
        placeholders = ",".join(["?" for _ in target_keys])

        cursor.execute(
            f"""
            SELECT name, encrypted_value
            FROM cookies
            WHERE host_key LIKE '%.bilibili.com'
              AND name IN ({placeholders})
            ORDER BY creation_utc DESC
            """,
            target_keys,
        )

        rows = cursor.fetchall()
        conn.close()

        # 去重（取每个 name 的最新值）
        result = {}
        for row in rows:
            name = row["name"]
            if name not in result:
                encrypted = row["encrypted_value"]
                decrypted = decrypt_chrome_cookie(encrypted, aes_key)
                if decrypted:
                    result[name] = decrypted

        return result if result else None

    except Exception as e:
        print(f"  [FAIL] 读取 Cookies 数据库出错: {e}")
        return None
    finally:
        if temp_db.exists():
            try:
                temp_db.unlink()
            except:
                pass


# ============================================================
#  Chrome 查找与启动
# ============================================================
def kill_profile_browser_processes(profile_dir):
    """仅杀死占用了指定 Profile 目录的浏览器进程，不影响主浏览器"""
    profile_dir = str(profile_dir).lower()
    killed = 0

    for exe_name in ["msedge.exe", "chrome.exe"]:
        try:
            # 使用 wmic 查找命令行中包含该 profile 目录的进程
            result = subprocess.run(
                ["wmic", "process", "where", f"name='{exe_name}'", "get", "ProcessId,CommandLine", "/format:csv"],
                capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.split("\n"):
                line = line.strip()
                if not line or profile_dir not in line.lower():
                    continue
                # wmic CSV 格式: Node,CommandLine,ProcessId
                # PID 在最后一个逗号之后
                last_comma = line.rfind(",")
                if last_comma < 0:
                    continue
                pid = line[last_comma + 1:].strip().strip('"')
                if pid.isdigit():
                    try:
                        subprocess.run(
                            ["taskkill", "/F", "/PID", pid],
                            capture_output=True, text=True, timeout=5,
                        )
                        killed += 1
                    except:
                        pass
        except:
            pass

    if killed > 0:
        print(f"  [INFO] 已终止 {killed} 个占用 Profile 的进程，释放 Cookie 数据库锁")
        time.sleep(1)
    return killed


def find_browser():
    """查找 Chrome / Edge 可执行文件路径"""
    for path in BROWSER_PATHS:
        if os.path.exists(path):
            return path
    # 尝试通过 where 命令查找
    for cmd in ["chrome", "msedge"]:
        try:
            result = subprocess.run(
                ["where", cmd],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip().split("\n")[0]
        except:
            pass
    return None


def launch_browser(browser_path, profile_dir):
    """启动浏览器独立实例"""
    profile_dir = str(profile_dir)
    os.makedirs(profile_dir, exist_ok=True)

    # 首次启动时，避免首次运行向导
    first_run_marker = os.path.join(profile_dir, "First Run")
    if not os.path.exists(first_run_marker):
        Path(first_run_marker).touch()

    # 关键标志：禁用后台运行，确保关闭窗口后进程完全退出
    cmd = [
        browser_path,
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-features=BackgroundFetch",
        "--new-window",
        "https://www.bilibili.com",
    ]

    print(f"  启动浏览器: {browser_path}")
    print(f"  Profile 目录: {profile_dir}")
    print()

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return proc
    except Exception as e:
        print(f"  [FAIL] 启动浏览器失败: {e}")
        return None


# ============================================================
#  调用 Flask API 保存账号
# ============================================================
def save_account_via_api(cookies):
    """通过 Flask API 保存账号"""
    try:
        data = json.dumps({
            "SESSDATA": cookies.get("SESSDATA", ""),
            "bili_jct": cookies.get("bili_jct", ""),
            "buvid3": cookies.get("buvid3", ""),
            "buvid4": cookies.get("buvid4", ""),
            "nickname": cookies.get("uname", ""),
        }).encode("utf-8")

        req = Request(
            f"{API_BASE}/api/accounts/import",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result
    except URLError as e:
        print(f"  [WARN] 无法连接到 Flask 服务: {e}")
        return None
    except Exception as e:
        print(f"  [WARN] 保存账号失败: {e}")
        return None


# ============================================================
#  主流程
# ============================================================
def main():
    print()
    print("=" * 55)
    print("  内置浏览器登录 - B站账号持久化")
    print("=" * 55)
    print()

    # 1. 查找浏览器
    browser_path = find_browser()
    if not browser_path:
        print("[FAIL] 未找到 Chrome 或 Edge 浏览器")
        print("请安装 Google Chrome 或 Microsoft Edge 后重试")
        print("你也可以手动导入 Cookie：在导入弹窗中粘贴 SESSDATA 和 bili_jct")
        # 写入失败标记
        os.makedirs(PROFILE_DIR, exist_ok=True)
        marker = PROFILE_DIR / "_login_failed"
        marker.write_text("browser_not_found")
        return 1

    browser_name = "Edge" if "edge" in browser_path.lower() else "Chrome"
    print(f"[OK] 找到 {browser_name}: {browser_path}")

    # 2. 检查是否已有 Profile（仅用于提示，不跳过浏览器打开）
    profile_exists = (
        (PROFILE_DIR / "Default" / "Network" / "Cookies").exists() or
        (PROFILE_DIR / "Default" / "Cookies").exists()
    )
    existing_uname = None
    if profile_exists:
        print("[INFO] 检测到已有浏览器 Profile，检查登录状态...")
        cookies = extract_cookies_from_profile(PROFILE_DIR)
        if cookies and cookies.get("SESSDATA") and cookies.get("bili_jct"):
            # 先用现有 Cookie 更新账号（快速恢复），但仍然打开浏览器
            result = save_account_via_api(cookies)
            if result and result.get("ok"):
                existing_uname = result.get("account", {}).get("uname", "?")
                print(f"[OK] 当前已登录: {existing_uname}")
            else:
                print("[INFO] 现有 Cookie 已提取但 API 保存失败，将继续打开浏览器")
        else:
            print("[INFO] 现有 Profile 中未检测到有效登录")

    # 3. 启动内置浏览器（不会影响主浏览器）
    print()
    print("=" * 55)
    print("  正在打开内置浏览器...")
    if existing_uname:
        print(f"  当前已登录: {existing_uname}")
        print("  关闭浏览器窗口即可使用此账号，或重新登录切换账号")
    else:
        print("  请在浏览器中登录 B站，登录完成后关闭浏览器窗口")
    print("  系统将自动提取 Cookie 并保存")
    print("=" * 55)
    print()

    proc = launch_browser(browser_path, PROFILE_DIR)
    if not proc:
        return 1

    # 4. 等待浏览器关闭
    print("  等待浏览器关闭...")
    proc.wait()
    print("  浏览器已关闭")

    # 等待后台进程退出，仅清理仍占用 Profile 的残留进程
    time.sleep(3)
    kill_profile_browser_processes(PROFILE_DIR)
    time.sleep(1)

    # 5. 提取 Cookie（带重试）
    print()
    print("  正在提取 Cookie...")

    cookies = None
    for attempt in range(3):
        cookies = extract_cookies_from_profile(PROFILE_DIR)
        if cookies:
            break
        print(f"  [RETRY] 第 {attempt + 1} 次提取失败，重试中...")
        time.sleep(2)
        kill_profile_browser_processes(PROFILE_DIR)
        time.sleep(1)
    if not cookies:
        print()
        print("[FAIL] 无法自动提取 Cookie")
        print("可能原因：")
        print("  1. 未在浏览器中登录 B站")
        print("  2. Chrome 加密方式不兼容")
        print()
        print("备用方案：重新打开内置浏览器，登录后手动复制 Cookie")
        print("  按 F12 → Application → Cookies → bilibili.com")
        print("  复制 SESSDATA 和 bili_jct 的值")
        # 写入失败标记
        os.makedirs(PROFILE_DIR, exist_ok=True)
        marker = PROFILE_DIR / "_login_failed"
        marker.write_text("extraction_failed")
        return 1

    if not cookies.get("SESSDATA") or not cookies.get("bili_jct"):
        print("[FAIL] 未能提取到 SESSDATA 或 bili_jct")
        print(f"  已提取到的键: {list(cookies.keys())}")
        return 1

    print(f"[OK] 成功提取 Cookie")
    print(f"  SESSDATA: {cookies['SESSDATA'][:8]}...")
    print(f"  bili_jct: {cookies['bili_jct'][:8]}...")
    if cookies.get("buvid3"):
        print(f"  buvid3:   {cookies['buvid3'][:8]}...")
    if cookies.get("DedeUserID"):
        print(f"  UID:      {cookies['DedeUserID']}")

    # 6. 保存到 Flask API
    result = save_account_via_api(cookies)
    if result and result.get("ok"):
        uname = result.get("account", {}).get("uname", "?")
        updated = "已更新" if result.get("updated") else "已导入"
        print(f"[OK] 账号{updated}: {uname}")
        print()
        print("=" * 55)
        print("  登录成功！账号已保存，可以关闭此窗口")
        print("  下次打开内置浏览器时，会话将自动恢复")
        print("=" * 55)
        return 0
    else:
        print("[WARN] 无法自动保存到看板，但 Cookie 已提取")
        print("  请手动复制以下信息到看板的导入弹窗：")
        print(f"  SESSDATA: {cookies.get('SESSDATA', '')}")
        print(f"  bili_jct: {cookies.get('bili_jct', '')}")
        return 1


if __name__ == "__main__":
    sys.exit(main())