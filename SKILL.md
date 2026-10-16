---
id: xueren-workbuddy-cleaner-pro
name: 雪人老师·WorkBuddy 清理专家[高级版]
title: 雪人老师·WorkBuddy 清理专家[高级版]
description: WorkBuddy 专用深度清理工具（高级版）：自动找出所有与 WorkBuddy 相关的目录（数据目录、工作区、程序安装目录、用户缓存等），按 安全/谨慎/不可删 三级分类，出报告并引导选档，确认后把垃圾移入系统回收站（通常可还原；超出回收站配额的部分会被系统直接永久删除，无法恢复）。当用户说"帮我深度清理 WorkBuddy""一键清理 WorkBuddy""帮我删除 WorkBuddy 缓存""清理并释放空间"等希望工具代为删除的意图时触发。只看报告不代删请用基础版。
slug: xueren-workbuddy-cleaner-pro
displayName: 雪人老师·WorkBuddy 清理专家[高级版]
summary: 自动找出所有 WorkBuddy 相关目录，三级分类出报告，确认档位后代为移入回收站。
version: 1.0.0
author: 雪人
license: MIT
display_name: xueren-workbuddy-cleaner-pro
trigger: ["深度清理 WorkBuddy", "一键清理 WorkBuddy", "帮我删除 WorkBuddy 缓存", "清理 WorkBuddy 并释放空间", "WorkBuddy 清理高级版"]
examples: "用户：帮我深度清理 WorkBuddy → 扫描后 show_widget 内联出回复卡片（指标卡+占比条）+ card 生成完整结果页用 present_files 打开（报告），正文按模板给分档汇总，用选择题引导选清理档位，确认后 clean --yes 移入回收站，收尾报告释放量。"
platforms: [ima, WorkBuddy, QClaw]
github: https://github.com/JackieZheng/xueren-workbuddy-cleaner-pro
---

# 雪人老师·WorkBuddy 清理专家[高级版]

WorkBuddy 专用深度清理工具：自动找出所有与 WorkBuddy 相关的目录，
按「安全清理 / 谨慎清理 / 不可删除」三级分类出报告，再把确认过的垃圾
移入系统回收站（通常可还原，但存在无法恢复的情形，见文末⚠️风险提示）。

> 只想看分析报告、自行手动删除的用户请用基础版「雪人老师·WorkBuddy 清理专家」。

## 标准交互流程（对话式闭环）

1. `doctor` 自检 → `scan --out workbuddy-cleanup-report.md` 扫描（只读），报告 present_files 打开
2. **两种呈现都要，职责不同**：
   - **回复卡片**（对话内嵌）：用 `show_widget` 渲染彩色指标卡（4 指标卡 + 三色占比条），
     与正文同一条回复直接可见
   - **报告**（文件）：`card` 生成 HTML 完整结果页（`assets/result-template.html` 基准）
     → `present_files` 打开
3. **回复正文严格按 `assets/RESULT_TEMPLATE.md` 模板输出**
   （效果基准：`assets/result-template.html`；数据取 `summary` / `list`）
   → 用选择题引导选档位，不要让用户拼命令
4. 执行前 `list --tier <档> --out workbuddy-delete-list.md` 导出逐条待删清单给用户过目
5. `clean ... --yes` 执行（**自动写 `workbuddy-cleanup-done.md`**）→
   收尾报告成功 / 跳过 / 释放量，三份报告 + 结果卡一并 present_files

## 命令

```bash
python scripts/wb_cleaner.py doctor                    # 自检（含回收站可用性）
python scripts/wb_cleaner.py scan --out workbuddy-cleanup-report.md  # 扫描报告（只读）
python scripts/wb_cleaner.py card                      # 完整结果页 wb-cleaner-card.html
python scripts/wb_cleaner.py summary                   # 摘要 JSON（指标卡/正文取数）
python scripts/wb_cleaner.py list --tier safe --min-mb 10  # 按分级/体积看明细
python scripts/wb_cleaner.py clean --tier safe --dry   # 预演（默认就是 dry-run）
python scripts/wb_cleaner.py clean --tier safe --yes   # 确认后执行（自动写执行结果报告）
```

## 关键限制

- 🔴 运行环境、技能、数据库、凭据、配置、用户档案、程序本体是**硬白名单**，任何参数都不会动
- `clean` 默认预演，必须显式 `--yes` 才会真正删除；删除只进系统回收站，**但不保证可还原**
  （回收站配额超出的部分会被系统直接永久删除，见文末⚠️风险提示）
- 🟡 谨慎档包含旧会话与历史记录，删除后会丢失历史回溯能力，用户未明确要求时不主动清理
- 被正在运行的 WorkBuddy 占用的文件会自动跳过并提示
- `doctor` 会标注每个目录的定位来源；`list --tier keep` 可查看全部保留项

## ⚠️ 操作风险提示

**删除不保证可还原**：工具会「尽量」把文件移入系统回收站，但**回收站不是保险箱**，
以下情况 Windows 会直接永久删除、且工具无法察觉：

- 单盘回收站**配额超限**（超出的部分被直接永久删除）
- 单个文件/目录**过大**（超过系统阈值时不进回收站）
- 所在盘**回收站被禁用或不支持**（如部分网络盘/可移动盘）
- **事后手动清空回收站**，已进回收站的内容也会彻底消失
- 跨卷/异常情况下，系统接口可能**静默降级为永久删除**

因此**任何无法替代的内容，务必在清理前自行备份**；执行前后都要把这条风险讲清楚。
