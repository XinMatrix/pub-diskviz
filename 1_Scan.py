import csv
import os
import time
import sqlite3
import winreg
from datetime import datetime
import sys
import psutil

import config
import mft


# ── 路径配置 ──
def get_base_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = get_base_dir()
DB_PATH = os.environ.get('DB_PATH', os.path.join(BASE_DIR, "disk_scan.db"))

SCAN_PATHS = [r"C:\\"]

MIN_BYTES = (config.load().get("min_mb") or 100) * 1024 * 1024

_HOME = os.path.expanduser("~")
_LOCALAPPDATA = os.environ.get("LOCALAPPDATA", "")
_APPDATA = os.environ.get("APPDATA", "")

HARD_EXCLUDED_PREFIXES = tuple(
    os.path.normpath(p).lower()
    for p in [
        r"C:\Windows",
        r"C:\Recovery",
        r"C:\System Volume Information",
        r"C:\$Recycle.Bin",
        r"C:\$WinREAgent",
        r"C:\PerfLogs",
        r"C:\ProgramData\Microsoft",
        r"C:\ProgramData\Windows",
        r"C:\Users\All Users",
        r"C:\bootmgr",
        r"C:\pagefile.sys",
        r"C:\hiberfil.sys",
        r"C:\swapfile.sys",
        r"C:\Users\All Users\Microsoft",
        os.path.join(_LOCALAPPDATA, "Microsoft", "Windows"),
    ]
    if p
)

HARD_EXCLUDED_CONTAINS = [
    "steamapps\\common",
]

HARD_EXCLUDED_BASENAMES = {
    "bootmgr", "pagefile.sys", "hiberfil.sys", "swapfile.sys",
    # 补充以下：
    "$mft", "$logfile", "$volume", "$attrdef", "$bitmap",
    "$boot", "$badclus", "$secure", "$upcase", "$extend",
    "$recycled", "bootsect.bak", "bootfont.bin",
    "ntldr", "ntdetect.com", "io.sys", "msdos.sys",
    "config.sys", "autoexec.bat",
    "desktop.ini",  # 根目录下的
}

