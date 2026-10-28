import sys, os, re, sqlite3, threading, time, concurrent.futures
from datetime import datetime
from openai import OpenAI
from collections import defaultdict

import config


# ── 路径 ──────────────────────────────────────────────────────────────
def get_base_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = get_base_dir()
DB_PATH = os.environ.get('DB_PATH', os.path.join(BASE_DIR, "disk_scan.db"))

# ── API 配置：全部来自用户自己的配置（BYOK）────────────────────────────
_CFG = config.load()
API_URL = _CFG.get("base_url") or ""
API_KEY = _CFG.get("api_key") or ""
MODEL = _CFG.get("model_analyze") or ""
MIN_MB = int(_CFG.get("min_mb") or 100)

TEMP = 0.1
# 只下发 temperature，不下发 top_p —— Anthropic 系模型两者同时传会直接 400。
MAX_TOKENS = 8192

# 单批送给模型的字符上限。越大越快，但一次失败的代价也越高。
MAX_CHARS = 12000
# 并发批次数。中转站限流严格时调到 1。
MAX_WORKERS = 3

# ─────────────────────────────────────────────────────────────────────

_PREFETCH_CACHE: dict[str, int] | None = None  # exe名(小写,无扩展名) → 距今天数


def get_prefetch() -> dict[str, int]:
    global _PREFETCH_CACHE
    if _PREFETCH_CACHE is not None:
        return _PREFETCH_CACHE

    result: dict[str, int] = {}
    pf_dir = r"C:\Windows\Prefetch"
    try:
        now = datetime.now()
        for fname in os.listdir(pf_dir):
            if not fname.upper().endswith(".PF"):
                continue
            # 文件名格式：EXENAME-HASH.pf，取第一个 '-' 前的部分
            exe_name = fname.split("-")[0].lower()
            # 去掉 .exe 后缀（部分条目有，部分没有）
            if exe_name.endswith(".exe"):
                exe_name = exe_name[:-4]
            try:
                mtime = os.path.getmtime(os.path.join(pf_dir, fname))
                days = (now - datetime.fromtimestamp(mtime)).days
                # 同名取最近的那条
                if exe_name not in result or days < result[exe_name]:
                    result[exe_name] = days
            except Exception:
                pass
    except Exception:
        pass

    _PREFETCH_CACHE = result
    return result


def prefetch_label(path: str) -> str:
    """
    根据文件名在 Prefetch 里查找最近运行记录。
    返回：'近期运行(N天前)' / '半年内运行' / '超过半年未运行' / '无运行记录'
    """
    pf = get_prefetch()
    if not pf:
        return "无Prefetch数据"

    exe_name = os.path.splitext(os.path.basename(path).lower())[0]
    days = pf.get(exe_name)
    if days is None:
        return "无运行记录"
    if days <= 30:
        return f"近期运行({days}天前)"
    if days <= 180:
        return f"半年内运行({days}天前)"
    return f"超过半年未运行({days}天前)"


# ─────────────────────────────────────────────────────────────────────
# 第一道墙：硬编码过滤（直接短路，不送 AI）
# ─────────────────────────────────────────────────────────────────────

BLOCK_PATH = [
    r"\windows\system32", r"\windows\syswow64", r"\windows\winsxs",
    r"\windows\servicing", r"\windows\boot",
    r"\microsoft\crypto",
    r"\login data", r"\cookies", r"\local state", r"\web data",
    r"\wechat files", r"\weixin files", r"\tencent files",
    r"\dingtalk\\", r"\feishu\\", r"\lark\\",
    r"\virtualbox vms", r"\vmware", r"\hyperv",
    r"\packages\canonicalgrouplimited", r"\wsl",
]

BLOCK_EXT = {".pem", ".key", ".crt", ".pfx", ".p12", ".cer",
             ".vhd", ".vhdx", ".vmdk", ".qcow2", ".reg"}

BLOCK_NAME = {
    "login data", "login data-journal", "cookies", "cookies-journal",
    "web data", "history", "local state",
    "key3.db", "key4.db", "logins.json", "cert9.db", "signons.sqlite",
}


