#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 清理专家 · 目录发现 + 分级规则

职责：
1. 自动发现所有「与 WorkBuddy 相关」的目录（数据区 / 工作区 / 安装区 / 用户区）
   —— 定位逻辑统一委托给 wb_discover（环境变量 → 注册表 → 进程 → 兜底）
2. 对每个目录按规则给出清理分级：
   - keep    不可删（运行时依赖、凭据、程序本体）
   - safe    安全清理（日志 / 快照 / 缓存垃圾 / 升级包 / 散落备份）
   - caution 谨慎清理（历史追踪 / 旧会话 / 项目记录，删了丢失回溯能力）
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

try:
    import wb_discover as _discover
except ImportError:  # 作为包导入时的后备
    from . import wb_discover as _discover  # type: ignore

# ---- 分级常量 ----
KEEP, SAFE, CAUTION = "keep", "safe", "caution"
TIER_ORDER = {SAFE: 0, CAUTION: 1, KEEP: 2}
TIER_LABEL = {SAFE: "🟢 安全清理", CAUTION: "🟡 谨慎清理", KEEP: "🔴 不可删除"}


@dataclass
class Rule:
    pattern: str          # 相对路径正则（POSIX 分隔符，search 语义，注意用 ^ 锚定）
    tier: str
    category: str
    reason: str = ""
    keep_days: int = 0    # >0 表示「最近 N 天内的不清理」
    expand: bool = False  # 命中后继续下钻（命中项本身不报，仅作为容器）

    def __post_init__(self):
        self.re_compiled = re.compile(self.pattern)

    def match(self, rel: str) -> bool:
        return self.re_compiled.search(rel) is not None


def _r(pattern, tier, category, reason="", keep_days=0, expand=False):
    return Rule(pattern, tier, category, reason, keep_days, expand)


# ---------------------------------------------------------------------------
# 规则表（顺序敏感：先匹配先命中，容器规则要排在内部例外规则之后）
# ---------------------------------------------------------------------------

DATA_RULES = [
    # ---------- 不可删除 ----------
    _r(r"^binaries(?:/|$)", KEEP, "runtime", "运行时环境（Python/Node 等），删了脚本直接跑不起来"),
    _r(r"^skills(?:/|$)", KEEP, "skills", "已安装技能本体"),
    _r(r"^workbuddy\.db", KEEP, "db", "会话数据库（历史对话）"),
    _r(r"^credentials(?:/|$)", KEEP, "credentials", "登录凭据"),
    _r(r"^security(?:/|$)", KEEP, "security", "安全密钥数据"),
    _r(r"^settings\.json", KEEP, "settings", "全局配置"),
    _r(r"^user-[^/]+(?:/|$)", KEEP, "user-profile", "用户档案与记忆"),
    _r(r"^workspace/memory(?:/|$)", KEEP, "memory", "工作区记忆"),
    # plugins：cache 可清，其余保留
    _r(r"^plugins/cache(?:/|$)", CAUTION, "plugins-cache", "插件缓存（可重新生成）"),
    _r(r"^plugins$", KEEP, "plugins", "插件本体与市场索引", expand=True),
    _r(r"^plugins/.+$", KEEP, "plugins", "插件本体与市场索引"),
    _r(r"^(?:mcp|mcps|im-backend|vibe)", KEEP, "mcp", "MCP / IM / 扩展后端数据"),
    # app：session 可清，其余保留
    _r(r"^app/session(?:/|$)", CAUTION, "app-session", "应用会话缓存"),
    _r(r"^app$", KEEP, "app", "应用数据", expand=True),
    _r(r"^app/.+$", KEEP, "app", "应用数据"),
    _r(r"^node_modules(?:/|$)", KEEP, "node-modules", "依赖包"),
    _r(r"^(?:ide|telemetry|statsig)$", KEEP, "misc-keep", "低频配置数据"),

    # ---------- 安全清理 ----------
    _r(r"^logs/sandbox/\d+$", SAFE, "sandbox-logs", "沙箱逐次运行日志", keep_days=1),
    _r(r"^logs/sandbox$", SAFE, "sandbox-logs", "沙箱运行日志", expand=True),
    _r(r"^logs/\d{4}-\d{2}-\d{2}$", SAFE, "daily-logs", "每日运行日志", keep_days=7),
    _r(r"^logs/.+$", SAFE, "logs", "运行日志", keep_days=7),
    _r(r"^logs$", SAFE, "logs", "运行日志", keep_days=7, expand=True),
    _r(r"^_skills_snapshot_", SAFE, "skills-snapshot", "旧技能全量快照（体积大、易重复）"),
    _r(r"^_archived_skills(?:/|$)", SAFE, "archived-skills", "手动归档的旧技能"),
    _r(r"^clipboard-images(?:/|$)", SAFE, "clipboard", "剪贴板历史图片"),
    _r(r"^tmp/.+$", SAFE, "tmp", "临时文件", keep_days=1),
    _r(r"^tmp$", SAFE, "tmp", "临时文件", keep_days=1, expand=True),
    _r(r"^update-cache(?:/|$)", SAFE, "updater-cache", "升级包缓存"),
    _r(r"^[^/]+\.(?:bak|tmp|old|log)$", SAFE, "stray-backup", "散落的备份/临时文件"),

    # ---------- 谨慎清理 ----------
    _r(r"^traces/\d+$", CAUTION, "traces", "调试追踪（过期无价值）", keep_days=7),
    _r(r"^traces/.+$", CAUTION, "traces", "调试追踪（过期无价值）", keep_days=7),
    _r(r"^traces$", CAUTION, "traces", "调试追踪（过期无价值）", expand=True),
    _r(r"^blobs(?:/|$)", CAUTION, "blobs", "二进制分片缓存"),
    _r(r"^workspace/sessions/.+$", CAUTION, "sessions", "会话快照仓库", keep_days=7),
    _r(r"^workspace/sessions$", CAUTION, "sessions", "会话快照仓库", expand=True),
    _r(r"^workspace/(?!memory).+$", CAUTION, "workspace", "工作区数据", keep_days=7),
    _r(r"^workspace$", CAUTION, "workspace", "工作区数据", expand=True),
    _r(r"^projects/.+$", CAUTION, "projects", "项目索引记录", keep_days=7),
    _r(r"^projects$", CAUTION, "projects", "项目索引记录", expand=True),
    _r(r"^cache/.+$", CAUTION, "cache", "本地缓存"),
    _r(r"^cache$", CAUTION, "cache", "本地缓存", expand=True),
    _r(r"^file-tree-manifests", CAUTION, "file-tree", "文件树清单（可重建）"),
    _r(r"^changes-detail", CAUTION, "changes", "变更明细（可重建）"),
    _r(r"^file-history", CAUTION, "file-history", "文件历史（丢失回溯能力）"),
    _r(r"^shell-snapshots", CAUTION, "shell-snapshots", "Shell 快照"),
    _r(r"^todos(?:/|$)", CAUTION, "todos", "任务清单记录"),
]

