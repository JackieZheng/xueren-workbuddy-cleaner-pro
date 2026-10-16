#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 清理专家 · 回收站删除助手（Windows）

把文件/目录尽量移入系统回收站，但**不保证可还原**，也**可能静默永久删除**（见下方说明）。

策略：
1. 优先用 send2trash（若已安装）
2. 回退 ctypes 调 SHFileOperationW(FOF_ALLOWUNDO)
3. 两者都失败时**拒绝执行**（不降级为永久删除）

注意（重要经验）：
- 本模块会尽量把文件送进回收站，但**回收站不是保险箱**：
  - Windows 回收站有配额。单盘配额超限时，Windows 会**直接永久删除**而不是进回收站，
    且 `SHFileOperationW` 可能返回成功（rc=0），本模块**无法察觉**是否真进了回收站。
  - 单个文件/目录**过大**、或所在盘**回收站被禁用/不支持回收站**（网络盘、可移动盘等）
    时，同样会直接永久删除。
  - 用户事后**清空回收站**，已进回收站的内容也会彻底消失。
  因此「删除可还原」**不能保证**。调用方必须明确提示用户：无法替代的内容请先自行备份，
  并在确认无误前不要清空回收站。
- 路径统一转成反斜杠绝对路径，避免 send2trash 在 `\\\\?\\` 长路径前缀下报
  FileNotFoundError（这是历史坑）。
"""
from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes

# ---- SHFileOperationW 常量 ----
FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040          # 关键：进回收站
FOF_NOERRORUI = 0x0400
FOF_WANTNUKEWARNING = 0x4000


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def _norm(path: str) -> str:
    """统一成反斜杠绝对路径（避免 \\\\?\\ 前缀 + 正斜杠导致的失败）。"""
    p = os.path.abspath(path)
    return p.replace("/", "\\")


def _recycle_ctypes(paths: list[str]) -> tuple[bool, int]:
    """用 SHFileOperationW 批量移入回收站。返回 (是否全部成功, 返回码)。"""
    if os.name != "nt":
        return False, -1
    # pFrom 需要以 \0\0 结尾
    joined = "\0".join(_norm(p) for p in paths) + "\0\0"
    op = _SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = joined
    op.pTo = None
    op.fFlags = (FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT
                 | FOF_NOERRORUI | FOF_WANTNUKEWARNING)
    op.fAnyOperationsAborted = False
    op.hNameMappings = None
    op.lpszProgressTitle = None
    try:
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    except Exception:  # noqa: BLE001
        return False, -2
    aborted = bool(op.fAnyOperationsAborted)
    return (rc == 0 and not aborted), rc


def _recycle_send2trash(paths: list[str]) -> tuple[list[str], list[str]]:
    """用 send2trash 逐个删除。返回 (成功列表, 失败列表[(path, err)])。"""
    try:
        from send2trash import send2trash  # type: ignore
    except Exception:  # noqa: BLE001
        return [], [(p, "send2trash 未安装") for p in paths]

    ok: list[str] = []
    bad: list[tuple[str, str]] = []
    for p in paths:
        np = _norm(p)
        try:
            send2trash(np)
        except Exception as e:  # noqa: BLE001
            # 历史坑：send2trash 删完后用短名验证会抛 FileNotFoundError，
            # 但文件其实已经进回收站 —— 此时按「已消失即成功」处理。
            if not os.path.exists(p):
                ok.append(p)
                continue
            bad.append((p, f"{type(e).__name__}: {e}"))
            continue
        ok.append(p)
    return ok, bad


def _has_send2trash() -> bool:
    try:
        import send2trash  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def recycle(paths: list[str], use_send2trash: bool = True) -> dict:
    """把一批路径移入回收站。

    返回 dict：
      - ok:      成功移入回收站（或已不存在）的路径
      - failed:  [(path, err)] 失败项
      - engine:  实际使用的引擎
    """
    paths = [p for p in paths if p]
    if not paths:
        return {"ok": [], "failed": [], "engine": "none"}

    if os.name != "nt":
        return {"ok": [], "failed": [(p, "仅支持 Windows") for p in paths],
                "engine": "unsupported"}

    if use_send2trash and _has_send2trash():
        ok, bad = _recycle_send2trash(paths)
        if not bad:
            return {"ok": ok, "failed": [], "engine": "send2trash"}
        # 个别失败项再用 SHFileOperationW 兜底
        retry = [p for p, _ in bad]
        good, _rc = _recycle_ctypes(retry)
        if good:
            ok.extend(retry)
            bad = []
        if ok or not bad:
            return {"ok": ok, "failed": bad, "engine": "send2trash+SHFileOperationW"}

    success, rc = _recycle_ctypes(paths)
    if success:
        return {"ok": paths, "failed": [], "engine": "SHFileOperationW"}
    ok, bad = [], []
    for p in paths:
        if not os.path.exists(p):
            ok.append(p)
            continue
        one_ok, _ = _recycle_ctypes([p])
        if one_ok:
            ok.append(p)
        else:
            bad.append((p, f"rc={rc}"))
    return {"ok": ok, "failed": bad, "engine": "SHFileOperationW"}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python wb_recycle.py <路径> [路径2 ...]")
        sys.exit(2)
    res = recycle(sys.argv[1:])
    print(f"引擎: {res['engine']}")
    print(f"成功: {len(res['ok'])}  失败: {len(res['failed'])}")
    for p, e in res["failed"]:
        print(f"  FAIL {p} :: {e}")