# 改后
USER_CONTENT_PATHS = [
    r"\desktop\\", r"\桌面\\", r"\documents\\", r"\文档\\",
    r"\pictures\\", r"\图片\\", r"\videos\\", r"\视频\\",
    r"\music\\", r"\音乐\\", r"\onedrive\\",
]

def hard_verdict(row: dict) -> str | None:
    if row.get("is_running", 0):
        return "❌ 不要删"
    path = norm(row.get("path", "")).lower()
    ext = (row.get("ext", "") or "").lower()
    fname = (row.get("filename", "") or "").lower()
    if ext in BLOCK_EXT:
        return "❌ 不要删"
    if fname in BLOCK_NAME:
        return "❌ 不要删"
    for seg in BLOCK_PATH:
        if seg in path:
            return "❌ 不要删"

    # 用户内容目录：强制需确认，不交给 AI 判断
    for seg in USER_CONTENT_PATHS:
        if seg in path:
            return "⚠️ 需确认"

    # SDK / 工具链库文件：删了编译环境就坏，且不会"自动重建"。
    # 换 claude-haiku 后实测会把 CUDA 的 *.lib 误判成"可重建"，这里硬拦。
    if ext in (".lib", ".a", ".so") and ("\\lib\\" in path or "\\include\\" in path):
        return "⚠️ 需确认"

    # 7天内访问过：强制需确认
    days = row.get("days_since_access", -1)
    if isinstance(days, (int, float)) and 0 <= days <= 7:
        return "⚠️ 需确认"

    # AppData\Roaming 下的 exe（非安装包）：强制需确认
    if ext == ".exe" and r"\appdata\roaming" in path:
        name = fname.replace(".exe", "")
        if not any(k in name for k in ("install", "setup", "update", "uninstall")):
            return "⚠️ 需确认"

    return None


# ─────────────────────────────────────────────────────────────────────
# siblings 优化：从原始字符串提取干净的文件名列表
# 扫描阶段已经存了"文件名, 文件名, ..."格式，这里做二次清洗
# 同时补充目录角色推断，让 AI 有更清晰的上下文
# ─────────────────────────────────────────────────────────────────────

# 有语义价值的扩展名，优先展示
_PRIORITY_EXTS = {
    '.exe', '.dll', '.asar', '.json', '.ini', '.cfg', '.py',
    '.bat', '.sh', '.ps1', '.yaml', '.toml', '.xml', '.plist',
}


def format_siblings(raw_siblings: str, target_path: str) -> str:
    """
    把扫描阶段存的 siblings 字符串重新整理：
    - 有语义的扩展名优先
    - 最多展示 10 个
    - 附加目录角色推断
    """
    if not raw_siblings or raw_siblings in ("(空目录)", "(无法读取)"):
        return raw_siblings or "(空目录)"

    items = [s.strip() for s in raw_siblings.split(",") if s.strip()]

    # 过滤掉"共N项"这种统计文字
    items = [i for i in items if not i.startswith("共") and not i.endswith("项")]

    priority = [i for i in items if os.path.splitext(i)[1].lower() in _PRIORITY_EXTS]
    others = [i for i in items if i not in priority]
    sample = (priority + others)[:10]

    # 推断目录角色
    names_lower = {i.lower() for i in items}
    role = _infer_dir_role(names_lower, target_path)

    result = ", ".join(sample)
    if role:
        result += f" | 目录角色={role}"
    return result


def _infer_dir_role(names: set[str], path: str) -> str:
    """
    根据同目录文件名组合推断目录角色。
    返回简短描述，供 AI 直接使用。
    """
    p = path.lower()

    # Electron 应用目录
    if "app.asar" in names or ("electron.exe" in names and "resources" in p):
        return "Electron应用根目录"

    # Python 包目录
    if "__init__.py" in names or "setup.py" in names or "pyproject.toml" in names:
        return "Python包目录"

    # Node.js 项目
    if "package.json" in names and "node_modules" in p:
        return "Node模块目录"

    # 安装目录特征（有卸载程序）
    uninstallers = {n for n in names if "uninstall" in n.lower() or "uninst" in n.lower()}
    if uninstallers:
        return "应用安装目录(含卸载程序)"

    # JetBrains / IDE 缓存
    if any("jetbrains" in p or "pycharm" in p or "idea" in p for _ in [1]):
        if any(n.endswith(".storage") or n.endswith(".dat") for n in names):
            return "IDE缓存目录"

    # HuggingFace / AI 模型缓存
    if "huggingface" in p or "modelscope" in p or "paddlehub" in p:
        return "AI模型缓存目录"

    # 浏览器缓存
    if any(k in p for k in ["\\cache\\", "\\gpucache\\", "\\code cache\\"]):
        return "浏览器缓存目录"

    # Gradle / Maven 构建缓存
    if ".gradle" in p or ".m2" in p:
        return "Java构建缓存目录"

    return ""


