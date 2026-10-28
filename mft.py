# -*- coding: utf-8 -*-
"""
mft.py — 直接读 NTFS MFT 的极速磁盘扫描内核（零第三方依赖，纯 ctypes）。

为什么需要它
------------
传统做法是 os.walk / os.scandir 逐个目录调用 FindFirstFile，每次都要走
文件系统驱动 + 路径解析，百万文件要几分钟。WizTree 之所以快，是因为它直接
读 NTFS 的 Master File Table —— 一张记录了卷上所有文件名、父目录、大小、
时间戳的表。

本模块做同一件事：把 $MFT 整块读进来，本地解析，把目录树在内存里重建出来。
实测 C 盘（3.0 GB MFT / 约 306 万条记录）全程 13~25 秒。

限制
----
* 仅 NTFS。FAT32 / exFAT / 网络盘走 os.scandir 兜底（见 scan_any）。
* 需要管理员权限才能打开 \\\\.\\X: 卷设备。没权限时同样兜底。
* $MFT 必须连续。绝大多数卷满足；不满足时结果可能缺条目，不会崩。

MFT 记录结构速查（参见 Microsoft [MS-FSCC] / [MS-NTFS]）
---------------------------------------------------------
  0   "FILE" 魔数
  4   WORD  更新序列数组偏移（USA offset）
  6   WORD  更新序列数组长度（USA count，= 扇区数 + 1）
  8   QWORD LSN
  16  WORD  序列号
  18  WORD  硬链接数
  20  WORD  第一个属性的偏移
  22  WORD  标志（bit0 = 在用，bit1 = 目录）
  24  DWORD 实际使用长度
  28  DWORD 分配长度
  32  QWORD 基础记录号

属性头（常驻）
  0  DWORD 类型（0x10 $STANDARD_INFORMATION / 0x30 $FILE_NAME /
                0x80 $DATA / 0xFFFFFFFF 结束）
  4  DWORD 属性长度（8 字节对齐）
  8  BYTE  是否常驻（0 = 常驻）
  9  BYTE  属性名长度
  10 WORD  属性名偏移
  16 DWORD 值长度（常驻时）
  20 WORD  值偏移（常驻时）
"""

from __future__ import annotations

import ctypes
import os
import time
from array import array
from ctypes import wintypes

__all__ = ["scan_volume", "scan_dir", "scan_any", "is_admin", "MftError"]

# ── Win32 常量 ────────────────────────────────────────────────────────
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
FILE_ATTRIBUTE_DIRECTORY = 0x10

FSCTL_GET_NTFS_VOLUME_DATA = 0x00090064

ATTR_END = 0xFFFFFFFF
ATTR_FILE_NAME = 0x30
ATTR_DATA = 0x80
ROOT_FRN = 5              # NTFS 根目录固定是 5 号记录
MAX_DEPTH = 256           # 防御性：父链最多回溯这么多层

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]


class MftError(RuntimeError):
    pass


class _NTFS_VOLUME_DATA_BUFFER(ctypes.Structure):
    """FSCTL_GET_NTFS_VOLUME_DATA 的返回结构，共 96 字节。"""
    _fields_ = [
        ("VolumeSerialNumber", ctypes.c_longlong),
        ("NumberSectors", ctypes.c_longlong),
        ("TotalClusters", ctypes.c_longlong),
        ("FreeClusters", ctypes.c_longlong),
        ("TotalReserved", ctypes.c_longlong),
        ("BytesPerSector", wintypes.DWORD),
        ("BytesPerCluster", wintypes.DWORD),
        ("BytesPerFileRecordSegment", wintypes.DWORD),
        ("ClustersPerFileRecordSegment", wintypes.DWORD),
        ("MftValidDataLength", ctypes.c_longlong),
        ("MftStartLcn", ctypes.c_longlong),
        ("Mft2StartLcn", ctypes.c_longlong),
        ("MftZoneStart", ctypes.c_longlong),
        ("MftZoneEnd", ctypes.c_longlong),
    ]


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ── 卷访问 ────────────────────────────────────────────────────────────

def _open_volume(drive: str):
    handle = _k32.CreateFileW(
        "\\\\.\\" + drive, GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None,
    )
    if handle == INVALID_HANDLE_VALUE:
        err = ctypes.get_last_error()
        raise MftError(
            "无法打开卷 %s（错误 %d：%s）。读取 MFT 需要管理员权限。"
            % (drive, err, ctypes.FormatError(err))
        )
    return handle


def _volume_info(handle) -> _NTFS_VOLUME_DATA_BUFFER:
    vd = _NTFS_VOLUME_DATA_BUFFER()
    returned = wintypes.DWORD()
    ok = _k32.DeviceIoControl(
        handle, FSCTL_GET_NTFS_VOLUME_DATA, None, 0,
        ctypes.byref(vd), ctypes.sizeof(vd), ctypes.byref(returned), None,
    )
    if not ok:
        err = ctypes.get_last_error()
        raise MftError("读取 NTFS 卷信息失败：%s" % ctypes.FormatError(err))
    return vd


