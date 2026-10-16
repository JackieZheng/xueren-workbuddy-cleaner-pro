#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 清理专家（高级版）· 命令行工具

子命令：
  doctor  自检环境（回收站可用性、发现的扫描目标、磁盘剩余）
  scan    扫描全部 WorkBuddy 相关目录 → 生成 Markdown / HTML / JSON 报告
  list    按分级列出条目（只读）
  card    生成自包含 HTML 完整结果页
  summary 输出紧凑摘要 JSON
  clean   按分级/ID 移入回收站（默认 dry-run，需 --yes 才真正执行）

设计原则：
  * 默认只读，绝不自动删除
  * 删除一律走系统回收站（通常可还原；配额超出的部分会被系统直接永久删除，无法恢复）
  * keep 级硬保护，任何参数都不会动
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as _dt
import hashlib
import html
import json
import os
import re
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wb_paths as W  # noqa: E402
import wb_discover as D  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

NOW = time.time()
DEFAULT_JSON = os.path.join(tempfile.gettempdir(), "wb_cleaner_last_scan.json")
MAX_DEPTH = 3


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def human(n: float) -> str:
    for u, s in (("TB", 1024 ** 4), ("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= s:
            return f"{n / s:.2f} {u}"
    return f"{n:.0f} B"


def _item_id(seed: str) -> str:
    return "wb-" + hashlib.md5(seed.lower().encode("utf-8")).hexdigest()[:8]


def esc(s) -> str:
    """HTML 转义（所有动态字符串进 HTML 前必须经过这里）。"""
    return html.escape(str(s), quote=True)


def measure(path: str, is_dir: bool) -> tuple[int, int, float]:
    """返回 (字节数, 文件数, 最新 mtime)。"""
    if not is_dir:
        try:
            st = os.stat(path)
            return st.st_size, 1, st.st_mtime
        except OSError:
            return 0, 0, 0.0
    total = files = 0
    newest = 0.0
    for r, _ds, fns in os.walk(path, onerror=lambda e: None):
        try:
            newest = max(newest, os.stat(r).st_mtime)
        except OSError:
            pass
        for f in fns:
            try:
                st = os.stat(os.path.join(r, f))
            except OSError:
                continue
            total += st.st_size
            files += 1
            if st.st_mtime > newest:
                newest = st.st_mtime
    return total, files, newest


def make_item(path, rel, root, tier, category, reason, is_dir, keep_days=0,
              age_override=None) -> dict:
    size, files, newest = measure(path, is_dir)
    age_days = age_override if age_override is not None else (
        (NOW - newest) / 86400.0 if newest else 9999)
    if tier in (W.SAFE, W.CAUTION) and keep_days > 0 and age_days < keep_days:
        tier, blocked = W.KEEP, True
        reason = f"{reason}（最近 {keep_days} 天内使用过，自动保留）"
    else:
        blocked = False
    return {
        "id": _item_id(os.path.realpath(path)),
        "root_key": root.key, "root_label": root.label, "root_kind": root.kind,
        "path": os.path.normpath(path), "rel": rel,
        "tier": tier, "category": category, "reason": reason,
        "is_dir": is_dir, "group": False, "paths": [os.path.normpath(path)],
        "bytes": size, "files": files, "mtime": newest,
        "age_days": round(age_days, 1), "recently_used": blocked,
    }


def group_items(children: list[dict], parent_path, parent_rel, root) -> list[dict]:
    """把容器目录下钻得到的一堆子项，按 (分级, 类别) 合并成少量汇总项。

    好处：traces 有 600+ 分片、projects 有上千 jsonl，合并后报告只有几行，
    但 paths 里保留了每个真实路径，清理时逐条删除。
    """
    buckets: dict = {}
    for it in children:
        k = (it["tier"], it["category"])
        b = buckets.setdefault(k, {"paths": [], "bytes": 0, "files": 0,
                                   "mtime": 0.0, "reasons": []})
        b["paths"].extend(it.get("paths") or [it["path"]])
        b["bytes"] += it["bytes"]
        b["files"] += it["files"]
        b["mtime"] = max(b["mtime"], it["mtime"])
        if it["reason"] not in b["reasons"]:
            b["reasons"].append(it["reason"])

    out = []
    for (tier, cat), b in buckets.items():
        n = len(b["paths"])
        reason = b["reasons"][0] if b["reasons"] else ""
        reason = re.sub(r"^\d+ 项 · ", "", reason)   # 避免「N 项 · N 项 · …」套娃
        # 只有一个子项时直接用子项路径，避免显示成父目录造成误解
        disp = b["paths"][0] if n == 1 else os.path.normpath(parent_path)
        out.append({
            "id": _item_id(os.path.realpath(parent_path) + "::" + cat + "::" + tier),
            "root_key": root.key, "root_label": root.label, "root_kind": root.kind,
            "path": os.path.normpath(disp), "rel": parent_rel,
            "tier": tier, "category": cat,
            "reason": f"{n} 项 · {reason}",
            "is_dir": True, "group": True, "paths": b["paths"],
            "bytes": b["bytes"], "files": b["files"], "mtime": b["mtime"],
            "age_days": round((NOW - b["mtime"]) / 86400.0, 1) if b["mtime"] else 9999,
            "recently_used": False,
        })
    return out


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------

def _rule_for(root, rel):
    if root.kind == "workspace":
        return W.workspace_classify(rel)
    return W.classify(root.rules, rel) if root.rules else None


def _walk(root, d, rel, depth) -> list[dict]:
    out: list[dict] = []
    try:
        entries = list(os.scandir(d))
    except OSError:
        return out
    for e in entries:
        erel = f"{rel}/{e.name}" if rel else e.name
        try:
            is_dir = e.is_dir(follow_symlinks=False)
        except OSError:
            continue
        r = _rule_for(root, erel)
        if r is None:
            if depth == 0 and is_dir and root.report_unclassified:
                out.append(make_item(e.path, erel, root, W.KEEP, "unclassified",
                                     "未分类（默认保留）", True))
            continue
        if r.expand and is_dir and depth < MAX_DEPTH:
            sub = _walk(root, e.path, erel, depth + 1)
            if sub:
                out.extend(group_items(sub, e.path, erel, root))
        else:
            out.append(make_item(e.path, erel, root, r.tier, r.category, r.reason,
                                 is_dir, r.keep_days))
    return out


def scan_root(root) -> list[dict]:
    if root.kind == "workspace" and not root.rules:
        items = []
        try:
            for name in sorted(os.listdir(root.path)):
                p = os.path.join(root.path, name)
                if not os.path.isdir(p):
                    continue
                r = W.workspace_classify(name)
                if r is None:
                    continue
                age = W.session_name_age_days(name, NOW)
                items.append(make_item(p, name, root, r.tier, r.category, r.reason,
                                       True, r.keep_days, age_override=age))
        except OSError:
            pass
        return items
    return _walk(root, root.path, "", 0)


def scan(include_user=True, workers=8) -> dict:
    roots = W.discover_roots(include_user=include_user)
    if include_user:
        roots += W.find_temp_roots()

    all_items: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(scan_root, r): r for r in roots}
        for fu in cf.as_completed(futs):
            r = futs[fu]
            try:
                all_items.extend(fu.result())
            except Exception as e:  # noqa: BLE001
                print(f"  [warn] 扫描 {r.path} 失败: {e}", file=sys.stderr)

    seen, uniq = set(), []
    for it in all_items:
        key = os.path.normcase(it["path"]) + "|" + it["category"] + "|" + it["tier"]
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)
    uniq.sort(key=lambda x: (W.TIER_ORDER[x["tier"]], -x["bytes"]))

    totals = {t: {"bytes": 0, "files": 0, "count": 0} for t in (W.SAFE, W.CAUTION, W.KEEP)}
    for it in uniq:
        t = totals[it["tier"]]
        t["bytes"] += it["bytes"]
        t["files"] += it["files"]
        t["count"] += 1

    return {
        "scanned_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "roots": [{"key": r.key, "label": r.label, "kind": r.kind,
                   "path": r.path, "note": r.note} for r in roots],
        "totals": totals,
        "items": uniq,
    }


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def _n_paths(it: dict) -> int:
    return len(it.get("paths") or [it["path"]])