# ─────────────────────────────────────────────────────────────────────
# Prompt：两阶段推理，先置信度再结论
# ─────────────────────────────────────────────────────────────────────
PROMPT = r"""你是一名有10年经验的系统工程师，正在帮用户清理 Windows 硬盘。

对每个文件，按以下两步推理后再给结论：

【第一步：理解这是什么】
- 这个文件属于哪个软件/系统？它的功能是什么？
- 参考：路径结构、文件名、扩展名、同目录文件（siblings）
- 特别注意 siblings 中出现的 uninstall.exe / app.asar / electron.exe / package.json 等，
  它们是判断"这是应用本体还是缓存"的关键证据
- siblings 中有 .exe / .dll 且当前文件是 .bin / .dat → 说明是程序资源而非缓存

【第二步：评估删除风险】
问自己：现在删掉，有哪个软件或用户会发现它不见了？

  有以下特征 → 高风险，输出 [⚠️ 需确认]：
  · 目录角色=Electron应用根目录 / 应用安装目录
  · install_status=installed 且 path_kind=app_or_resource_like → 直接输出 [⚠️ 需确认]，不需要其他证据
  · 同目录有 uninstall.exe（说明这是装好的应用，不是缓存）
  · path_kind=app_or_resource_like 且路径在 Program Files / Program Files (x86) 下 → 输出 [⚠️ 需确认]，用途写"程序运行组件"
  · prefetch=近期运行(N天前) 且文件是该应用的组件
  · 路径在 Desktop / 桌面 / OneDrive / Documents / 文档 下 → 输出 [⚠️ 需确认]，用途写"用户个人文件"
  · 不确定删了会不会坏软件

  有以下特征 → 低风险，输出 [✅ 可删除]：
  · 典型缓存路径（cache/tmp/temp/gpucache）且可自动重建
  · AI模型缓存目录，重新运行程序可自动下载
  · 压缩包/安装包，原软件已正常安装，包本身无用
  · days_since_access > 365 且无任何程序依赖迹象
  · install_status=not_found 且路径特征明显是废弃残留

  置信度低（特征矛盾或信息不足）→ 一律输出 [⚠️ 需确认]，不要强行猜测

特殊规则：
- hard_blocked=1 → 必须输出 [❌ 不要删]，但仍需正常描述文件用途和删除后果，格式与其他文件完全相同
- 含 backup / 备份 字样 → 后果必须写"删除不可恢复，请确认"
- prefetch 字段仅对 .exe 文件有意义，其他类型忽略此字段
- path_kind=app_or_resource_like 且 install_status=installed → 必须输出 [⚠️ 需确认]

【输出格式（每文件一行，只输出结果行，不输出推理过程）】

[标签] [用途说明≤30字 | 后果≤15字] 完整路径

示例：
[✅ 可删除] [Chrome GPU缓存，长期未使用 | 程序自动重建] C:\\...\\GPUCache\\data_0
[⚠️ 需确认] [MineContext应用核心包，软件依赖 | 删除软件失效] C:\\Program Files\\MineContext\\resources\\app.asar
[⚠️ 需确认] [项目备份文件 | 删除不可恢复，请确认] D:\\backups\\project_2024.zip
[❌ 不要删] [系统级文件，禁止删除 | 影响系统稳定] C:\\Windows\\...
"""


# ─────────────────────────────────────────────────────────────────────
# 数据库
# ─────────────────────────────────────────────────────────────────────