EXT_KIND_MAP: dict[str, str] = {
    # ── 虚拟磁盘 ──────────────────────────────────────────────────
    ".vhdx": "virtual_disk",
    ".vhd": "virtual_disk",
    ".vmdk": "virtual_disk",
    ".qcow2": "virtual_disk",
    ".img": "virtual_disk",
    ".wim": "virtual_disk",

    # ── 密钥/证书 ─────────────────────────────────────────────────
    ".pem": "secret_like",
    ".key": "secret_like",
    ".crt": "secret_like",
    ".cer": "secret_like",
    ".p12": "secret_like",
    ".pfx": "secret_like",

    # ── 数据库 ────────────────────────────────────────────────────
    ".db": "database_like",
    ".db3": "database_like",
    ".sqlite": "database_like",
    ".sqlite3": "database_like",
    ".mdf": "database_like",
    ".ldf": "database_like",
    ".frm": "database_like",
    ".ibd": "database_like",

    # ── AI 模型 ───────────────────────────────────────────────────
    ".bin": "ai_model_like",
    ".pt": "ai_model_like",
    ".pth": "ai_model_like",
    ".ckpt": "ai_model_like",
    ".onnx": "ai_model_like",
    ".engine": "ai_model_like",
    ".pdparams": "ai_model_like",
    ".safetensors": "ai_model_like",
    ".gguf": "ai_model_like",
    ".npz": "ai_model_like",
    ".pb": "ai_model_like",
    ".tflite": "ai_model_like",
    ".mlmodel": "ai_model_like",
    ".caffemodel": "ai_model_like",

    # ── 压缩/镜像 ─────────────────────────────────────────────────
    ".zip": "archive_like",
    ".rar": "archive_like",
    ".7z": "archive_like",
    ".tar": "archive_like",
    ".gz": "archive_like",
    ".bz2": "archive_like",
    ".xz": "archive_like",
    ".zst": "archive_like",
    ".lz4": "archive_like",
    ".iso": "archive_like",
    ".dmg": "archive_like",

    # ── 开发包 ────────────────────────────────────────────────────
    ".jar": "java_package_like",
    ".war": "java_package_like",
    ".ear": "java_package_like",
    ".aar": "java_package_like",
    ".whl": "java_package_like",
    ".nupkg": "java_package_like",

    # ── 游戏资源 ──────────────────────────────────────────────────
    ".vpk": "game_resource_like",
    ".pak": "game_resource_like",
    ".obb": "game_resource_like",
    ".ucas": "game_resource_like",
    ".utoc": "game_resource_like",
    ".pck": "game_resource_like",
    ".bundle": "game_resource_like",
    ".assets": "game_resource_like",
    ".wad": "game_resource_like",
    ".bsa": "game_resource_like",
    ".ba2": "game_resource_like",
    ".forge": "game_resource_like",
    ".psarc": "game_resource_like",
    ".rpf": "game_resource_like",

    # ── 安装包 ────────────────────────────────────────────────────
    ".exe": "installer_like",
    ".msi": "installer_like",
    ".msix": "installer_like",
    ".appx": "installer_like",
    ".appxbundle": "installer_like",
    ".cab": "installer_like",
    ".deb": "installer_like",
    ".rpm": "installer_like",

    # ── 日志/崩溃转储 ─────────────────────────────────────────────
    ".log": "app_logs_like",
    ".etl": "app_logs_like",
    ".dmp": "app_logs_like",

    # ── 缓存/临时 ─────────────────────────────────────────────────
    ".tmp": "app_cache_like",
    ".temp": "app_cache_like",
    ".cache": "app_cache_like",
    ".crdownload": "app_cache_like",
    ".part": "app_cache_like",
    ".partial": "app_cache_like",
    ".download": "app_cache_like",
    ".incomplete": "app_cache_like",
    ".ddp": "app_cache_like",
    ".ushaderprecache": "app_cache_like",
    ".pid": "app_cache_like",

    # ── 备份（注意：⚠️ 需确认，不可直接删） ──────────────────────
    ".bak": "backup_like",
    ".old": "backup_like",

    # ── 配置 ──────────────────────────────────────────────────────
    ".json": "config_like",
    ".yaml": "config_like",
    ".yml": "config_like",
    ".toml": "config_like",
    ".ini": "config_like",
    ".cfg": "config_like",
    ".conf": "config_like",
    ".reg": "config_like",
}

DEV_HINT_KEYWORDS = [".jdks", ".jbr", ".gradle", ".m2", "android-sdk", "android\\sdk",
                     "node_modules", ".npm", ".pnpm-store", ".yarn",
                     "site-packages", ".venv", "conda", "miniconda", "anaconda",
                     "jetbrains", ".cargo", ".rustup", "go\\pkg", "go/pkg",
                     ".nuget", ".dotnet", "__pycache__",
                     "playwright", "puppeteer", "selenium"]
AI_MODEL_HINT_KEYWORDS = ["huggingface", "modelscope", "paddlehub", "paddlenlp",
                          "ollama", "lmstudio", "koboldai", "oobabooga",
                          "stable-diffusion", "stablediffusion", "comfyui", "invokeai", "automatic1111",
                            "civitai", "diffusers", "transformers", "torch\\hub", "torch/hub",
                          "checkpoints", "loras", "embeddings", "hypernetworks"]

RISKY_DIR_KEYWORDS = [
    "userdata", "user data",
    "profile", "profiles",
    "save", "saves",
    "storage", "indexeddb",
    "database", "databases",
    "plugins", "plugin",
    "extensions",
    "mods",
    "model",  # 保留单数，比 "models" 稍精确
]

def norm_lower(path: str) -> str:
    return os.path.normpath(path).lower()


