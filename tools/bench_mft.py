# -*- coding: utf-8 -*-
"""
tools/bench_mft.py — 对比 MFT 扫描与 os.walk 的实际速度。

以管理员身份运行（否则 MFT 路径会降级成目录遍历，对比就没意义了）：

    python tools/bench_mft.py C: 100

参数：盘符、最小文件大小（MB）。
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mft


def main():
    drive = sys.argv[1] if len(sys.argv) > 1 else "C:"
    min_mb = int(sys.argv[2]) if len(sys.argv) > 2 else 100
    min_bytes = min_mb * 1024 * 1024

    if not mft.is_admin():
        print("⚠ 当前不是管理员，MFT 路径会降级成目录遍历，测不出真实速度。")
        print("  请用『以管理员身份运行』的终端再跑一次。\n")

    print(f"目标：{drive}  最小 {min_mb} MB\n")

    # ── MFT ──────────────────────────────────────────────
    t0 = time.time()
    method = "walk"
    try:
        rows, method = mft.scan_any(
            drive + "\\", min_bytes,
            on_progress=lambda p, t: None,
            should_skip=None,
        )
        mft_secs = time.time() - t0
        print(f"[{method}] {mft_secs:.1f}s  ->  {len(rows):,} 个文件，"
              f"合计 {sum(s for _, s in rows) / 1024 ** 3:.1f} GB")
    except Exception as e:
        print("MFT 扫描失败：", e)
        rows, mft_secs = [], 0.0

    if method != "mft":
        print("\n（没有走 MFT，跳过 os.walk 对比）")
        return

    # ── os.walk ──────────────────────────────────────────
    print("\n开始 os.walk 对照（可能需要几分钟）…")
    t0 = time.time()
    found = 0
    total = 0
    stack = [drive + "\\"]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            sz = e.stat(follow_symlinks=False).st_size
                            if sz >= min_bytes:
                                found += 1
                                total += sz
                    except OSError:
                        pass
        except OSError:
            pass
    walk_secs = time.time() - t0
    print(f"[walk] {walk_secs:.1f}s  ->  {found:,} 个文件，合计 {total / 1024 ** 3:.1f} GB")

    if mft_secs > 0:
        print(f"\nMFT 快 {walk_secs / mft_secs:.1f} 倍")
    if rows and found:
        print(f"两侧条目差：{len(rows) - found}")


if __name__ == "__main__":
    main()