def init_db():
    with sqlite3.connect(DB_PATH) as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS analysis (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                analyzed_at TEXT NOT NULL,
                path        TEXT NOT NULL UNIQUE,
                label       TEXT NOT NULL,
                reason      TEXT,
                size_bytes  INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_analysis_label ON analysis(label);

            CREATE TABLE IF NOT EXISTS app_suggestions (
                app_root         TEXT PRIMARY KEY,
                display_name     TEXT,
                total_size_bytes INTEGER,
                file_count       INTEGER,
                deletable_count  INTEGER,
                suggestion       TEXT,
                created_at       TEXT
            );

            CREATE TABLE IF NOT EXISTS scan_status (
                key TEXT PRIMARY KEY, value TEXT
            );
        """)

def norm(path: str) -> str:
    return os.path.normpath(path.strip())


def load_rows() -> list[dict]:
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        raw = c.execute("""
            SELECT e.* FROM entries e
            WHERE e.size_bytes >= ?
              AND e.path NOT IN (SELECT path FROM analysis)
            ORDER BY e.size_bytes DESC
        """, (MIN_MB * 1024 * 1024,)).fetchall()

    return [dict(r) for r in raw]


def save(records: list[dict]):
    if not records:
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_PATH) as c:
        c.executemany(
            "INSERT OR REPLACE INTO analysis (analyzed_at,path,label,reason,size_bytes) VALUES (?,?,?,?,?)",
            [(now, norm(r["path"]), r["label"], r.get("reason", ""), r.get("size_bytes", 0)) for r in records]
        )

# 在 save() 函数结束后，紧接着加入这个新函数（原文件没有这个函数，纯新增）

REBUILD_NOTES = [
    (r"ace studio\mlaudio",         "程序自动重建", "需重新安装 ACE Studio"),
    (r"ace studio\mlaudio",         "可自动重建",   "需重新安装 ACE Studio"),
    (r"ms-playwright",              "程序自动重建", "需运行 playwright install 重建"),
    (r"ms-playwright",              "可自动重建",   "需运行 playwright install 重建"),
    (r"adobe\plug-ins",             "程序自动重建", "删除后 RAW 格式将无法在 Adobe 中打开"),
    (r"adobe\plug-ins",             "可自动重建",   "删除后 RAW 格式将无法在 Adobe 中打开"),
]

def patch_reason(records: list[dict]) -> list[dict]:
    for r in records:
        p = r.get("path", "").lower()
        for path_seg, old_text, new_text in REBUILD_NOTES:
            if path_seg in p and old_text in r.get("reason", ""):
                r["reason"] = r["reason"].replace(old_text, new_text)
    return records

def set_status(progress: int, text: str):
    try:
        with sqlite3.connect(DB_PATH) as c:
            c.execute("INSERT OR REPLACE INTO scan_status VALUES ('progress',?)", (str(progress),))
            c.execute("INSERT OR REPLACE INTO scan_status VALUES ('stage_text',?)", (text,))
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────
# 批次构建
# ─────────────────────────────────────────────────────────────────────

def days_label(d: int) -> str:
    if d < 0:    return "时间未知"
    if d == 0:   return "今天使用过"
    if d <= 7:   return "近期使用过"
    if d <= 30:  return "本月内使用过"
    if d <= 180: return "数月未使用"
    if d <= 365: return "半年以上未使用"
    return "长期未使用"


def row_to_line(r: dict) -> str:
    mb = r["size_bytes"] / (1024 * 1024)
    size = f"{mb / 1024:.1f}GB" if mb >= 1024 else f"{mb:.0f}MB"
    blocked = " | hard_blocked=1" if r.get("hard_blocked") else ""

    # siblings 二次整理
    siblings_fmt = format_siblings(r.get("siblings", ""), r.get("path", ""))

    # Prefetch 只对 exe 有意义
    ext = (r.get("ext", "") or "").lower()
    pf_info = ""
    if ext == ".exe":
        pf_info = f" | prefetch={prefetch_label(r['path'])}"

    return (
        f"path={norm(r['path'])} | ext={ext or '(none)'} | size={size} | "
        f"kind={r['path_kind']} | last_access={days_label(r['days_since_access'])} | "
        f"install_status={r['install_status']} | running={r['is_running']} | "
        f"siblings=[{siblings_fmt}]{pf_info}{blocked}"
    )


def build_batch_text(rows: list[dict]) -> str:
    return "文件列表：\n" + "\n".join(row_to_line(r) for r in rows)

def interleave_by_size(rows: list[dict]) -> list[dict]:
    """按大小区间分层，轮流取样，使每批大小分布均匀"""
    MB = 1024 * 1024
    breakpoints = [
        100  * MB,   # 100MB
        500  * MB,   # 500MB
        2048 * MB,   # 2GB
        10240 * MB,  # 10GB
        # 10GB以上 → 最后一桶
    ]
    groups: list[list[dict]] = [[] for _ in range(len(breakpoints) + 1)]
    for r in rows:
        s = r["size_bytes"]
        for i, bp in enumerate(breakpoints):
            if s < bp:
                groups[i].append(r)
                break
        else:
            groups[-1].append(r)

    # 每桶内部按大小降序，优先分析桶内较大的
    for g in groups:
        g.sort(key=lambda r: r["size_bytes"], reverse=True)

    result = []
    while any(groups):
        for g in groups:
            if g:
                result.append(g.pop(0))
    return result

def split_batches(rows: list[dict]) -> list[list[dict]]:
    rows = interleave_by_size(rows)          # ← 均匀抽样后再分批
    batches, cur, cur_len = [], [], 0
    for r in rows:
        line = row_to_line(r)
        if cur and cur_len + len(line) > MAX_CHARS:
            batches.append(cur)
            cur, cur_len = [r], len(line)
        else:
            cur.append(r)
            cur_len += len(line)
    if cur:
        batches.append(cur)
    return batches

# ─────────────────────────────────────────────────────────────────────
# AI 调用 + 解析
# ─────────────────────────────────────────────────────────────────────

LABEL_RE = re.compile(r'^\[(✅ 可删除|⚠️ 需确认|❌ 不要删)\]\s+\[(.+?)\]\s+(.+)$')


def parse(text: str, rows: list[dict]) -> list[dict]:
    size_map = {norm(r["path"]): r["size_bytes"] for r in rows}
    lower_map = {p.lower(): p for p in size_map}
    results = []
    for line in text.splitlines():
        m = LABEL_RE.match(line.strip())
        if not m:
            continue
        label, reason, raw_path = m.group(1), m.group(2).strip(), m.group(3).strip()
        path = norm(raw_path)
        if path not in size_map:
            path = lower_map.get(path.lower(), path)
        results.append({"label": label, "path": path, "reason": reason,
                        "size_bytes": size_map.get(path, 0)})
    return results


def _retryable(e: str) -> bool:
    return any(k in e.lower() for k in [
        "sensitive", "limit", "context length", "rate limit",
        "timeout", "timed out", "connection", "empty", "bad gateway",
        "overloaded", "internal", "529", "500", "502", "503",
    ])


def call_api(client: OpenAI, rows: list[dict]) -> str:
    for attempt in range(1, 4):
        try:
            stream = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "system", "content": PROMPT},
                          {"role": "user", "content": build_batch_text(rows)}],
                stream=True, timeout=120, temperature=TEMP, max_tokens=MAX_TOKENS,
            )
            text = ""
            for chunk in stream:
                if getattr(chunk, "choices", None):
                    text += getattr(chunk.choices[0].delta, "content", "") or ""
            if not text.strip():
                raise RuntimeError("模型返回为空")
            return text
        except Exception as e:
            if attempt < 3:
                time.sleep(5 * attempt)
            else:
                raise RuntimeError(f"重试3次均失败: {e}")


def analyze(client: OpenAI, rows: list[dict]):
    """递归二分，确保每个文件都有结论。"""
    if not rows:
        return
    try:
        text = call_api(client, rows)
        parsed = parse(text, rows)

        if not parsed or "sensitive" in text.lower() or "敏感" in text:
            raise ValueError("解析失败或内容敏感")

        save(patch_reason(parsed))

        parsed_paths = {norm(p["path"]) for p in parsed}
        missed = [r for r in rows if norm(r["path"]) not in parsed_paths]
        if missed and len(missed) < len(rows):
            analyze(client, missed)
        elif missed:
            save([{
                "path": r["path"],
                "label": r.get("label", "⚠️ 需确认"),  # hard_blocked 的保留原标签
                "reason": "AI未返回结果，需人工确认" if not r.get("hard_blocked") else "系统级/敏感文件，禁止删除",
                "size_bytes": r.get("size_bytes", 0)
            } for r in missed])

    except Exception as e:
        if len(rows) > 1 and (len(rows) <= 2 or _retryable(str(e))):
            mid = len(rows) // 2
            analyze(client, rows[:mid])
            analyze(client, rows[mid:])
        else:
            save([{"path": r["path"], "label": "⚠️ 需确认",
                   "reason": "AI分析失败，需人工确认",
                   "size_bytes": r.get("size_bytes", 0)} for r in rows])


# ─────────────────────────────────────────────────────────────────────
# 后处理：一致性校验 + 应用聚合
# ─────────────────────────────────────────────────────────────────────

def consistency_check():
    """
    同目录+同扩展名组内，若可删除数 ≤ 不可删除数，则降级。
    纯本地规则，不调用模型。
    """
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute("""
            SELECT a.path, a.label, e.parent_dir, e.ext
            FROM analysis a JOIN entries e ON a.path = e.path
        """).fetchall()

    groups = defaultdict(list)
    for path, label, pdir, ext in rows:
        groups[(os.path.normpath(pdir or "").lower(), (ext or "").lower())].append((path, label))

    downgrade = []
    for items in groups.values():
        if len(items) < 2:
            continue
        deletable = [p for p, l in items if l == "✅ 可删除"]
        non_deletable = [p for p, l in items if l != "✅ 可删除"]
        if 0 < len(deletable) <= len(non_deletable):
            downgrade.extend(deletable)

    if not downgrade:
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_PATH) as c:
        c.executemany("""
            UPDATE analysis SET label='⚠️ 需确认',
            reason=reason||'（一致性校验降级）', analyzed_at=?
            WHERE path=? AND label='✅ 可删除'
        """, [(now, p) for p in downgrade])


APP_PATTERNS = [
    r"^([A-Za-z]:\\Program Files(?: \(x86\))?\\[^\\]+\\[^\\]+)",
    r"^([A-Za-z]:\\Program Files(?: \(x86\))?\\[^\\]+)",
    r"^([A-Za-z]:\\ProgramData\\[^\\]+)",
    r"^([A-Za-z]:\\Games\\[^\\]+)",
    r"^([A-Za-z]:\\SteamLibrary\\steamapps\\common\\[^\\]+)",
    r"^([A-Za-z]:\\Steam\\steamapps\\common\\[^\\]+)",
]


def _app_root(path: str) -> str | None:
    for p in APP_PATTERNS:
        m = re.match(p, path, re.IGNORECASE)
        if m:
            return os.path.normpath(m.group(1)).lower()
    return None


def _display_name(root: str) -> str:
    skip = {"program files", "program files (x86)", "programdata",
            "games", "steamlibrary", "steam", "steamapps", "common"}
    parts = [p for p in os.path.normpath(root).split(os.sep)
             if p.lower() not in skip and len(p) > 1 and p[1:] != ":"]
    return " › ".join(parts) or root


def app_aggregation():
    with sqlite3.connect(DB_PATH) as c:
        c.execute("DELETE FROM app_suggestions")
        rows = c.execute("SELECT path,label,size_bytes FROM analysis").fetchall()

    groups = defaultdict(list)
    for path, label, size in rows:
        root = _app_root(path)
        if root:
            groups[root].append((path, label, size or 0))

    suggestions, now = [], datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for root, items in groups.items():
        if len(items) < 3:
            continue
        total = sum(s for _, _, s in items)
        deletable = [x for x in items if x[1] == "✅ 可删除"]
        keep = [x for x in items if x[1] == "❌ 不要删"]
        if keep or len(deletable) / len(items) < 0.75 or total < 200 * 1024 * 1024:
            continue
        suggestions.append((root, _display_name(root), total, len(items), len(deletable),
                            "建议通过系统设置卸载整个应用，而非逐文件删除", now))

    if not suggestions:
        return
    with sqlite3.connect(DB_PATH) as c:
        c.executemany("""
            INSERT OR REPLACE INTO app_suggestions
            (app_root,display_name,total_size_bytes,file_count,deletable_count,suggestion,created_at)
            VALUES (?,?,?,?,?,?,?)
        """, suggestions)

def rule_patch():
    """
    规则补丁：
    1. 路径含 backup/backups → ⚠️ 需确认
    2. app_or_resource_like 路径下的 .jar → ⚠️ 需确认
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_PATH) as c:
        # 规则1：路径含 \backup\ 或 \backups\
        c.execute("""
            UPDATE analysis
            SET label       = '⚠️ 需确认',
                reason      = reason || '（路径含backup，删除不可恢复）',
                analyzed_at = ?
            WHERE label = '✅ 可删除'
              AND (LOWER(path) LIKE '%\\backup\\%'
                OR LOWER(path) LIKE '%\\backups\\%'
                OR LOWER(path) LIKE '%/backup/%'
                OR LOWER(path) LIKE '%/backups/%')
        """, (now,))

        # 规则2：.jar 且 path_kind 为 app_or_resource_like
        c.execute("""
            UPDATE analysis
            SET label       = '⚠️ 需确认',
                reason      = reason || '（jar文件，删除可能导致程序功能失效）',
                analyzed_at = ?
            WHERE label = '✅ 可删除'
              AND LOWER(path) LIKE '%.jar'
              AND path IN (
                  SELECT path FROM entries
                  WHERE path_kind = 'app_or_resource_like'
              )
        """, (now,))
