#!/usr/bin/env python3
"""
B站凭据（cookie）解析与校验 —— **源码里绝不存放 cookie**。

背景（2026-10-15）：本技能的 `bilibili_transcribe.py` 曾在第 21 行硬编码一整条
真实账号 cookie（含 `SESSDATA` / `bili_jct`），等于把账号凭据写进源码、随 git 走。
本模块把凭据抽出来，并提供「先不用 cookie 试、失败再问用户」所需的判定工具。

查找顺序（先命中先用）：
  1. 显式参数        `--cookie "SESSDATA=..."` 或 `--cookie-file PATH`
  2. 环境变量        `BILI_COOKIE` / `BILI_COOKIE_FILE`
  3. 当前目录        `./cookies.txt`
  4. 技能目录        `<skill>/cookies.txt`          （.gitignore 已排除）
  5. 用户配置目录    `~/.config/bilibili-transcribe/cookies.txt`  ← 推荐存放处
                     （Windows 上同时认 `%APPDATA%\\bilibili-transcribe\\cookies.txt`）

cookie 文件的写法（三种都认）：
  - 浏览器复制出来的原始串：`SESSDATA=xxx; bili_jct=yyy; buvid3=zzz`
  - 只写一个值：`SESSDATA=xxx`
  - JSON（浏览器插件导出的 `[{"name":"SESSDATA","value":"xxx"}, ...]`）

只依赖标准库 —— 与技能其余脚本保持一致。
"""

import json
import os
import re
import ssl
import sys
import urllib.parse
import urllib.request

__all__ = [
    "REQUIRED_KEYS", "OPTIONAL_KEYS", "cookie_candidates", "resolve_cookie",
    "save_cookie", "normalize_cookie", "missing_keys", "validate_cookie",
    "cookie_hint", "store_path",
]

# SESSDATA 是"你是谁"；bili_jct 是"写操作令牌"。取流（playurl）只要有 SESSDATA 就够，
# 但保留 bili_jct 以便将来支持收藏/投币类操作。
REQUIRED_KEYS = ("SESSDATA",)
OPTIONAL_KEYS = ("bili_jct", "buvid3", "DedeUserID", "buvid4", "b_nut")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_NAV = "https://api.bilibili.com/x/web-interface/nav"


def store_path():
    """推荐存放处（用户级，不进 git）。"""
    base = os.environ.get("BILI_CONFIG_DIR")
    if not base:
        appdata = os.environ.get("APPDATA")
        if os.name == "nt" and appdata:
            base = os.path.join(appdata, "bilibili-transcribe")
        else:
            base = os.path.join(os.path.expanduser("~"), ".config", "bilibili-transcribe")
    return os.path.join(base, "cookies.txt")


