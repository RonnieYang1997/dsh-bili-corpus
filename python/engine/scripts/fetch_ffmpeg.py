#!/usr/bin/env python3
"""
把静态 ffmpeg 取到技能自己的 bin/ 目录。

为什么要这个脚本
----------------
识别链路只需要 ffmpeg 做两件事：把 m4a 解成 16k 单声道 WAV、读音频时长。
但 **Windows 上 PATH 里通常没有 ffmpeg**，而此前脚本是去 DSH/AionUi 的会话目录里
"借用"别人的 ffmpeg.exe —— 那是**运行时依赖别人的安装**，对方一删/一搬就断。

现在改成：ffmpeg 属于本技能自己的运行时，装在 `<技能>/bin/` 下，
由本脚本一次性取回（与 `fetch_sensevoice_model.py` 同一套思路）。
查找顺序里 `<技能>/bin/` 优先级最高，PATH 只作为最后兜底。

用法
----
    python scripts/fetch_ffmpeg.py                 # 取到 <技能>/bin/
    python scripts/fetch_ffmpeg.py --check         # 只报告现状，不下载
    python scripts/fetch_ffmpeg.py --force         # 已存在也重下
    python scripts/fetch_ffmpeg.py --dir D:/tools  # 换目标目录

来源（按顺序尝试，任一成功即止）
--------------------------------
  1. https://gh-proxy.com/<BtbN GitHub 地址>      ← 实测最快
  2. https://ghproxy.net/<BtbN GitHub 地址>
  3. https://github.com/BtbN/... （直连）
  4. https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip

**为什么把代理放前面**：2026-10-15 在本机同一时段实测下载带宽（每个 12 秒）：
    gh-proxy.com 转发 BtbN     773 KB/s
    gyan.dev 直连               16 KB/s
    ghproxy.net 转发 BtbN        5 KB/s
    GitHub 直连                  0（连不上）
gyan.dev 按 16 KB/s 下 109MB 要约 **2.5 小时** —— 事实上第一次 setup 就卡在这里。
把代理放前面，同样的包约 2~3 分钟。两者都是 Windows x64 静态构建；
本脚本只从压缩包里取出 `*/bin/ffmpeg.exe`，
其余（文档、DLL、预设）一律不落地。
"""

import argparse
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = SKILL_ROOT / "bin"

_BTBN = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
         "ffmpeg-master-latest-win64-gpl.zip")

# 顺序 = 实测带宽从快到慢（见文件头注释）。代理放前面是因为 gyan.dev 直连
# 实测只有 16 KB/s，下完 109MB 要 2.5 小时。
SOURCES = [
    ("BtbN via gh-proxy.com", "https://gh-proxy.com/" + _BTBN),
    ("BtbN via ghproxy.net", "https://ghproxy.net/" + _BTBN),
    ("BtbN direct", _BTBN),
    ("gyan.dev essentials", "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"),
]