def _is_ntfs(drive: str) -> bool:
    """查卷的文件系统类型。非 NTFS 走 scandir。"""
    _k32.GetVolumeInformationW.restype = wintypes.BOOL
    _k32.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD,
    ]
    try:
        fs = ctypes.create_unicode_buffer(64)
        serial = wintypes.DWORD()
        maxlen = wintypes.DWORD()
        flags = wintypes.DWORD()
        ok = _k32.GetVolumeInformationW(
            drive + "\\", None, 0, ctypes.byref(serial),
            ctypes.byref(maxlen), ctypes.byref(flags), fs, 64,
        )
        return bool(ok) and fs.value.upper() == "NTFS"
    except Exception:
        return False


# ── MFT 记录解析 ──────────────────────────────────────────────────────

def _fixup(chunk: bytearray, base: int, rec_size: int, sector_size: int) -> None:
    """就地修复更新序列数组。

    MFT 用 USA 保护每条记录：每个扇区的最后 2 字节被替换成序列号，原值存在
    记录头部的 USA 数组里。不修复的话，跨扇区边界的属性字段会读到序列号
    而不是真实数据 —— 典型症状是文件大小解析出天文数字。
    """
    usa_off = int.from_bytes(chunk[base + 4:base + 6], "little")
    usa_cnt = int.from_bytes(chunk[base + 6:base + 8], "little")
    if usa_cnt < 2 or usa_cnt > 64:
        return
    if usa_off + usa_cnt * 2 > rec_size:
        return
    for i in range(1, usa_cnt):
        dst = base + i * sector_size - 2
        src = base + usa_off + i * 2
        if dst + 2 > base + rec_size:
            break
        chunk[dst:dst + 2] = chunk[src:src + 2]


def _parse_record(chunk: bytearray, base: int, rec_size: int):
    """解析一条 MFT 记录。

    返回 (in_use, is_dir, parent_frn, name, data_size) 或 None。
    name 为 None 表示该记录没有可用的 $FILE_NAME（几乎只出现在系统元数据上）。
    """
    flags = int.from_bytes(chunk[base + 22:base + 24], "little")
    in_use = bool(flags & 0x0001)
    is_dir = bool(flags & 0x0002)

    attr_off = int.from_bytes(chunk[base + 20:base + 22], "little")
    p = base + attr_off
    end = base + rec_size

    parent = None
    name = None
    name_ns = 99
    size = None

    while p + 8 <= end:
        atype = int.from_bytes(chunk[p:p + 4], "little")
        if atype == ATTR_END:
            break
        alen = int.from_bytes(chunk[p + 4:p + 8], "little")
        if alen < 8 or p + alen > end:
            break

        if atype == ATTR_FILE_NAME and chunk[p + 8] == 0:
            vlen = int.from_bytes(chunk[p + 16:p + 20], "little")
            voff = int.from_bytes(chunk[p + 20:p + 22], "little")
            v = p + voff
            if v + 66 <= end and vlen >= 66:
                ns = chunk[v + 65]
                # 命名空间：0=POSIX 1=Win32 2=DOS 3=Win32&DOS。优先 Win32 名。
                if name is None or (ns in (1, 3) and name_ns not in (1, 3)):
                    nlen = chunk[v + 64]
                    if v + 66 + nlen * 2 <= end:
                        parent = int.from_bytes(chunk[v:v + 8], "little") & 0x0000FFFFFFFFFFFF
                        name = bytes(chunk[v + 66:v + 66 + nlen * 2]).decode("utf-16-le", "replace")
                        name_ns = ns

        elif atype == ATTR_DATA and chunk[p + 9] == 0:      # 无名数据流
            if chunk[p + 8]:                                 # 非常驻
                value = int.from_bytes(chunk[p + 48:p + 56], "little")
            else:                                            # 常驻
                value = int.from_bytes(chunk[p + 16:p + 20], "little")
            if size is None or value > size:
                size = value

        p += alen

    return in_use, is_dir, parent, name, size


# ── 主扫描 ────────────────────────────────────────────────────────────