# 安装区（WorkBuddy 安装目录）—— 只挑可再生的日志/备份/崩溃转储
INSTALL_RULES = [
    _r(r"^debug\.log$", SAFE, "install-log", "调试日志"),
    _r(r"^[^/]+\.(?:bak|old|tmp)$", SAFE, "install-backup", "备份文件"),
    _r(r"^(?:.+\.dmp)$", SAFE, "crash-dump", "崩溃转储"),
    _r(r"^logs(?:/|$)", SAFE, "install-logs", "安装目录日志", expand=True),
]

# 用户区（AppData / Temp / .cache）—— 只删与 WorkBuddy 相关的条目
USER_RULES = [
    _r(r"^logs/.+$", SAFE, "app-logs", "应用日志", keep_days=7),
    _r(r"^logs$", SAFE, "app-logs", "应用日志", keep_days=7, expand=True),
    _r(r"^Cache/.+$", CAUTION, "app-cache", "应用缓存"),
    _r(r"^Cache$", CAUTION, "app-cache", "应用缓存", expand=True),
    _r(r"^GPUCache(?:/|$)", SAFE, "gpu-cache", "GPU 着色器缓存（可再生）"),
    _r(r"^Code Cache(?:/|$)", SAFE, "code-cache", "JS 代码缓存（可再生）"),
    _r(r"^CodeBuddyExtension", KEEP, "extension", "扩展数据"),
]

# 升级包残留目录：installer.exe 已下载的安装包，可重新下载
UPDATER_RULES = [
    _r(r"^installer\.exe$", SAFE, "updater", "已下载的升级安装包（可重新下载）"),
    _r(r"^.+\.(?:exe|msi|zip|tmp|log)$", SAFE, "updater", "升级过程残留文件"),
]


def user_home() -> str:
    return os.path.expanduser("~")


def _local() -> str:
    return os.environ.get("LOCALAPPDATA") or os.path.join(user_home(), "AppData", "Local")


def _roaming() -> str:
    return os.environ.get("APPDATA") or os.path.join(user_home(), "AppData", "Roaming")