def write_markdown(data: dict, path: str) -> None:
    t = data["totals"]
    L = []
    L.append("# WorkBuddy 清理专家 · 扫描报告\n")
    L.append(f"- 扫描时间：{data['scanned_at']}")
    L.append(f"- 扫描目标：{len(data['roots'])} 处\n")
    L.append("## 总览\n")
    L.append("| 分级 | 条目 | 体积 | 文件数 |")
    L.append("|---|---:|---:|---:|")
    for tier in (W.SAFE, W.CAUTION, W.KEEP):
        x = t[tier]
        L.append(f"| {W.TIER_LABEL[tier]} | {x['count']} | {human(x['bytes'])} | {x['files']:,} |")
    L.append("")
    L.append("## 扫描目标\n")
    L.append("| 类别 | 路径 | 说明 |")
    L.append("|---|---|---|")
    for r in data["roots"]:
        L.append(f"| {r['label']} | `{r['path']}` | {r['note']} |")
    L.append("")

    for tier in (W.SAFE, W.CAUTION):
        rows = sorted([i for i in data["items"] if i["tier"] == tier],
                      key=lambda x: -x["bytes"])
        if not rows:
            continue
        L.append(f"## {W.TIER_LABEL[tier]}（{human(t[tier]['bytes'])}）\n")
        L.append("| ID | 体积 | 子项 | 龄期 | 路径 | 说明 |")
        L.append("|---|---:|---:|---:|---|---|")
        for i in rows:
            age = f"{i['age_days']:.0f}天" if i["age_days"] < 9000 else "—"
            L.append(f"| `{i['id']}` | {human(i['bytes'])} | {_n_paths(i)} | {age} "
                     f"| `{i['path']}` | {i['reason']} |")
        L.append("")

    keep = sorted([i for i in data["items"] if i["tier"] == W.KEEP], key=lambda x: -x["bytes"])
    L.append(f"## 🔴 不可删除（{human(t[W.KEEP]['bytes'])}）\n")
    L.append("| 体积 | 路径 | 说明 |")
    L.append("|---:|---|---|")
    for i in keep[:80]:
        L.append(f"| {human(i['bytes'])} | `{i['path']}` | {i['reason']} |")
    if len(keep) > 80:
        L.append(f"| … | 其余 {len(keep) - 80} 项见 JSON | |")
    L.append("")
    L.append("> **提示**：会话工作目录（`~/WorkBuddy/<时间戳>`）里通常含 `node_modules`、")
    L.append("> `.cache`、`__pycache__` 及各类模型缓存（`.hf` 前缀目录）等可再生目录，本工具按整目录评估体积；")
    L.append("> 如需精确到子目录，进入对应目录后用 `--dir` 方式手动查看。\n")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))


