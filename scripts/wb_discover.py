#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 清理专家 · 目录自动发现

按可靠性降序尝试多种信号源，**不依赖硬编码盘符 / 用户名**：

  安装目录 install    环境变量 WORKBUDDY_APP_PATH / WORKBUDDY_RESOURCES_PATH
                      → 注册表（卸载键 / 文件关联命令行）
                      → 运行中 WorkBuddy.exe 进程
                      → 常规候选（%LOCALAPPDATA%\\Programs、各盘 \\WorkBuddy）
  数据目录 data       环境变量 WORKBUDDY_CONFIG_DIR / CODEBUDDY_CONFIG_DIR / WORKBUDDY_USER_DATA_DIR
                      → ~/.workbuddy 及各盘 Users\\<user>\\.workbuddy
  工作区   workspace  CODEBUDDY_PROJECT_DIR 向上回溯到 WorkBuddy 目录
                      → ~/WorkBuddy

所有结果按 realpath 去重（`.workbuddy` 常是指向其它盘的符号链接）。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

APP_EXE = "WorkBuddy.exe"
APP_KEY = "workbuddy"          # 用于匹配 DisplayName / 注册表类名（小写比较）
DATA_FOLDER = ".workbuddy"
WORKSPACE_FOLDER = "WorkBuddy"

# 信号来源
ENV, REGISTRY, PROCESS, FALLBACK = "env", "registry", "process", "fallback"
SOURCE_LABEL = {
    ENV: "环境变量",
    REGISTRY: "注册表",
    PROCESS: "运行中进程",
    FALLBACK: "常规候选",
}


@dataclass
class Found:
    path: str
    source: str
    kind: str          # install / data / workspace

    @property
    def label(self) -> str:
        return SOURCE_LABEL.get(self.source, self.source)


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

def user_home() -> str:
    return os.path.expanduser("~")


def _win_path(p: str) -> str:
    """把 Git-Bash 风格 `/c/Users/...` 或带引号的路径归一为 Windows 路径。"""
    p = p.strip().strip('"')
    m = re.match(r"^/([A-Za-z])/(.*)$", p)
    if m:
        return (m.group(1).upper() + ":/" + m.group(2)).replace("/", "\\")
    return p


def _dedup(items: list[Found]) -> list[Found]:
    """按 realpath 去重，并丢弃不存在的路径。"""
    out, seen = [], set()
    for f in items:
        if not f.path or not os.path.isdir(f.path):
            continue
        key = os.path.normcase(os.path.realpath(f.path))
        if key in seen:
            continue
        seen.add(key)
        f.path = os.path.normpath(f.path)
        out.append(f)
    return out


def _is_app_dir(p: str) -> bool:
    return bool(p) and os.path.isfile(os.path.join(p, APP_EXE))


def _walk_up_to_app_dir(p: str, max_up: int = 6) -> str:
    """从 p 起向上查找含 WorkBuddy.exe 的目录。"""
    cur = os.path.abspath(p)
    for _ in range(max_up):
        if _is_app_dir(cur):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return ""


def _exe_dir_from_cmd(cmd: str) -> str:
    """从 `"D:\\WorkBuddy\\WorkBuddy.exe" "%1"` 之类字符串提取 exe 所在目录。"""
    if not cmd:
        return ""
    m = (re.search(r'"([^"]+\.exe)"', cmd, re.IGNORECASE)
         or re.search(r'([A-Za-z]:\\[^\s"]+\.exe)', cmd, re.IGNORECASE))
    if not m:
        return ""
    return os.path.dirname(m.group(1))


# ---------------------------------------------------------------------------
# 安装目录
# ---------------------------------------------------------------------------

def _install_from_env() -> list[Found]:
    out = []
    for key in ("WORKBUDDY_APP_PATH", "WORKBUDDY_RESOURCES_PATH",
                "WORKBUDDY_PROMPT_TEMPLATES_DIR", "CODEBUDDY_BUILTIN_SKILLS_DIR"):
        v = os.environ.get(key)
        if not v:
            continue
        d = _walk_up_to_app_dir(_win_path(v))
        if d:
            out.append(Found(d, ENV, "install"))
    return out


def _install_from_registry() -> list[Found]:
    if os.name != "nt":
        return []
    try:
        import winreg  # type: ignore
    except ImportError:
        return []

    out: list[Found] = []

    # 1) 文件关联命令行（最直接、最可靠）
    for cls in ("workbuddy", "WorkBuddy.doc", "WorkBuddy.txt", "WorkBuddy.pptx"):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                rf"SOFTWARE\Classes\{cls}\shell\open\command") as k:
                cmd, _ = winreg.QueryValueEx(k, "")
        except OSError:
            continue
        d = _exe_dir_from_cmd(cmd)
        if _is_app_dir(d):
            out.append(Found(os.path.normpath(d), REGISTRY, "install"))

    # 2) 卸载注册表项（跨盘符通用；排除 Chrome 等伪装项）
    hives = [
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, base in hives:
        try:
            root = winreg.OpenKey(hive, base)
        except OSError:
            continue
        try:
            i = 0
            while True:
                try:
                    sub_name = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(root, sub_name) as sub:
                        def g(field: str) -> str:
                            try:
                                return str(winreg.QueryValueEx(sub, field)[0])
                            except OSError:
                                return ""
                        disp, pub = g("DisplayName"), g("Publisher")
                        if APP_KEY not in disp.lower():
                            continue
                        if "google" in pub.lower() or "chrome" in pub.lower():
                            continue          # Chrome Web App 不是真身
                        loc = g("InstallLocation")
                        if _is_app_dir(loc):
                            out.append(Found(os.path.normpath(loc), REGISTRY, "install"))
                        for field in ("UninstallString", "QuietUninstallString",
                                      "DisplayIcon"):
                            d = _exe_dir_from_cmd(g(field))
                            if _is_app_dir(d):
                                out.append(Found(os.path.normpath(d), REGISTRY, "install"))
                except OSError:
                    continue
        finally:
            try:
                winreg.CloseKey(root)
            except OSError:
                pass
    return out


def _install_from_process() -> list[Found]:
    """扫描运行中的 WorkBuddy.exe（纯 ctypes，不依赖 psutil / wmic）。"""
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

        count = 4096
        pids = (wintypes.DWORD * count)()
        needed = wintypes.DWORD()
        if not psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(needed)):
            return []
        n = needed.value // ctypes.sizeof(wintypes.DWORD)

        out: list[Found] = []
        for pid in list(pids[:n]):
            if pid == 0:
                continue
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                continue
            try:
                buf = ctypes.create_unicode_buffer(32768)
                size = wintypes.DWORD(len(buf))
                if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                    exe = buf.value
                    if os.path.basename(exe).lower() == APP_EXE.lower():
                        out.append(Found(os.path.dirname(exe), PROCESS, "install"))
            finally:
                kernel32.CloseHandle(handle)
        return out
    except Exception:  # noqa: BLE001
        return []