def is_hard_excluded(path: str) -> bool:
    p = os.path.normpath(path).lower()
    if p.startswith(HARD_EXCLUDED_PREFIXES):
        return True
    if any(seg in p for seg in HARD_EXCLUDED_CONTAINS):
        return True
    basename = os.path.basename(p).rstrip('.')  # ← 去掉末尾的点，兼容 $MFT. 这种情况
    if basename in HARD_EXCLUDED_BASENAMES:
        return True
    return False


def split_path_info(path: str):
    norm = os.path.normpath(path)
    filename = os.path.basename(norm)
    parent_dir = os.path.dirname(norm)
    _, ext = os.path.splitext(filename)
    return norm, filename, parent_dir, ext.lower()


def contains_any_keyword(text: str, keywords) -> bool:
    t = text.lower()
    return any(k in t for k in keywords)


def guess_path_kind(path: str, filename: str, ext: str) -> str:
    p = path.lower()

    if is_hard_excluded(path):
        return "system_core"

    if ext in EXT_KIND_MAP:
        if ext == ".exe":
            if contains_any_keyword(p, ["download", "downloader", "update", "updater",
                                        "upgrade", "installer", "setup"]):
                return "installer_like"
            if p.startswith(norm_lower(r"C:\Program Files")) or \
                    p.startswith(norm_lower(r"C:\Program Files (x86)")):
                return "app_or_resource_like"
            return "installer_like"
        if ext in {".bin", ".dat"} and (
                p.startswith(norm_lower(r"C:\Program Files")) or
                p.startswith(norm_lower(r"C:\Program Files (x86)"))
        ):
            return "app_or_resource_like"
        return EXT_KIND_MAP[ext]

    # 扩展名未命中，走路径关键词判断
    if contains_any_keyword(p, AI_MODEL_HINT_KEYWORDS):
        return "ai_model_like"
    if contains_any_keyword(p, ["logs", "log", "crash", "crashes", "dumps", "dump"]):
        return "app_logs_like"
    if contains_any_keyword(p, ["temp", "tmp"]):
        return "temp_like"
    if contains_any_keyword(p, ["cache", "gpucache", "code cache", "shadercache", "patchcache"]):
        return "app_cache_like"
    if contains_any_keyword(p, DEV_HINT_KEYWORDS):
        return "dev_env_like"
    if contains_any_keyword(p, RISKY_DIR_KEYWORDS):
        return "app_or_resource_like"
    if os.path.basename(p) == "rollingcache":
        return "app_cache_like"

    return "unknown"


# ── 新增：采集文件时间信息 ────────────────────────────────────────────────────
def get_file_times(path: str):
    try:
        stat = os.stat(path)
        mtime = datetime.fromtimestamp(stat.st_mtime)
        days = (datetime.now() - mtime).days
        return (
            mtime.strftime("%Y-%m-%d"),
            mtime.strftime("%Y-%m-%d"),
            days,
        )
    except Exception:
        return ("unknown", "unknown", -1)


# ── 新增：读取同目录兄弟文件 ─────────────────────────────────────────────────
def get_siblings(path: str, max_count: int = 12) -> str:
    """返回同目录下其他文件名列表（逗号分隔，最多 max_count 个）"""
    parent = os.path.dirname(path)
    target = os.path.basename(path)
    try:
        items = [f for f in os.listdir(parent) if f != target]
        # 优先展示文件（非目录），数量截断
        files = [f for f in items if os.path.isfile(os.path.join(parent, f))]
        dirs = [f + "/" for f in items if os.path.isdir(os.path.join(parent, f))]
        combined = files + dirs
        sample = combined[:max_count]
        if len(combined) > max_count:
            sample.append(f"共{len(combined)}项")
        return ", ".join(sample) if sample else "(空目录)"
    except Exception:
        return "(无法读取)"


# ── 新增：已安装程序列表（只读一次，缓存） ────────────────────────────────────

_INSTALLED_PROGRAMS: set[str] | None = None