def write_html(data: dict, path: str) -> None:
    t = data["totals"]

    def card(tier):
        x = t[tier]
        return (f'<div class="card {tier}"><div class="lb">{W.TIER_LABEL[tier]}</div>'
                f'<div class="big">{human(x["bytes"])}</div>'
                f'<div class="sub">{x["count"]} 条目 · {x["files"]:,} 文件</div></div>')

    rows = []
    for tier in (W.SAFE, W.CAUTION):
        for i in sorted([x for x in data["items"] if x["tier"] == tier], key=lambda x: -x["bytes"]):
            rows.append(
                f'<tr class="{tier}"><td>{W.TIER_LABEL[tier]}</td>'
                f'<td class="p"><span class="cat">{esc(i["category"])}</span> {esc(i["path"])}</td>'
                f'<td class="r">{human(i["bytes"])}</td><td class="r">{i["files"]:,}</td>'
                f'<td class="r">{_n_paths(i)}</td><td>{esc(i["reason"])}</td>'
                f'<td><code>{esc(i["id"])}</code></td></tr>')
    tgt = "".join(f'<li><b>{esc(r["label"])}</b> <code>{esc(r["path"])}</code> — {esc(r["note"])}</li>'
                  for r in data["roots"])
    html = f"""<!doctype html><meta charset="utf-8">
<title>WorkBuddy 清理专家 · 扫描报告</title>
<style>
 body{{font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;margin:0;
   background:#f5f7f8;color:#20303a}}
 .wrap{{max-width:1180px;margin:0 auto;padding:28px 20px 60px}}
 h1{{font-size:22px;margin:0 0 4px}} .meta{{color:#6b7d88;margin-bottom:20px}}
 .cards{{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:22px}}
 .card{{flex:1;min-width:190px;background:#fff;border-radius:14px;padding:16px 18px;
   box-shadow:0 1px 3px rgba(0,0,0,.06);border-left:5px solid #8a9aa5}}
 .card.safe{{border-color:#01a539}} .card.caution{{border-color:#e0a800}}
 .card.keep{{border-color:#c0392b}}
 .lb{{font-size:13px;color:#6b7d88}} .big{{font-size:26px;font-weight:700;margin:2px 0}}
 .sub{{font-size:12px;color:#8a9aa5}}
 table{{width:100%;border-collapse:collapse;background:#fff;border-radius:12px;
   overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.06);font-size:13px}}
 th,td{{padding:8px 10px;border-bottom:1px solid #eef2f4;text-align:left;vertical-align:top}}
 th{{background:#f0f4f6;font-weight:600;color:#4a5b66}}
 td.r{{text-align:right;white-space:nowrap}} td.p{{max-width:520px;word-break:break-all}}
 tr.safe td:first-child{{color:#018a30}} tr.caution td:first-child{{color:#b78700}}
 .cat{{background:#e8f6ec;color:#018a30;border-radius:4px;padding:1px 6px;font-size:11px}}
 tr.caution .cat{{background:#fdf3d7;color:#b78700}}
 .tgt{{background:#fff;border-radius:12px;padding:10px 22px;box-shadow:0 1px 3px rgba(0,0,0,.06);
   margin-bottom:22px}}
 code{{background:#eef2f4;padding:1px 5px;border-radius:4px;font-size:12px}}
</style>
<div class="wrap">
 <h1>WorkBuddy 清理专家 · 扫描报告</h1>
 <div class="meta">扫描时间 {esc(data['scanned_at'])} · 共 {len(data['roots'])} 处目标 ·
   删除走系统回收站，不保证可还原，重要文件请先备份</div>
 <div class="cards">{card(W.SAFE)}{card(W.CAUTION)}{card(W.KEEP)}</div>
 <div class="tgt"><b>扫描目标</b><ul>{tgt}</ul></div>
 <table><thead><tr><th>分级</th><th>路径</th><th>体积</th><th>文件</th><th>子项</th>
 <th>说明</th><th>ID</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
</div>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


# ---------------------------------------------------------------------------
# 清单导出（只读）
# ---------------------------------------------------------------------------

def write_list_md(items: list[dict], path: str, label: str) -> None:
    """把清单导出为 Markdown（如 workbuddy-delete-list.md）。"""
    L = [f"# WorkBuddy 清理清单 · {label}\n",
         f"- 生成时间：{_dt.datetime.now().isoformat(timespec='seconds')}",
         f"- 共 {len(items)} 条目 / {human(sum(i['bytes'] for i in items))}\n",
         "| 路径 | 大小 | 子项 | 依据 | id |", "|---|---:|---:|---|---|"]
    for i in items:
        L.append(f"| `{i['path']}` | {human(i['bytes'])} | {_n_paths(i)} "
                 f"| {i['reason']} | `{i['id']}` |")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))


# ---------------------------------------------------------------------------
# 清理
# ---------------------------------------------------------------------------

def expand_paths(items: list[dict]) -> list[str]:
    seen, out = set(), []
    for i in items:
        for p in (i.get("paths") or [i["path"]]):
            k = os.path.normcase(p)
            if k not in seen:
                seen.add(k)
                out.append(p)
    out.sort(key=len)   # 先父后子，减少操作数
    return out


def write_done_md(items: list[dict], result: dict, path: str) -> None:
    """清理执行完成后自动产出 workbuddy-cleanup-done.md（按类别汇总 + 执行结果）。"""
    import collections
    by = collections.defaultdict(lambda: [0, 0])
    for i in items:
        b = by[i["category"]]
        b[0] += 1
        b[1] += i["bytes"]
    L = ["# WorkBuddy 清理 · 执行结果\n",
         f"- 执行时间：{_dt.datetime.now().isoformat(timespec='seconds')}",
         f"- 结果：成功 **{result['ok']}** 个路径，失败/跳过 **{result['failed']}** 个\n",
         "## 按类别汇总\n",
         "| 类别 | 条目 | 体积 |", "|---|---:|---:|"]
    for cat, (n, b) in sorted(by.items(), key=lambda x: -x[1][1]):
        L.append(f"| {cat} | {n} | {human(b)} |")
    L.append(f"\n**合计：{len(items)} 条目 / {human(sum(i['bytes'] for i in items))}**\n")
    L.append("> ⚠️ **删除不保证可还原**：已尽量移入系统回收站，但回收站不是保险箱——"
             "配额超限、单文件/目录过大、回收站被禁用或所在盘不支持回收站时，Windows 会"
             "**直接永久删除、本工具无法察觉**；之后清空回收站也会彻底消失。\n"
             "> **确认无误前请勿清空回收站**，以便仍有机会还原；无法替代的内容请事先备份。")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))


def do_clean(items: list[dict], yes: bool, dry: bool,
             report_path: str | None = None) -> dict:
    import wb_recycle  # 延迟导入

    targets = [i for i in items if i["tier"] in (W.SAFE, W.CAUTION)]
    paths = expand_paths(targets)
    total = sum(i["bytes"] for i in targets)

    print(f"\n待清理：{len(targets)} 条目 → {len(paths)} 个路径 / {human(total)}")
    if dry or not yes:
        print("（dry-run 预演，未执行删除；加 --yes 才真正执行删除）")
        for i in sorted(targets, key=lambda x: -x["bytes"])[:25]:
            print(f"  [{i['tier']}] {human(i['bytes']):>10}  {_n_paths(i):>4}子项  {i['path']}")
        if len(targets) > 25:
            print(f"  ... 其余 {len(targets) - 25} 条")
        print("⚠️ 风险提示：删除会尽量移入回收站，但回收站不是保险箱——"
              "配额超限、单文件/目录过大、回收站被禁用或所在盘不支持回收站时，"
              "Windows 会【直接永久删除】且本工具无法察觉；事后清空回收站也会彻底消失。"
              "无法替代的内容请先自行备份。")
        return {"dry_run": True, "items": len(targets), "paths": len(paths), "bytes": total}

    ok_n = ok_b = fail = 0
    print("⚠️ 开始删除。回收站不是保险箱：配额超限 / 单文件(目录)过大 / 回收站被禁用或"
          "所在盘不支持回收站 时，Windows 会【直接永久删除】且本工具无法察觉；"
          "之后清空回收站也会让它们彻底消失。无法替代的内容请确保已自行备份。")
    B = 200
    for k in range(0, len(paths), B):
        batch = paths[k:k + B]
        res = wb_recycle.recycle(batch)
        gone = {os.path.normcase(p) for p in res["ok"]}
        for p in batch:
            if os.path.normcase(p) in gone or not os.path.exists(p):
                ok_n += 1
        fail += len(res["failed"])
        for p, e in res["failed"]:
            print(f"  [跳过] {p} :: {e}")
        print(f"  进度 {min(k + B, len(paths))}/{len(paths)}  成功={ok_n} 失败={fail}")

    print(f"\n完成：成功 {ok_n} 个路径，失败 {fail} 个")
    print("⚠️ 本次删除已尽量移入回收站，但【无法保证全部可还原】：配额超限、过大文件、"
          "回收站不可用等情况会被 Windows 直接永久删除。在确认无误前【请勿清空回收站】，"
          "以便仍有机会还原；若需真正释放空间，确认后再清空回收站。")
    result = {"dry_run": False, "ok": ok_n, "failed": fail}
    if report_path:
        write_done_md(targets, result, report_path)
        print(f"执行报告 → {report_path}")
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_doctor(args):
    print("== WorkBuddy 清理专家 · 环境自检 ==\n")
    print(f"平台       : {sys.platform}")
    print(f"Python     : {sys.version.split()[0]}  ({sys.executable})")
    print("运行模式   : 扫描 + 回收站清理（clean 默认 dry-run，需 --yes 才执行）")
    try:
        import send2trash  # noqa: F401
        print("回收站引擎 : send2trash ✅")
    except Exception:  # noqa: BLE001
        print("回收站引擎 : 回退 SHFileOperationW(FOF_ALLOWUNDO) ✅")
    print("\n== 发现的扫描目标（← 标注定位来源）==")
    roots = W.discover_roots() + W.find_temp_roots()
    for r in roots:
        print(f"  ✅ [{r.kind:9}] {r.label}")
        tail = f"   ← {r.source}" if getattr(r, "source", "") else ""
        print(f"      {r.path}{tail}")

    res = D.find_resource_dirs()
    if res:
        print("\n== 工具 / 资源目录（只读，不参与清理）==")
        for f in res:
            print(f"  📁 [{f.label:6}] {f.path}")

    print("\n== 数据盘剩余空间 ==")
    seen = set()
    for r in roots:
        drive = os.path.splitdrive(os.path.abspath(r.path))[0]
        if not drive or drive in seen:
            continue
        seen.add(drive)
        try:
            u = shutil.disk_usage(drive + os.sep)
            print(f"  {drive}  剩余 {human(u.free)} / 共 {human(u.total)}"
                  f"（已用 {u.used / u.total * 100:.0f}%）")
        except Exception as e:  # noqa: BLE001
            print(f"  {drive}  读取失败: {e}")


def cmd_scan(args):
    t0 = time.time()
    print("扫描中……")
    data = scan(include_user=not args.no_user, workers=args.workers)
    json_out = args.json or DEFAULT_JSON
    os.makedirs(os.path.dirname(json_out), exist_ok=True)
    with open(json_out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"JSON → {json_out}")
    if args.json and os.path.normcase(args.json) != os.path.normcase(DEFAULT_JSON):
        # 同时写一份默认路径，方便 list/card 直接复用，不必重复扫描
        with open(DEFAULT_JSON, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    if args.out:
        (write_html if args.out.lower().endswith(".html") else write_markdown)(data, args.out)
        print(f"报告 → {args.out}")
    t = data["totals"]
    print(f"\n完成（{time.time() - t0:.1f}s）")
    for tier in (W.SAFE, W.CAUTION, W.KEEP):
        print(f"  {W.TIER_LABEL[tier]:<12} {t[tier]['count']:>4} 条目 / {human(t[tier]['bytes'])}")


def _load(args) -> dict:
    src = args.json or DEFAULT_JSON
    if not os.path.isfile(src):
        print("未找到扫描结果，正在重新扫描……")
        data = scan(include_user=not getattr(args, "no_user", False))
        with open(src, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return data
    with open(src, "r", encoding="utf-8") as f:
        return json.load(f)


def _filter(items, args):
    if getattr(args, "id", None):
        want = set(args.id)
        return [i for i in items if i["id"] in want]
    if getattr(args, "tier", None):
        items = [i for i in items if i["tier"] == args.tier]
    if getattr(args, "category", None):
        items = [i for i in items if i["category"].startswith(args.category)]
    if getattr(args, "min_mb", None):
        items = [i for i in items if i["bytes"] >= args.min_mb * 1024 * 1024]
    return items


def _summary_from_data(data: dict, top: int = 8) -> dict:
    """从扫描结果构造紧凑摘要（总占用 / 三档统计 / Top 目录 / 扫描根）。"""
    totals = data["totals"]
    items = data["items"]
    top_items = sorted(items, key=lambda x: -x["bytes"])[: max(1, top)]
    return {
        "scanned_at": data.get("scanned_at", ""),
        "totals": {
            "bytes": sum(t["bytes"] for t in totals.values()),
            "files": sum(t["files"] for t in totals.values()),
            "tiers": {t: {"bytes": totals[t]["bytes"], "count": totals[t]["count"],
                          "files": totals[t]["files"]}
                      for t in (W.SAFE, W.CAUTION, W.KEEP)},
        },
        "top_dirs": [{"id": i["id"], "path": i["path"], "tier": i["tier"],
                      "category": i["category"], "bytes": i["bytes"],
                      "files": i["files"], "reason": i["reason"]} for i in top_items],
        "roots": data.get("roots", []),
    }


def cmd_summary(args):
    """输出紧凑 JSON 摘要，供对话式呈现：可视化卡片 + 分档汇总。

    设计动机：scan 的完整 JSON 大而碎（group 项成百），agent 每次重新聚合费时。
    summary 一步给出 画卡片所需的全部数字：总占用 / 三档统计 / Top 目录 / 扫描根。
    """
    print(json.dumps(_summary_from_data(_load(args), args.top),
                     ensure_ascii=False, indent=2))


# 结果卡配色
_TIER_COLOR = {W.SAFE: "#97C459", W.CAUTION: "#EF9F27", W.KEEP: "#B4B2A9"}
_TIER_TEXT = {W.SAFE: "#3B6D11", W.CAUTION: "#854F0B", W.KEEP: "#5F5E5A"}


def _wrap_html(inner: str) -> str:
    return ('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>WorkBuddy 清理扫描结果</title></head>'
            '<body style="margin:0;font-family:-apple-system,\'Segoe UI\',\'Microsoft YaHei\','
            'sans-serif;background:#fff;color:#1a1a1a;">'
            '<div style="max-width:680px;margin:24px auto;padding:0 16px;">'
            + inner + '</div></body></html>')


def _card_parts(s: dict) -> str:
    """结果卡片段：4 指标卡 + 三色占比条 + Top 目录条形表 + 风险尾注。"""
    def fmt_gb(n: float) -> str:
        return f"{n / 1024 ** 3:.2f} GB" if n >= 1024 ** 3 else f"{n / 1024 ** 2:.0f} MB"

    t = s["totals"]
    tiers = t["tiers"]
    total = max(1, t["bytes"])
    pct = {k: (round(tiers[k]["bytes"] / total * 100) if tiers[k]["bytes"] else 0)
           for k in tiers}
    pct[W.CAUTION] += 100 - sum(pct.values())          # 归一化到 100%

    top = s["top_dirs"]
    top1 = max((i["bytes"] for i in top), default=1) or 1

    def _short(p: str) -> str:
        p = p.replace("\\", "/")
        for pre in ("WorkBuddy/", ".workbuddy/"):
            if pre in p:
                p = p[p.index(pre):]
                break
        return p if len(p) <= 26 else p[:25] + "…"

    rows = []
    for i in top:
        w = max(2, round(i["bytes"] / top1 * 100))
        rows.append(
            '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">'
            '<div style="width:190px;font-size:12px;white-space:nowrap;overflow:hidden;'
            'text-overflow:ellipsis;" title="' + esc(i["path"]) + '">' + esc(_short(i["path"])) + '</div>'
            '<div style="flex:1;background:#F1EFE8;border-radius:4px;height:15px;">'
            '<div style="width:' + str(w) + '%;background:' + _TIER_COLOR[i["tier"]] +
            ';height:100%;border-radius:4px;"></div></div>'
            '<div style="width:58px;text-align:right;font-size:12px;color:#888;">'
            + fmt_gb(i["bytes"]) + '</div></div>')

    def _card(label, color, big, sub):
        return ('<div style="background:#F5F4F0;border-radius:8px;padding:12px 14px;">'
                '<div style="font-size:12px;color:#666;">' + label + '</div>'
                '<div style="font-size:22px;font-weight:500;margin:2px 0;color:' + color + ';">'
                + big + '</div>'
                '<div style="font-size:11px;color:#999;">' + sub + '</div></div>')

    parts = [
        '<div style="display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;'
        'margin-bottom:14px;">',
        _card("总占用", "#1a1a1a", fmt_gb(t["bytes"]), f"{t['files']:,} 个文件"),
        _card("🟢 安全可删", _TIER_TEXT[W.SAFE], fmt_gb(tiers[W.SAFE]["bytes"]),
              f"{tiers[W.SAFE]['count']} 项 · 零风险"),
        _card("🟡 可选清理", _TIER_TEXT[W.CAUTION], fmt_gb(tiers[W.CAUTION]["bytes"]),
              f"{tiers[W.CAUTION]['count']} 项 · 丢回溯"),
        _card("🔴 建议保留", _TIER_TEXT[W.KEEP], fmt_gb(tiers[W.KEEP]["bytes"]),
              f"{tiers[W.KEEP]['count']} 项 · 运行依赖"),
        '</div>',
        '<div style="display:flex;height:14px;border-radius:7px;overflow:hidden;margin-bottom:8px;">',
        f'<div style="width:{pct[W.SAFE]}%;background:{_TIER_COLOR[W.SAFE]};"></div>',
        f'<div style="width:{pct[W.CAUTION]}%;background:{_TIER_COLOR[W.CAUTION]};"></div>',
        f'<div style="width:{pct[W.KEEP]}%;background:{_TIER_COLOR[W.KEEP]};"></div>',
        '</div>',
        '<div style="display:flex;flex-wrap:wrap;gap:16px;font-size:12px;color:#666;margin-bottom:18px;">',
        f'<span>🟢 安全可删 {pct[W.SAFE]}%</span>'
        f'<span>🟡 可选清理 {pct[W.CAUTION]}%</span>'
        f'<span>🔴 建议保留 {pct[W.KEEP]}%</span>',
        '</div>',
        f'<div style="font-size:13px;font-weight:500;margin-bottom:10px;">占用 Top {len(top)} 目录</div>',
        *rows,
        '<div style="margin-top:16px;font-size:12px;color:#999;">扫描时间 '
        + esc(s["scanned_at"] or "—")
        + ' · 雪人老师·WorkBuddy 清理专家[高级版] 生成 · 删除尽量移入回收站，但<b>不保证可还原</b>：'
        '配额超限/过大文件/回收站不可用时 Windows 会直接永久删除且无法察觉，'
        '清空回收站亦会彻底消失，重要文件请先备份</div>',
    ]
    return "".join(parts)


def write_card(s: dict, path: str) -> None:
    """（兼容）仅生成结果卡 HTML。"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(_wrap_html(_card_parts(s)))