# ─────────────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()

    all_rows = load_rows()
    if not all_rows:
        raise SystemExit(0)

    # ── 预加载 Prefetch（一次性，后续 row_to_line 里直接用缓存） ──
    pf = get_prefetch()
    print(f"[Prefetch] 已读取 {len(pf)} 条运行记录")

    # ── 硬编码短路：直接存库，不送 AI ──────────────────────────────
    hard_records = []
    ai_rows = []
    for r in all_rows:
        verdict = hard_verdict(r)
        if verdict == "❌ 不要删":
            hard_records.append({
                **r,
                "label": verdict,
                "reason": "系统级/敏感文件，已自动判定",
                "hard_blocked": True,
            })
        elif verdict == "⚠️ 需确认":
            hard_records.append({
                **r,
                "label": verdict,
                "reason": "路径或访问时间特征，建议人工确认",
                "hard_blocked": False,
            })
        else:
            ai_rows.append(r)

    if hard_records:
        ai_rows = hard_records + ai_rows  # 只合并，不提前存库

    if not ai_rows:
        consistency_check()
        rule_patch()  # ← 新增这一行
        app_aggregation()
        set_status(100, "分析完成")
        raise SystemExit(0)

    # ── AI 分析 ────────────────────────────────────────────────────
    if not (API_URL and API_KEY and MODEL):
        print("[跳过] 还没有配置模型接口，去界面右上角的『设置』里填。")
        consistency_check()
        rule_patch()
        app_aggregation()
        set_status(100, "未配置模型，已跳过 AI 分析")
        raise SystemExit(0)

    client = OpenAI(api_key=API_KEY, base_url=API_URL)
    batches = split_batches(ai_rows)
    total = len(batches)

    done = [0]
    lock = threading.Lock()
    stop = threading.Event()


    def process(args):
        if stop.is_set():
            return
        i, batch = args
        analyze(client, batch)
        with lock:
            done[0] += 1
            pct = 50 + int(done[0] / total * 49)
            set_status(pct, f"AI 分析中 {done[0]}/{total} 批")


    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(process, (i, batch)): i for i, batch in enumerate(batches)}
        for fut in concurrent.futures.as_completed(futures):
            if stop.is_set():
                for f in futures:
                    f.cancel()
                break
            try:
                fut.result()
            except Exception:
                pass

    consistency_check()
    rule_patch()
    app_aggregation()
    set_status(100, "分析完成")
