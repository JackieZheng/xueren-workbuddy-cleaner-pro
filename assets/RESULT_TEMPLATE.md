# 扫描结果正文模板（RESULT_TEMPLATE）

> **用法**：`scan` + `card` 完成后，回复正文**严格按本模板结构输出**，
> `<...>` 占位符用实测数据替换。视觉基准：`assets/scan-result-example.png`。
> 数据来源：`summary`（totals / top_dirs 含 reason）、`list --tier safe|caution`。

---

这份是 `<扫描根，如 ~/.workbuddy>` 的空间构成与清理档位全景：

〔此处内嵌 show_widget 回复卡片：4 指标卡（总占用 / 🟢 安全可删 / 🟡 可选清理 / 🔴 建议保留）
+ 三色占比条，与正文同一条回复直接可见；
另用 `card` 生成 `wb-cleaner-card.html`（完整结果页）后 present_files 打开作为报告。
效果基准：assets/result-template.html〕

## 总体情况

`<数据目录>` <是指向 `<真实盘>` 的符号链接，实际数据都在 <盘> 盘 / 位于 <盘> 盘>，共 **<总量> GB / <文件数> 个文件**。

## 🟢 第一档：建议直接删除（<X> GB，无功能影响）

| 路径 | 大小 | 依据 |
|---|---:|---|
| `<safe 档条目路径>` | `<大小>` | `<reason，如：运行日志，保留最近 7 天足够>` |
| …（safe 档逐条列全） | | |

## 🟡 第二档：可清但会丢失历史回溯（约 <X> GB）

- `<traces/>` 除最近 7 天 → **<X> GB**（调试追踪，过期无价值）
- `<workspace/sessions/>` 旧会话 → **<X> GB**（最大 `<目录名>` <体积>）
- `<projects/>` 7 天前记录 → **<X> MB**
- …（caution 档按体积降序逐条，`list --tier caution` 取数）

## 🔴 不可删（<X> GB 运行时依赖）

<binaries/> <体积>（其中最大子项一句话）、<skills/>、<workbuddy.db>、<credentials/>、
<security/>、<settings.json>、<plugins/marketplaces/>——这些删了会直接影响运行。

完整逐条路径清单见报告文件。<一句建议：建议保留哪些；哪些被占用需先关 WorkBuddy。>

**需要你明确指定清理范围后我才会动手（删除会走系统回收站，通常可还原；但超出回收站
配额的部分会被系统直接永久删除、无法恢复）：**

〔随后用选择题引导档位：只清第一档（推荐）/ 第一档+追踪缓存 / 两档全清 / 先不动〕