def scan_volume(drive: str, min_bytes: int = 0, on_progress=None,
                should_skip=None) -> list[tuple[str, int]]:
    """扫描整个 NTFS 卷，返回 [(完整路径, 字节大小)]，只含 >= min_bytes 的文件。

    drive 形如 "C:"。on_progress(percent:int, text:str) 可选。
    should_skip(path_lower) -> bool 可选，用于提前排除目录树。
    """
    drive = drive.rstrip("\\/").upper()
    if not drive.endswith(":"):
        raise ValueError("drive 参数应形如 'C:'")

    handle = _open_volume(drive)
    try:
        vd = _volume_info(handle)
        rec_size = vd.BytesPerFileRecordSegment or 1024
        sector = vd.BytesPerSector or 512
        mft_start = vd.MftStartLcn * vd.BytesPerCluster
        mft_len = vd.MftValidDataLength
        total_rec = mft_len // rec_size

        # 紧凑数组，避免 300 万个 Python 元组把内存吃干净
        parents = array("Q", bytes(total_rec * 8))
        name_off = array("I", bytes(total_rec * 4))
        name_len = array("H", bytes(total_rec * 2))
        is_dir = bytearray(total_rec)
        pool = bytearray()

        hits: list[tuple[int, int]] = []      # (frn, size)

        CHUNK_REC = 8192                      # 每块 8192 条 = 8 MB
        pos = mft_start
        done = 0
        t0 = time.time()

        while pos < mft_start + mft_len:
            want = min(CHUNK_REC * rec_size, mft_start + mft_len - pos)
            _k32.SetFilePointerEx(handle, ctypes.c_longlong(pos), None, 0)
            raw = ctypes.create_string_buffer(want)
            got = wintypes.DWORD()
            if not _k32.ReadFile(handle, raw, want, ctypes.byref(got), None):
                err = ctypes.get_last_error()
                raise MftError("读取 $MFT 失败：%s" % ctypes.FormatError(err))
            chunk = bytearray(raw.raw[:got.value])

            n_rec = len(chunk) // rec_size
            for i in range(n_rec):
                base = i * rec_size
                if chunk[base:base + 4] != b"FILE":
                    continue
                _fixup(chunk, base, rec_size, sector)
                parsed = _parse_record(chunk, base, rec_size)
                if parsed is None:
                    continue
                in_use, dir_flag, parent, name, size = parsed
                if not in_use:
                    continue
                frn = done + i
                if frn >= total_rec:
                    break
                if name is not None:
                    encoded = name.encode("utf-16-le")
                    name_off[frn] = len(pool)
                    name_len[frn] = len(encoded) // 2
                    pool += encoded
                    parents[frn] = parent or 0
                is_dir[frn] = 1 if dir_flag else 0
                if not dir_flag and size is not None and size >= min_bytes:
                    hits.append((frn, size))

            done += n_rec
            pos += got.value
            if on_progress and total_rec:
                on_progress(min(99, int(done * 100 / total_rec)),
                            "读取 MFT %d%%" % int(done * 100 / total_rec))
            if got.value == 0:
                break

        scan_secs = time.time() - t0
    finally:
        _k32.CloseHandle(handle)

    # ── 回溯拼路径 ────────────────────────────────────────────────
    t1 = time.time()
    cache: dict[int, str] = {}
    out: list[tuple[str, int]] = []
    prefix = drive + "\\"
    skipped = 0

    def name_of(frn: int) -> str:
        o = name_off[frn]
        n = name_len[frn]
        return bytes(pool[o:o + n * 2]).decode("utf-16-le", "replace")

    for frn, size in hits:
        parts = []
        cur = frn
        depth = 0
        bad = False
        while cur != ROOT_FRN:
            cached = cache.get(cur)
            if cached is not None:
                parts.append(cached)
                break
            if cur >= total_rec or depth > MAX_DEPTH:
                bad = True
                break
            nm_len = name_len[cur]
            if nm_len == 0:
                bad = True
                break
            parts.append(name_of(cur))
            nxt = parents[cur]
            if nxt == cur:
                bad = True
                break
            cur = nxt
            depth += 1
        if bad:
            skipped += 1
            continue
        parts.reverse()
        path = prefix + "\\".join(parts)
        cache[frn] = path
        if should_skip and should_skip(path):
            continue
        out.append((path, size))

    if on_progress:
        on_progress(100, "MFT 解析完成")

    return out


def _looks_like_volume_root(path: str) -> bool:
    p = path.replace("/", "\\")
    return len(p) in (2, 3) and p[1] == ":" and (len(p) == 2 or p[2] == "\\")


def scan_dir(root: str, min_bytes: int = 0, on_progress=None,
             should_skip=None) -> list[tuple[str, int]]:
    """os.scandir 兜底扫描（非 NTFS / 无管理员权限 / 只扫某个子目录）。"""
    out: list[tuple[str, int]] = []
    stack = [os.path.abspath(root)]
    visited = 0
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if should_skip and should_skip(entry.path):
                                continue
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            if st.st_size >= min_bytes:
                                out.append((entry.path, st.st_size))
                    except OSError:
                        continue
        except (OSError, PermissionError):
            continue
        visited += 1
        if on_progress and visited % 24 == 0:
            on_progress(60, "遍历目录 %d 个，已找到 %d 个大文件" % (visited, len(out)))
    return out


def scan_any(path: str, min_bytes: int = 0, on_progress=None,
             should_skip=None) -> tuple[list[tuple[str, int]], str]:
    """对外统一入口。

    返回 (结果列表, 使用的方法)。方法是 "mft" 或 "walk"，便于调用方提示用户。
    """
    if _looks_like_volume_root(path):
        drive = path[0].upper() + ":"
        if _is_ntfs(drive):
            try:
                return scan_volume(drive, min_bytes, on_progress, should_skip), "mft"
            except MftError:
                # 没有管理员权限 / 卷被占用 → 静默降级，不打断用户
                if on_progress:
                    on_progress(0, "MFT 不可用，改用目录遍历（较慢）")
    return scan_dir(path, min_bytes, on_progress, should_skip), "walk"