@dataclass
class Root:
    key: str
    label: str
    kind: str          # data / workspace / install / user
    path: str
    rules: list
    note: str = ""
    report_unclassified: bool = False   # 顶层未命中项是否登记为「保留」展示
    source: str = ""                    # 定位来源（环境变量/注册表/运行中进程/常规候选）


def discover_roots(include_user: bool = True) -> list[Root]:
    """发现全部扫描根。

    路径定位由 wb_discover 负责（环境变量 → 注册表 → 运行中进程 → 常规候选），
    这里只负责把它们装配成带分级规则的 Root。
    """
    roots: list[Root] = []

    for f in _discover.find_data():
        roots.append(Root("data:" + f.path, "WorkBuddy 数据区", "data", f.path, DATA_RULES,
                          ".workbuddy 主数据目录（日志/追踪/缓存/会话/技能）",
                          report_unclassified=True, source=f.label))

    for f in _discover.find_workspace():
        roots.append(Root("workspace:" + f.path, "WorkBuddy 工作区（会话目录）",
                          "workspace", f.path, [],
                          "每个目录是一次会话的工作目录，可能含你的产出文件",
                          source=f.label))

    for f in _discover.find_install():
        roots.append(Root("install:" + f.path, "WorkBuddy 程序目录", "install", f.path,
                          INSTALL_RULES,
                          "程序本体不可删，仅识别日志/备份/崩溃转储", source=f.label))

    if include_user:
        local, roaming = _local(), _roaming()
        cands = [
            (os.path.join(local, "WorkBuddy"), USER_RULES, "应用本地数据 / 日志"),
            (os.path.join(roaming, "WorkBuddy"), USER_RULES, "应用漫游数据"),
            (os.path.join(local, "@genieworkbuddy-desktop-updater"), UPDATER_RULES,
             "桌面端升级包缓存"),
            (os.path.join(local, "CodeBuddyExtension"), USER_RULES, "扩展数据"),
        ]
        for p, rules, note in cands:
            if os.path.isdir(p):
                roots.append(Root("user:" + p, f"用户目录 · {os.path.basename(p)}",
                                  "user", p, rules, note, source="环境变量"))
    return roots


# WorkBuddy 相关的临时文件前缀（Temp / .cache 下命中即视为相关）
TEMP_PATTERN = r"^(?:workbuddy|codebuddy|wb[-_]|genie|@genie|miora|xueren)"
TEMP_RE = re.compile(TEMP_PATTERN, re.IGNORECASE)


def find_temp_roots() -> list[Root]:
    out = []
    tmp = os.environ.get("TEMP") or os.path.join(_local(), "Temp")
    if os.path.isdir(tmp):
        out.append(Root("temp:" + tmp, "系统临时目录（仅 WorkBuddy 相关）", "user", tmp,
                        [_r(TEMP_PATTERN, SAFE, "temp", "临时文件（可再生）", keep_days=1)],
                        "只处理文件名以 workbuddy / codebuddy / wb- / genie / xueren 开头的条目",
                        source="环境变量"))
    cache = os.path.join(user_home(), ".cache")
    if os.path.isdir(cache):
        out.append(Root("cache:" + cache, "用户 .cache（仅 WorkBuddy 相关）", "user", cache,
                        [_r(TEMP_PATTERN, CAUTION, "cache", "用户级缓存")],
                        source="常规候选"))
    return out


def classify(rule_list: list[Rule], rel: str) -> Rule | None:
    """按规则表匹配相对路径（POSIX 分隔符），首个命中即返回。"""
    for r in rule_list:
        if r.match(rel):
            return r
    return None


_SESSION_NAME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})$")


def workspace_classify(name: str) -> Rule | None:
    """工作区专用规则：时间戳命名的会话目录 → 谨慎清理。"""
    if _SESSION_NAME_RE.match(name):
        return _r(r".*", CAUTION, "session-dir", "一次会话的工作目录（可能含产出文件）",
                  keep_days=14)
    return None


def session_name_age_days(name: str, now: float) -> float | None:
    """从会话目录名（YYYY-MM-DD-HH-MM-SS）解析「距今多少天」。

    比用 mtime 可靠：mtime 会因后续文件写入被刷新，目录名的日期才是会话创建时间。
    """
    m = _SESSION_NAME_RE.match(name)
    if not m:
        return None
    import datetime as _d
    try:
        dt = _d.datetime(*[int(x) for x in m.groups()])
    except ValueError:
        return None
    return max(0.0, (now - dt.timestamp()) / 86400.0)