def get_installed_programs() -> set[str]:
    global _INSTALLED_PROGRAMS
    if _INSTALLED_PROGRAMS is not None:
        return _INSTALLED_PROGRAMS

    names: set[str] = set()
    keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, subkey in keys:
        try:
            with winreg.OpenKey(hive, subkey) as reg_key:
                count = winreg.QueryInfoKey(reg_key)[0]
                for i in range(count):
                    try:
                        sub = winreg.EnumKey(reg_key, i)
                        with winreg.OpenKey(reg_key, sub) as sk:
                            try:
                                name, _ = winreg.QueryValueEx(sk, "DisplayName")
                                if name:
                                    names.add(name.lower())
                            except FileNotFoundError:
                                pass
                    except Exception:
                        pass
        except Exception:
            pass

    _INSTALLED_PROGRAMS = names
    print(f"[注册表] 已读取 {len(names)} 个已安装程序")
    return names


def infer_install_status(path: str) -> str:
    """
    根据路径中的关键词，在已安装程序列表里做模糊匹配。
    返回：'installed' / 'not_found' / 'unknown'
    """
    programs = get_installed_programs()
    parts = path.lower().replace("\\", "/").split("/")
    # 取路径中段（排除盘符和 users/appdata 等通用前缀）
    candidates = [p for p in parts if len(p) > 3 and p not in {
        "users", "appdata", "local", "roaming", "locallow",
        "program files", "program files (x86)", "programdata",
        "c:", "d:", "e:", "f:",
    }]
    for c in candidates:
        for prog in programs:
            if c in prog or prog in c:
                return "installed"
    return "not_found"


# ── 新增：运行中进程的可执行路径集合（只读一次） ─────────────────────────────

_RUNNING_EXE_PATHS: set[str] | None = None


def get_running_exe_paths() -> set[str]:
    global _RUNNING_EXE_PATHS
    if _RUNNING_EXE_PATHS is not None:
        return _RUNNING_EXE_PATHS

    paths: set[str] = set()

    for proc in psutil.process_iter(['exe']):
        try:
            exe = proc.info.get('exe')
            if exe:
                paths.add(os.path.normpath(exe).lower())
        except Exception:
            pass

    _RUNNING_EXE_PATHS = paths
    print(f"[进程] 当前运行中进程: {len(paths)} 个")
    return paths


def is_file_in_use(path: str) -> bool:
    """判断文件路径是否被某个运行中进程直接使用（exe路径匹配）"""
    norm = os.path.normpath(path).lower()
    return norm in get_running_exe_paths()


# ── DB 初始化（新增字段） ────────────────────────────────────────────────────
def init_db(conn):
    conn.executescript("""
        PRAGMA journal_mode=WAL;

        CREATE TABLE IF NOT EXISTS scans (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            scanned_at TEXT NOT NULL,
            scan_path  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS entries (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id         INTEGER NOT NULL REFERENCES scans(id),
            path            TEXT NOT NULL,
            filename        TEXT NOT NULL,
            ext             TEXT,
            parent_dir      TEXT NOT NULL,
            path_kind       TEXT,
            size_bytes      INTEGER NOT NULL,
            last_modified   TEXT,
            last_accessed   TEXT,
            days_since_access INTEGER,
            siblings        TEXT,
            install_status  TEXT,
            is_running      INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS analysis (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            analyzed_at TEXT NOT NULL,
            path        TEXT NOT NULL,
            label       TEXT NOT NULL,
            reason      TEXT,
            size_bytes  INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_entries_scan_path ON entries(scan_id, path);
        CREATE INDEX IF NOT EXISTS idx_entries_size      ON entries(size_bytes DESC);
        CREATE INDEX IF NOT EXISTS idx_entries_kind      ON entries(path_kind);
        CREATE INDEX IF NOT EXISTS idx_entries_ext       ON entries(ext);
        CREATE INDEX IF NOT EXISTS idx_entries_access    ON entries(days_since_access);
    """)

    # ── 兼容旧表结构迁移 ──────────────────────────────────────────
    existing_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(entries)")
    }
    migrations = [
        ("scan_id", "ALTER TABLE entries ADD COLUMN scan_id INTEGER"),
        ("last_modified", "ALTER TABLE entries ADD COLUMN last_modified TEXT"),
        ("last_accessed", "ALTER TABLE entries ADD COLUMN last_accessed TEXT"),
        ("days_since_access", "ALTER TABLE entries ADD COLUMN days_since_access INTEGER"),
        ("siblings", "ALTER TABLE entries ADD COLUMN siblings TEXT"),
        ("install_status", "ALTER TABLE entries ADD COLUMN install_status TEXT"),
        ("is_running", "ALTER TABLE entries ADD COLUMN is_running INTEGER DEFAULT 0"),
    ]
    for col, sql in migrations:
        if col not in existing_cols:
            conn.execute(sql)
            print(f"[迁移] 已添加列: {col}")
    conn.commit()
    # ─────────────────────────────────────────────────────────────