def _body_parts(s: dict, data: dict) -> str:
    """正文段：总体情况 + 🟢 表格 + 🟡 列表 + 🔴 白名单 + 确认引导。

    结构与 assets/RESULT_TEMPLATE.md 一致，视觉复刻效果基准。
    """
    import os as _os

    def fmt_gb(n: float) -> str:
        return f"{n / 1024 ** 3:.2f} GB" if n >= 1024 ** 3 else f"{n / 1024 ** 2:.0f} MB"

    t = s["totals"]
    tiers = t["tiers"]
    items = data["items"]
    code = lambda x: ('<code style="background:#F5F4F0;padding:1px 5px;border-radius:4px;'
                      'font-size:12px;">' + esc(x) + '</code>')  # noqa: E731
    h = lambda txt: ('<div style="font-size:14px;font-weight:500;margin:18px 0 8px;">'
                     + txt + '</div>')  # noqa: E731
    p = lambda txt: ('<p style="font-size:13px;line-height:1.7;margin:6px 0;">'
                     + txt + '</p>')  # noqa: E731

    data_root = next((r["path"] for r in s.get("roots", []) if r.get("kind") == "data"), "")
    intro = ""
    if data_root:
        real = _os.path.realpath(data_root)
        if _os.path.normcase(real) == _os.path.normcase(data_root):
            intro = (p(code(data_root) + ' 共 <b>' + fmt_gb(t["bytes"]) + '</b> / '
                       + f"{t['files']:,}" + ' 个文件。'))
        else:
            intro = (p(code(data_root) + ' 是指向 <code>' + esc(real) + '</code> 的符号链接，'
                       '实际数据共 <b>' + fmt_gb(t["bytes"]) + '</b> / '
                       + f"{t['files']:,}" + ' 个文件。'))

    def _tbl(rows):
        trs = "".join(
            '<tr>'
            '<td style="padding:6px 10px;border-bottom:1px solid #eee;word-break:break-all;">'
            + code(i["path"]) + '</td>'
            '<td style="padding:6px 10px;border-bottom:1px solid #eee;text-align:right;'
            'white-space:nowrap;">' + fmt_gb(i["bytes"]) + '</td>'
            '<td style="padding:6px 10px;border-bottom:1px solid #eee;">' + esc(i["reason"]) + '</td>'
            '</tr>' for i in rows)
        return ('<table style="width:100%;border-collapse:collapse;font-size:12px;">'
                '<tr style="color:#666;">'
                '<th style="text-align:left;padding:6px 10px;border-bottom:1px solid #ddd;">路径</th>'
                '<th style="text-align:right;padding:6px 10px;border-bottom:1px solid #ddd;">大小</th>'
                '<th style="text-align:left;padding:6px 10px;border-bottom:1px solid #ddd;">依据</th>'
                '</tr>' + trs + '</table>')

    safe = sorted((i for i in items if i["tier"] == W.SAFE), key=lambda x: -x["bytes"])
    caut = sorted((i for i in items if i["tier"] == W.CAUTION), key=lambda x: -x["bytes"])
    rt = next((i for i in items if i["category"] == "runtime"), None)

    caut_rows = "".join(
        '<div style="margin:5px 0;font-size:13px;">' + code(i["path"]) + ' → <b>'
        + fmt_gb(i["bytes"]) + '</b>（' + esc(i["reason"]) + '）</div>' for i in caut)

    keep_line = (code("binaries/") + (' ' + fmt_gb(rt["bytes"]) if rt else '')
                 + '（运行环境）、' + code("skills/") + '、' + code("workbuddy.db") + '、'
                 + code("credentials/") + '、' + code("security/") + '、'
                 + code("settings.json") + '、' + code("plugins/")
                 + '——这些删了会直接影响运行。')

    return (
        h('总体情况') + intro
        + h('🟢 第一档：建议直接删除（' + fmt_gb(tiers[W.SAFE]["bytes"]) + '，无功能影响）')
        + (_tbl(safe) if safe else p('本档暂无可清理项。'))
        + h('🟡 第二档：可清但会丢失历史回溯（约 ' + fmt_gb(tiers[W.CAUTION]["bytes"]) + '）')
        + (caut_rows if caut_rows else p('本档暂无可清理项。'))
        + h('🔴 不可删（' + fmt_gb(tiers[W.KEEP]["bytes"]) + ' 运行时依赖）')
        + p(keep_line)
        + p('完整逐条路径清单见报告文件。建议保留 ' + code("credentials/") + '、'
            + code("security/") + '、' + code("settings.json") + '、' + code("workbuddy.db")
            + '、' + code("binaries/") + '、' + code("skills/")
            + '，这些删了会直接影响运行。')
        + p('确认清理档位后，本工具会把对应文件移入系统回收站（通常可还原；但超出回收站'
            '配额的部分会被系统直接永久删除、无法恢复）。无法替代的内容请先自行备份，'
            + code("credentials/") + '、' + code("security/") + '、' + code("settings.json")
            + '、' + code("workbuddy.db") + '、' + code("binaries/") + '、' + code("skills/")
            + ' 等运行依赖不会被改动。')
    )