# 压缩包里需要取出的成员（按后缀匹配，忽略顶层版本目录名）
#
# 2026-10-15：**只要 ffmpeg.exe，不再要 ffprobe.exe**。
# ffprobe 有 161MB，而在整个技能里只干一件事 —— 报音频时长；
# 而 `ffmpeg -i <file>` 本来就把 `Duration:` 打在 stderr 上（见 ffprobe_duration）。
# 去掉它，技能自带运行时从 323MB 降到 161MB，下载量减半，也少一个二进制要维护。
WANTED = ("bin/ffmpeg.exe",)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def runs(exe: Path) -> bool:
    """该可执行文件能否真的跑起来。不用管道（R22）。"""
    try:
        r = subprocess.run([str(exe), "-version"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=60)
        return r.returncode == 0
    except Exception:
        return False


def have_all(out: Path) -> bool:
    return all((out / Path(w).name).is_file() and runs(out / Path(w).name) for w in WANTED)


def download(url: str, dest: Path, attempts: int = 3) -> bool:
    """流式下载到 dest（先写 .part，成功再改名）。"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    for i in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=120, context=ctx) as r:
                total = int(r.headers.get("Content-Length") or 0)
                tmp = dest.with_suffix(dest.suffix + ".part")
                got = 0
                last = 0.0
                with open(tmp, "wb") as f:
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        now = time.time()
                        if now - last > 2:
                            last = now
                            pct = ("%3d%%" % (got * 100 / total)) if total else "  ? "
                            print("    %s  %6.1fMB%s  %4.0fs"
                                  % (pct, got / 1048576,
                                     ("/%6.1fMB" % (total / 1048576)) if total else "",
                                     now - t0), flush=True)
                tmp.replace(dest)
            print("    下载完成 %.1fMB  %.0fs" % (dest.stat().st_size / 1048576,
                                                 time.time() - t0), flush=True)
            return True
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            print("    第 %d 次失败：%s" % (i, e), flush=True)
            time.sleep(2 * i)
    return False


def extract(zip_path: Path, out: Path) -> int:
    """只取 WANTED 里的两个 exe。返回取出的个数。"""
    n = 0
    with zipfile.ZipFile(zip_path) as z:
        for member in z.namelist():
            norm = member.replace("\\", "/")
            for want in WANTED:
                if norm.endswith(want) and "/bin/" in norm:
                    target = out / Path(want).name
                    with z.open(member) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst, 1 << 20)
                    print("    取出 %s  (%.1fMB)" % (target.name,
                                                   target.stat().st_size / 1048576))
                    n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="取静态 ffmpeg 到技能自己的 bin/")
    ap.add_argument("--dir", default=str(DEFAULT_DIR), help="目标目录（默认 <技能>/bin）")
    ap.add_argument("--check", action="store_true", help="只报告现状，不下载")
    ap.add_argument("--force", action="store_true", help="已存在也重新下载")
    args = ap.parse_args()

    out = Path(args.dir).expanduser().resolve()
    print("目标目录: %s" % out)

    if have_all(out) and not args.force:
        for w in WANTED:
            p = out / Path(w).name
            print("  已存在且可运行 %s  (%.1fMB)" % (p.name, p.stat().st_size / 1048576))
        return 0
    if args.check:
        for w in WANTED:
            p = out / Path(w).name
            if p.is_file():
                print("  %s 存在但**不能运行**" % p.name if not runs(p) else "  %s ok" % p.name)
            else:
                print("  %s 缺失" % p.name)
        return 1

    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        zpath = Path(td) / "ffmpeg.zip"
        for label, url in SOURCES:
            print("尝试来源: %s" % label)
            print("  %s" % url)
            if not download(url, zpath):
                continue
            try:
                n = extract(zpath, out)
            except zipfile.BadZipFile as e:
                print("    压缩包损坏：%s" % e)
                continue
            if n == len(WANTED):
                break
            print("    只取出 %d/%d 个文件，换下一个来源" % (n, len(WANTED)))
            zpath.unlink(missing_ok=True)

    print()
    if have_all(out):
        for w in WANTED:
            p = out / Path(w).name
            print("  [ok] %s  (%.1fMB)  可运行" % (p, p.stat().st_size / 1048576))
        print("\n完成。ffmpeg 现在是本技能自带的运行时，不再依赖任何外部安装。")
        return 0
    print("  [失败] 没能装好可运行的 ffmpeg。", file=sys.stderr)
    print("         可手动下载 zip 并解出 bin/ffmpeg.exe 放到 %s" % out,
          file=sys.stderr)
    print("""
         如果**每个来源都下载成功、文件大小也对，却仍然"存在但不能运行"**
         （杀软内容级封锁的特征：读都读不了、复制到别处也跑不了），
         绕法是换一个体积小的构建，一条命令：
             %s -m pip install imageio-ffmpeg
        然后把 site-packages 里的 imageio_ffmpeg/binaries/ffmpeg-win-x86_64-*.exe
         复制成 %s\\ffmpeg.exe（gyan.dev 7.1 构建约 84MB，2026-10 实测可用）。
         详见 SKILL.md「ffmpeg 被杀软内容级封锁时的应急换件」。
""" % (sys.executable, out), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