def cookie_candidates(explicit_file=None):
    """按优先级返回候选 cookie 文件路径（不判断存在性）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    skill_root = os.path.dirname(here)
    out = []
    if explicit_file:
        out.append(explicit_file)
    env_file = os.environ.get("BILI_COOKIE_FILE")
    if env_file:
        out.append(env_file)
    out.append(os.path.join(os.getcwd(), "cookies.txt"))
    out.append(os.path.join(skill_root, "cookies.txt"))
    out.append(store_path())
    return out


def normalize_cookie(raw):
    """把各种写法的 cookie 归一成 `k=v; k=v` 串。

    - JSON（插件导出）→ 拼串
    - `SESSDATA=xxx` 单值 → 原样
    - 裸的 SESSDATA 值（形如 `abc%2C123%2Cxyz`）→ 补上键名
    - 多行 / 制表符分隔 → 归一成 `; `
    """
    if not raw:
        return ""
    text = raw.strip()
    if not text:
        return ""

    # JSON 形态
    if text[:1] in "[{":
        try:
            data = json.loads(text)
            items = data if isinstance(data, list) else data.get("cookies", [])
            pairs = [(str(d.get("name", "")).strip(), str(d.get("value", "")).strip())
                     for d in items if isinstance(d, dict)]
            pairs = [(k, v) for k, v in pairs if k]
            if pairs:
                return "; ".join("%s=%s" % (k, v) for k, v in pairs)
        except Exception:
            pass

    # 多行 → 分号（浏览器"复制为 cURL"或逐行粘贴）
    if "\n" in text or "\r" in text:
        text = "; ".join(p.strip() for p in text.splitlines() if p.strip())

    # 裸值：没有 '=' 但像 SESSDATA（含 %2C 逗号编码或足够长且无空格）
    if "=" not in text and re.fullmatch(r"[A-Za-z0-9%_\-*,]{16,}", text):
        return "SESSDATA=" + text

    # 单行但缺分号分隔（`a=1 b=2`）——只在看起来是 k=v 序列时规整
    if ";" not in text and re.search(r"\s+[A-Za-z_][A-Za-z0-9_]*=", text):
        text = re.sub(r"\s+(?=[A-Za-z_][A-Za-z0-9_]*=)", "; ", text)

    return text.strip().strip(";").strip()


def resolve_cookie(explicit=None, cookie_file=None, quiet=True):
    """解析 cookie。找不到返回空串（调用方据此决定"免 cookie 试跑"或"问用户"）。

    `explicit` 既可以是完整 cookie 串，也可以是 `SESSDATA=...` 单值。
    """
    if explicit:
        c = normalize_cookie(explicit)
        if c:
            return c

    for path in cookie_candidates(cookie_file):
        try:
            if path and os.path.isfile(path):
                with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
                    c = normalize_cookie(f.read())
                if c:
                    if not quiet:
                        print("[cookie] 取自 %s" % path)
                    return c
        except OSError:
            continue

    c = normalize_cookie(os.environ.get("BILI_COOKIE", ""))
    return c


def missing_keys(cookie):
    """返回缺失的必需键名列表。"""
    have = set()
    for part in (cookie or "").split(";"):
        if "=" in part:
            have.add(part.split("=", 1)[0].strip())
    return [k for k in REQUIRED_KEYS if k not in have]


def _http_json(url, cookie="", timeout=15):
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Referer": "https://www.bilibili.com/",
        **({"Cookie": cookie} if cookie else {}),
    })
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def validate_cookie(cookie):
    """调 nav 接口验证 cookie 是否有效。

    返回 {"ok": bool, "code": int, "uname": str, "mid": int, "reason": str}。
    注意：**空 cookie 不是错误** —— B站公开视频低清档位无需登录，
    所以 `ok=False, reason="no-cookie"` 时应当"先免 cookie 试跑"，而不是直接报错。
    """
    if not cookie:
        return {"ok": False, "code": None, "uname": "", "mid": 0, "reason": "no-cookie"}
    miss = missing_keys(cookie)
    if miss:
        return {"ok": False, "code": None, "uname": "", "mid": 0,
                "reason": "missing-key:" + ",".join(miss)}
    try:
        j = _http_json(_NAV, cookie)
    except Exception as e:
        return {"ok": False, "code": None, "uname": "", "mid": 0,
                "reason": "network:%s:%s" % (type(e).__name__, e)}
    code = j.get("code")
    data = j.get("data") or {}
    if code == 0 and data.get("isLogin"):
        return {"ok": True, "code": 0, "uname": data.get("uname", ""),
                "mid": int(data.get("mid") or 0), "reason": ""}
    if code == -101:
        return {"ok": False, "code": code, "uname": "", "mid": 0,
                "reason": "SESSDATA 已失效或过期（-101 账号未登录）"}
    return {"ok": False, "code": code, "uname": "", "mid": 0,
            "reason": "nav 返回 code=%s message=%s" % (code, j.get("message"))}


def save_cookie(raw, path=None):
    """把 cookie 写到用户级位置（不进 git）。返回写入路径。"""
    path = path or store_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = normalize_cookie(raw)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text + "\n")
    try:  # 尽量收紧权限（Windows 上语义有限，Linux/macOS 有效）
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def cookie_hint():
    """给用户看的取 cookie 指引（纯文本，供 CLI 打印）。"""
    return (
        "获取 B站 cookie（只需要 SESSDATA 一项）：\n"
        "  1. 浏览器登录 bilibili.com\n"
        "  2. F12 → Application/存储 → Cookies → https://www.bilibili.com\n"
        "  3. 复制 SESSDATA 的 Value\n"
        "  4. 任选一种交给我：\n"
        "     · 直接粘贴（形如 SESSDATA=xxxx%2Cyyyy%2Czzzz）\n"
        "     · 写进文件：%s\n"
        "     · 或设环境变量 BILI_COOKIE\n"
        "说明：**公开视频的低清档位不需要 cookie**，本技能会先不带 cookie 试跑，\n"
        "      只在需要登录（高码率/风控/充电内容）时才向你索取。"
        % store_path()
    )


def _selftest():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cases = [
        ("SESSDATA=abc%2C123%2Cxyz", "SESSDATA=abc%2C123%2Cxyz"),
        ("abc%2C123%2Cxyzdefghijklmn", "SESSDATA=abc%2C123%2Cxyzdefghijklmn"),
        ('[{"name":"SESSDATA","value":"v1"},{"name":"bili_jct","value":"v2"}]',
         "SESSDATA=v1; bili_jct=v2"),
        ("SESSDATA=v1\nbili_jct=v2", "SESSDATA=v1; bili_jct=v2"),
        ("SESSDATA=v1;bili_jct=v2", "SESSDATA=v1;bili_jct=v2"),
        ("", ""),
    ]
    bad = 0
    for src, want in cases:
        got = normalize_cookie(src)
        flag = "ok " if got == want else "FAIL"
        if got != want:
            bad += 1
        print("  %s  %r -> %r" % (flag, src[:40], got[:60]))
    print("  missing_keys('SESSDATA=v') =", missing_keys("SESSDATA=v"))
    print("  missing_keys('buvid3=v')   =", missing_keys("buvid3=v"))
    print("  validate_cookie('')        =", validate_cookie(""))
    print("SELFTEST", "PASS" if bad == 0 else "FAIL(%d)" % bad)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(_selftest())