def _install_from_fallback() -> list[Found]:
    local = os.environ.get("LOCALAPPDATA") or os.path.join(user_home(), "AppData", "Local")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    cands = [os.path.join(local, "Programs", WORKSPACE_FOLDER),
             os.path.join(pf, WORKSPACE_FOLDER),
             os.path.join(pf86, WORKSPACE_FOLDER)]
    for drive in "CDEF":
        cands.append(os.path.join(drive + ":\\", WORKSPACE_FOLDER))
    return [Found(p, FALLBACK, "install") for p in cands if _is_app_dir(p)]


# ---------------------------------------------------------------------------
# 数据目录
# ---------------------------------------------------------------------------

def _normalize_data_root(p: str) -> str:
    """把 `<盘>/.workbuddy/app` 之类子路径归一到 `.workbuddy` 根。"""
    cur = os.path.abspath(p)
    for _ in range(5):
        name = os.path.basename(cur)
        if name == DATA_FOLDER:
            return cur
        parent = os.path.dirname(cur)
        if os.path.basename(parent) == DATA_FOLDER:
            return parent
        if not os.path.isdir(parent) or parent == cur:
            break
        cur = parent
    return p if os.path.isdir(p) else ""


def _data_from_env() -> list[Found]:
    out = []
    for key in ("WORKBUDDY_CONFIG_DIR", "CODEBUDDY_CONFIG_DIR",
                "WORKBUDDY_USER_DATA_DIR"):
        v = os.environ.get(key)
        if not v:
            continue
        p = _normalize_data_root(_win_path(v))
        if p:
            out.append(Found(p, ENV, "data"))
    return out


def _data_from_fallback() -> list[Found]:
    home = user_home()
    user = os.path.basename(home)
    cands = [os.path.join(home, DATA_FOLDER)]
    for drive in "CDEF":
        cands.append(os.path.join(drive + ":\\", "Users", user, DATA_FOLDER))
    return [Found(p, FALLBACK, "data") for p in cands if os.path.isdir(p)]


# ---------------------------------------------------------------------------
# 工作区
# ---------------------------------------------------------------------------

def _workspace_from_env() -> list[Found]:
    v = os.environ.get("CODEBUDDY_PROJECT_DIR") or os.environ.get("WORKBUDDY_WORKSPACE")
    if not v:
        return []
    cur = os.path.abspath(_win_path(v))
    for _ in range(4):
        if os.path.basename(cur) == WORKSPACE_FOLDER:
            return [Found(cur, ENV, "workspace")]
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    parent = os.path.dirname(os.path.abspath(_win_path(v)))
    return [Found(parent, ENV, "workspace")] if os.path.isdir(parent) else []


def _workspace_from_fallback() -> list[Found]:
    p = os.path.join(user_home(), WORKSPACE_FOLDER)
    return [Found(p, FALLBACK, "workspace")] if os.path.isdir(p) else []


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------

def find_install() -> list[Found]:
    return _dedup(_install_from_env() + _install_from_registry()
                  + _install_from_process() + _install_from_fallback())


def find_data() -> list[Found]:
    return _dedup(_data_from_env() + _data_from_fallback())


def find_workspace() -> list[Found]:
    return _dedup(_workspace_from_env() + _workspace_from_fallback())


def find_resource_dirs() -> list[Found]:
    """WorkBuddy 自带的资源 / 工具目录（只读展示，不参与清理）。

    含：安装目录下的 resources、提示词模板、内置技能，以及数据区的用户技能目录。
    """
    out: list[Found] = []
    for key in ("WORKBUDDY_RESOURCES_PATH", "WORKBUDDY_PROMPT_TEMPLATES_DIR",
                "CODEBUDDY_BUILTIN_SKILLS_DIR"):
        v = os.environ.get(key)
        if v and os.path.isdir(_win_path(v)):
            out.append(Found(_win_path(v), ENV, "resource"))
    for f in find_data():
        for sub in ("skills", os.path.join("plugins", "marketplaces")):
            p = os.path.join(f.path, sub)
            if os.path.isdir(p):
                out.append(Found(p, f.source, "resource"))
    return _dedup(out)


def discover_all() -> dict[str, list[Found]]:
    return {
        "install": find_install(),
        "data": find_data(),
        "workspace": find_workspace(),
        "resource": find_resource_dirs(),
    }


if __name__ == "__main__":
    for kind, items in discover_all().items():
        print(f"== {kind} ==")
        for f in items:
            print(f"  [{f.label:6}] {f.path}")