def write_result_page(s: dict, data: dict, path: str) -> None:
    """完整版扫描结果页 = 结果卡 + 正文分档明细（复刻效果基准，今后统一按此出）。"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(_wrap_html(_card_parts(s) + _body_parts(s, data)))


def cmd_card(args):
    """生成完整扫描结果页（扫描完 → present_files 打开给用户看）。"""
    data = _load(args)
    s = _summary_from_data(data, args.top)
    write_result_page(s, data, args.out)
    print(f"结果页 → {args.out}")
    print(f"结果卡 → {args.out}")


def cmd_list(args):
    data = _load(args)
    args.tier = args.tier or W.SAFE
    items = _filter(data["items"], args)
    items.sort(key=lambda x: (W.TIER_ORDER[x["tier"]], -x["bytes"]))
    print(f"{len(items)} 条目 / {human(sum(i['bytes'] for i in items))}\n")
    for i in items:
        print(f"[{i['tier']:>7}] {human(i['bytes']):>10}  {_n_paths(i):>4}子项  {i['path']}")
        print(f"          id={i['id']}  {i['reason']}")
    if getattr(args, "out", None):
        write_list_md(items, args.out, W.TIER_LABEL[args.tier])
        print(f"\n清单 → {args.out}")


def cmd_clean(args):
    data = _load(args)
    items = _filter(data["items"], args)
    if not items:
        print("没有匹配的待清理项。")
        return
    do_clean(items, yes=args.yes, dry=args.dry,
             report_path=(getattr(args, "report", "") or None))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wb_cleaner",
        description="WorkBuddy 清理专家（高级版）：扫描并清理 WorkBuddy 相关目录"
                    "（数据区 / 工作区 / 安装区 / 用户缓存）")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("scan", help="扫描并生成报告")
    s.add_argument("--out", help="报告输出路径（.md 或 .html）")
    s.add_argument("--json", help="JSON 结果路径（供 list/card 复用）")
    s.add_argument("--no-user", action="store_true", help="跳过用户目录/Temp")
    s.add_argument("--workers", type=int, default=8)
    s.set_defaults(func=cmd_scan)

    s2 = sub.add_parser("summary", help="输出扫描摘要 JSON（供可视化呈现与引导确认）")
    s2.add_argument("--top", type=int, default=8, help="Top 目录条数（默认 8）")
    s2.add_argument("--json")
    s2.add_argument("--no-user", action="store_true")
    s2.set_defaults(func=cmd_summary)

    cd = sub.add_parser("card", help="生成自包含 HTML 扫描结果卡（扫描完给用户看）")
    cd.add_argument("--out", default="wb-cleaner-card.html", help="卡片输出路径")
    cd.add_argument("--top", type=int, default=8, help="Top 目录条数（默认 8）")
    cd.add_argument("--json")
    cd.add_argument("--no-user", action="store_true")
    cd.set_defaults(func=cmd_card)

    l = sub.add_parser("list", help="列出扫描条目（只读）")
    l.add_argument("--tier", choices=[W.SAFE, W.CAUTION, W.KEEP], default=W.SAFE)
    l.add_argument("--category")
    l.add_argument("--min-mb", type=float)
    l.add_argument("--id", nargs="+")
    l.add_argument("--out", help="把清单导出为 Markdown（如 workbuddy-delete-list.md）")
    l.add_argument("--json")
    l.add_argument("--no-user", action="store_true")
    l.set_defaults(func=cmd_list)

    c = sub.add_parser("clean", help="移入回收站（默认 dry-run）")
    c.add_argument("--tier", choices=[W.SAFE, W.CAUTION], default=W.SAFE)
    c.add_argument("--id", nargs="+", help="按 ID 精确清理（可多个）")
    c.add_argument("--category")
    c.add_argument("--min-mb", type=float)
    c.add_argument("--dry", action="store_true", help="预演，不删除")
    c.add_argument("--yes", action="store_true", help="确认执行删除")
    c.add_argument("--report", default="workbuddy-cleanup-done.md",
                   help="执行结果报告路径（仅 --yes 实删时写出；传空串禁用）")
    c.add_argument("--json")
    c.add_argument("--no-user", action="store_true")
    c.set_defaults(func=cmd_clean)

    d = sub.add_parser("doctor", help="环境自检")
    d.set_defaults(func=cmd_doctor)
    return p


def main(argv=None):
    p = build_parser()
    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        p.print_help()
        return 1
    return args.func(args) or 0


if __name__ == "__main__":
    raise SystemExit(main())