def reset_db(conn):
    conn.executescript("""
        DELETE FROM entries;
        DELETE FROM scans;
        VACUUM;
    """)
    conn.commit()


def collect_candidates(scan_path):
    """找出卷/目录下所有 >= MIN_BYTES 的文件。

    优先直接读 NTFS 的 MFT —— 一次把整张文件表读进内存解析，C 盘通常十几秒
    扫完；拿不到管理员权限、或者不是 NTFS 卷时，自动降级成 os.scandir 遍历。
    """
    print(f"\n正在扫描 (Scanning): {scan_path}")
    t0 = time.time()
    rows, method = mft.scan_any(
        scan_path,
        MIN_BYTES,
        on_progress=lambda pct, text: print(f"  [{pct:3d}%] {text}"),
        should_skip=lambda p: is_hard_excluded(p.lower()),
    )
    print(f"扫描方式 (Method): {'NTFS MFT（快速）' if method == 'mft' else '目录遍历（较慢）'}")
    print(f"耗时 (Time): {time.time() - t0:.1f} 秒")
    print(f"命中 (Candidates): {len(rows)} 个 >= {MIN_BYTES // 1048576} MB 的文件")
    return rows, method


def ingest_scan(conn, scan_path, rows):
    """把 (路径, 大小) 增强成完整元数据后写入 entries 表。"""
    get_installed_programs()
    get_running_exe_paths()

    batch = []
    skipped_hard = 0
    skipped_invalid = 0

    for raw_path, size_bytes in rows:
        if raw_path.endswith("\\") or size_bytes <= 0 or size_bytes < MIN_BYTES:
            skipped_invalid += 1
            continue

        norm_path, filename, parent_dir, ext = split_path_info(raw_path)

        # 过滤 NTFS 系统元数据文件（$MFT、$LogFile 等）
        if filename.lstrip().startswith('$'):
            skipped_hard += 1
            continue

        if is_hard_excluded(norm_path):
            skipped_hard += 1
            continue

        if ext == ".dll":
            skipped_hard += 1
            continue

        if not os.path.isfile(norm_path):
            skipped_invalid += 1
            continue

        path_kind = guess_path_kind(norm_path, filename, ext)
        last_modified, last_accessed, days_since_access = get_file_times(norm_path)
        siblings = get_siblings(norm_path)
        install_status = infer_install_status(norm_path)
        running = 1 if is_file_in_use(norm_path) else 0

        batch.append((
            norm_path, filename, ext, parent_dir, path_kind, size_bytes,
            last_modified, last_accessed, days_since_access,
            siblings, install_status, running,
        ))


    # 按 path（index 0）去重，保留最后出现的那条
    seen: dict[str, tuple] = {}
    for row in batch:
        seen[row[0]] = row
    batch = list(seen.values())


    cur = conn.execute(
        "INSERT INTO scans (scanned_at, scan_path) VALUES (?, ?)",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), scan_path),
    )
    scan_id = cur.lastrowid

    conn.executemany("""
        INSERT INTO entries (
            scan_id, path, filename, ext, parent_dir, path_kind, size_bytes,
            last_modified, last_accessed, days_since_access,
            siblings, install_status, is_running
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [(scan_id, *row) for row in batch])
    conn.commit()

    # 清理 analysis 中 path 不在新 entries 里的孤儿记录
    conn.execute("""
        DELETE FROM analysis
        WHERE path NOT IN (SELECT path FROM entries)
    """)
    conn.commit()


    print(f"已排除系统核心路径 (Hard Excluded): {skipped_hard} 个文件")
    print(f"已跳过无效项 (Skipped): {skipped_invalid} 个")
    print(f"成功存入 (Stored): {len(batch)} 个候选文件")
    return len(batch)


def print_top_files(conn, limit=200):
    print(f"\n{'大小':<10} | {'类型':<18} | {'未访问天数':<10} | {'安装状态':<12} | 路径")
    print("-" * 130)
    for path, size_bytes, path_kind, days, status in conn.execute("""
        SELECT path, size_bytes, path_kind, days_since_access, install_status
        FROM entries ORDER BY size_bytes DESC LIMIT ?
    """, (limit,)):
        size_mb = size_bytes / (1024 * 1024)
        size_str = f"{size_mb / 1024:.2f}GB" if size_mb > 1024 else f"{size_mb:.1f}MB"
        days_str = f"{days}天" if days >= 0 else "?"
        print(f"{size_str:<10} | {path_kind or 'unknown':<18} | {days_str:<10} | {status or '?':<12} | {path}")


def print_kind_summary(conn):
    print("\n类型提示统计:")
    print("-" * 60)
    for kind, cnt, total_bytes in conn.execute("""
        SELECT path_kind, COUNT(*), SUM(size_bytes)
        FROM entries GROUP BY path_kind ORDER BY SUM(size_bytes) DESC
    """):
        gb = (total_bytes or 0) / (1024 ** 3)
        print(f"{(kind or 'unknown'):<20} | {cnt:>6} 个 | {gb:>8.2f} GB")


def export_candidates_csv(conn, out_csv="disk_data.csv"):
    with open(out_csv, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            "path", "filename", "ext", "parent_dir", "path_kind", "size_bytes",
            "last_modified", "last_accessed", "days_since_access",
            "siblings", "install_status", "is_running",
        ])
        for row in conn.execute("""
            SELECT path, filename, ext, parent_dir, path_kind, size_bytes,
                   last_modified, last_accessed, days_since_access,
                   siblings, install_status, is_running
            FROM entries ORDER BY size_bytes DESC
        """):
            writer.writerow(row)
    print(f"\n已导出候选文件 CSV: {out_csv}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        SCAN_PATHS = sys.argv[1:]

    db_conn = sqlite3.connect(DB_PATH)
    init_db(db_conn)
    reset_db(db_conn)

    if not mft.is_admin():
        print("[提示] 当前不是管理员，无法直接读 MFT，将退回目录遍历（会慢一些）。")
        print("       以管理员身份运行可以快 5~10 倍。")

    total_stored = 0
    for p in SCAN_PATHS:
        rows, method = collect_candidates(p)
        total_stored += ingest_scan(db_conn, p, rows)

    print_top_files(db_conn, limit=200)
    print_kind_summary(db_conn)

    # 如果文件大小变化超过5%，视为文件已变更，清除旧的analysis结论让AI重新分析
    db_conn.execute("""
        DELETE FROM analysis
        WHERE path IN (
            SELECT a.path FROM analysis a
            JOIN entries e ON a.path = e.path
            WHERE ABS(CAST(a.size_bytes AS REAL) - CAST(e.size_bytes AS REAL))
                  / MAX(CAST(e.size_bytes AS REAL), 1) > 0.05
        )
    """)
    db_conn.commit()

    export_candidates_csv(db_conn)

    print(f"\n扫描完成，总候选文件数: {total_stored}")
    db_conn.close()
