# -*- coding: utf-8 -*-
"""paths.py —— pipeline 的路径唯一来源（P2a）。

host 用子进程 env 传"当前语料"的路径；单独手跑时回落到 dufuzhixin 的现布局。

env：
  DSH_CORPUS_LANDING      新稿落点（临时落脚，写稿先写这里）
  DSH_CORPUS_ARCHIVE      产物存档（git 仓，唯一校准源）
  DSH_CORPUS_STATE        状态目录（ledger / 队列 / 快照 / 报告）
  DSH_CORPUS_PATCHES      补丁根目录（batch-N 在下面；缺省 <STATE>/_patches）
  DSH_CORPUS_DOC_PREFIX   稿件文件名前缀（缺省 dufu-）
  DSH_CORPUS_ASR_RAW      原始 ASR 归档目录（缺省 <corpus>/_asr_raw）

注意：corpus_lint.py 的规则仍按 `dufu-BV*.md` 命名写死（P2a 只做路径解耦）。
"""
import os

DSH_HOME = os.path.join(os.path.expanduser("~"), ".dsh")
_CORPUS_HOME = os.environ.get("DSH_CORPUS_HOME") or os.path.join(DSH_HOME, "corpus", "dufuzhixin")

STATE = os.path.abspath(
    os.environ.get("DSH_CORPUS_STATE") or os.path.join(_CORPUS_HOME, "_smoothing_state"))
CORPUS = os.path.dirname(STATE)

LANDING = os.path.abspath(
    os.environ.get("DSH_CORPUS_LANDING")
    or os.environ.get("DSH_CORPUS_MAIN")
    or os.path.join(CORPUS, "memory"))
ARCHIVE = os.path.abspath(
    os.environ.get("DSH_CORPUS_ARCHIVE")
    or os.environ.get("DSH_CORPUS_MIRROR")
    or os.path.join(os.path.expanduser("~"), "Documents", "GitHub", "dufuzhixin", "memory"))

# 旧脚本的口径：MAIN = 写稿处，MIRROR = 另一副本；两库必须逐字节一致（R18）。
MAIN = LANDING
MIRROR = ARCHIVE
MAIN_ROOTS = [LANDING, ARCHIVE]

DOC_PREFIX = os.environ.get("DSH_CORPUS_DOC_PREFIX")
if DOC_PREFIX is None:  # 显式传空串要保留（plain 语料没有前缀），不能 `or` 回落
    DOC_PREFIX = "dufu-"
# P4：人工编号语料（`001 篇名（讲者）.md`）没有 BV 号可当主键，改用正则认稿。
# 设了 DSH_CORPUS_DOC_PATTERN 之后：
#   is_doc(name)  以正则判定是不是稿件
#   stem(name)    稿件 id = 文件名去 .md（`001 篇名（讲者）`）
#   doc_path(id)  = <root>/<id>.md
# 没设时保持旧行为（前缀 + bvid）。
DOC_PATTERN = os.environ.get("DSH_CORPUS_DOC_PATTERN") or ""
_DOC_RE = None
if DOC_PATTERN:
    import re as _re
    try:
        _DOC_RE = _re.compile(DOC_PATTERN)
    except Exception as _e:  # 正则写坏了不炸，退回旧规则
        import sys as _sys
        print("**警告**：DSH_CORPUS_DOC_PATTERN 无效（%s），退回前缀规则" % _e, file=_sys.stderr)
        _DOC_RE = None
# 语料 id：只用来在台账里打戳（排查串库），不参与任何路径计算。
CORPUS_ID = os.environ.get("DSH_CORPUS_ID") or ""
PATCHES = os.path.abspath(
    os.environ.get("DSH_CORPUS_PATCHES") or os.path.join(STATE, "_patches"))
ASR_RAW = os.path.abspath(
    os.environ.get("DSH_CORPUS_ASR_RAW") or os.path.join(CORPUS, "_asr_raw"))
AUDIO_DIR = os.environ.get("DSH_CORPUS_AUDIO") or os.path.join(CORPUS, "_audio")
BACKUP_DIR = os.environ.get("DSH_CORPUS_BACKUP") or os.path.join(CORPUS, "_backups")

# 语料根的守卫标记（corpus_lint R20 用；存档仓里不该有 git 操作）
MAIN_MARKER = "no-git-operations.md"


def is_doc(name):
    """文件名是不是本语料的稿件（P4：有 docPattern 用正则，否则前缀规则）。"""
    if not name or name.startswith(".") or not name.endswith(".md"):
        return False
    if _DOC_RE is not None:
        return bool(_DOC_RE.search(name))
    return name.startswith(DOC_PREFIX)


def stem(name):
    """文件名 → 稿件 id（bvid 语料 = BV 号；docPattern 语料 = 去 .md 的文件名）。"""
    base = name[:-3] if name.lower().endswith(".md") else name
    if _DOC_RE is not None:
        return base
    return base[len(DOC_PREFIX):] if base.startswith(DOC_PREFIX) else base


def doc_name(bvid):
    """稿件 id → 文件名（bvid 语料 = dufu-BVxxx.md；docPattern 语料 = <id>.md）。"""
    if _DOC_RE is not None:
        return "%s.md" % bvid
    return "%s%s.md" % (DOC_PREFIX, bvid)


def doc_path(bvid, root=None):
    return os.path.join(root or LANDING, doc_name(bvid))


def doc(bvid, create_root=None):
    """已在两库之一 → 返回那个路径；都没有 → 返回落点里的目标路径。"""
    for root in MAIN_ROOTS:
        p = doc_path(bvid, root)
        if os.path.exists(p):
            return p
    return doc_path(bvid, create_root or LANDING)


def exists(p):
    return os.path.exists(p)


def doc_by_name(name):
    for root in MAIN_ROOTS:
        p = os.path.join(root, name)
        if os.path.exists(p):
            return p
    return os.path.join(LANDING, name)


def iter_docs(roots=None, pattern=None):
    for root in (roots or MAIN_ROOTS):
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            if name.startswith("."):
                continue
            if not os.path.isfile(os.path.join(root, name)):
                continue
            if _match(name, pattern) if pattern else is_doc(name):
                yield os.path.join(root, name)


def iter_names(pattern=None):
    """两库并集的稿件文件名（同名只出一次，落点优先）。"""
    seen = set()
    for root in MAIN_ROOTS:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            if name in seen or name.startswith("."):
                continue
            if not os.path.isfile(os.path.join(root, name)):
                continue
            if _match(name, pattern) if pattern else is_doc(name):
                seen.add(name)
                yield name


def _match(name, pattern):
    import fnmatch
    return fnmatch.fnmatch(name, pattern)


def doc_count():
    return sum(1 for _ in iter_names())


def state(*parts):
    return os.path.join(STATE, *parts) if parts else STATE


def patches(*parts):
    return os.path.join(PATCHES, *parts) if parts else PATCHES


def batch_dir(n):
    return os.path.join(PATCHES, "batch-%d" % n)


def stale_dir(batch, stamp):
    return os.path.join(PATCHES, "_stale", "batch-%d-%s" % (batch, stamp))


def ensure_dirs():
    for p in (LANDING, ARCHIVE, STATE, PATCHES):
        os.makedirs(p, exist_ok=True)
