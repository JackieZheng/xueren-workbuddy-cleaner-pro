# 雪人老师 · WorkBuddy 清理专家[高级版]

一个 **WorkBuddy 专用**的深度清理工具。它不只扫 `.workbuddy`——会自动找出
**所有跟 WorkBuddy 有关的目录**，分好类，出一份能直接看的报告，再把你确认过的垃圾
**移进系统回收站**（通常可还原；但超出回收站配额的部分会被系统**直接永久删除，无法恢复**）。

纯 Python 标准库，零依赖、零 CDN、绝不联网。

> 只想看分析报告、自行手动删除？请用基础版「雪人老师·WorkBuddy 清理专家」。

## 界面预览

扫描完成后的完整结果页（下图来自示例数据）：

![扫描结果页预览](https://raw.githubusercontent.com/JackieZheng/xueren-workbuddy-cleaner-pro/main/assets/screenshot-demo.png)

---

## 它到底扫哪些地方

这些路径**不写死**——工具按多种线索自动定位（WorkBuddy 的应用配置、系统安装信息、正在运行的程序、常规位置），
换盘符、换用户名、换安装位置都不用改配置。跑一次 `doctor` 就能看到每一处是从哪儿找到的。

| 区域 | 路径（自动定位） | 扫什么 |
|---|---|---|
| **数据区** | `~/.workbuddy`（含软链背后的真实盘，自动去重） | 日志、调试追踪、缓存、会话快照、旧技能快照、剪贴板图片、散落备份 |
| **工作区** | `~/WorkBuddy/<时间戳>` | 每次会话的工作目录（体积大头常在这里） |
| **安装区** | 系统安装信息里记录的位置 | 只挑 `debug.log`、`*.bak`、崩溃转储 |
| **用户区** | `%LOCALAPPDATA%/WorkBuddy`、`%APPDATA%/WorkBuddy` | 应用日志、Electron 缓存、GPU/JS 缓存 |
| **升级残留** | `%LOCALAPPDATA%/@genieworkbuddy-desktop-updater` | 已下载的 `installer.exe`（常有好几百 MB） |
| **临时/缓存** | `%TEMP%`、`~/.cache` | **只挑**名字以 `workbuddy / codebuddy / wb- / genie / miora / xueren` 开头的 |
| **工具/资源** | 安装目录 `resources`、内置技能、`~/.workbuddy/skills` | **只读展示**，不参与清理 |

---

## 两种用法

### 一、直接对话（推荐）

只要说「深度清理 WorkBuddy」「一键清理 WorkBuddy」「WorkBuddy 占用太大」之类的，技能就会：
自检 → 扫描 → **出一张可视化结果卡**（总占用 / 三档分类 / Top 目录条形表）→
**用选择题问你要清到哪一档** → 按你的选择清理 → 报告释放了多少。
你全程不需要记任何命令。

### 二、手动跑命令

```bash
# 自检：看看发现了哪些目录、回收站能不能用
python scripts/wb_cleaner.py doctor

# 扫描出报告（只读，不删任何东西）
python scripts/wb_cleaner.py scan --out wb-report.html

# 生成完整结果页（结果卡 + 三档明细，双击就能看）
python scripts/wb_cleaner.py card

# 摘要 JSON（同上数据的机器可读版）
python scripts/wb_cleaner.py summary

# 看清单（按分级/体积筛）
python scripts/wb_cleaner.py list --tier safe --min-mb 10

# 清理（默认 dry-run，加 --yes 才真删）
python scripts/wb_cleaner.py clean --tier safe --dry
python scripts/wb_cleaner.py clean --tier safe --yes
```

---

## 产出物

| 产物 | 何时产生 | 说明 |
|---|---|---|
| `workbuddy-cleanup-report.md` | 扫描后 | 分级明细报告（逐条路径 + 大小 + 依据） |
| `wb-cleaner-card.html` | 扫描后 | 完整结果页（指标卡 + 占比条 + Top 目录 + 三档明细），双击就能看 |
| `workbuddy-delete-list.md` | 你确认清理后、执行前 | 逐条待删清单 |
| `workbuddy-cleanup-done.md` | 执行完成后自动 | 执行结果（按类别汇总 + 成功/失败 + 风险提示） |
| 扫描 JSON | 扫描后 | 默认 `%TEMP%\wb_cleaner_last_scan.json`，供 `list` / `clean` / `card` 复用 |

---

## 三级分类（核心规则）

- 🟢 **安全清理**：运行日志、沙箱日志、旧技能快照、剪贴板图片、`tmp`、升级包、散落 `.bak/.tmp`
- 🟡 **谨慎清理**：调试追踪 `traces`、会话快照 `sessions`、`projects`、`blobs`、`file-history`、旧会话工作区
  —— **删了会丢失历史回溯能力**
- 🔴 **不可删除**：`binaries/`（运行时）、`skills/`、`workbuddy.db`、`credentials/`、`security/`、
  `settings.json`、`plugins/`（非 cache）、用户档案、程序本体 —— **代码层硬白名单，任何参数都动不了**

**龄期保护**：日志/追踪/会话默认保留最近若干天（日志 7 天、今天的沙箱日志、14 天内的会话目录），
近期用过的会自动降级为「不可删除」。

---

## 关键注意

1. **只读优先**：`doctor`/`scan`/`list` 绝不删除；`clean` 默认 dry-run，必须显式 `--yes`。
2. **删除不保证可还原（⚠️ 必须清楚）**：工具会「尽量」把文件移入系统回收站，
   但**回收站不是保险箱**，以下情况 Windows 会直接永久删除、且工具无法察觉是否成功进了回收站：
   - 单盘回收站**配额超限**（超出的部分被直接永久删除）
   - 单个文件/目录**过大**（超过系统阈值时不进回收站）
   - 所在盘**回收站被禁用或不支持**（如部分网络盘/可移动盘）
   - **事后手动清空回收站**，已进回收站的内容也会彻底消失
   - 跨卷/异常情况下，系统接口可能**静默降级为永久删除**

   因此**任何无法替代的内容，务必在清理前自行备份**。
3. **谨慎档不可回溯**：🟡 谨慎档包含旧会话与历史记录，删除后无法再回溯；
   你未明确要求时不会主动清理。
4. **必须先确认**：执行 `clean --yes` 前，必须获得你对清理档位的明确确认，不会擅自扩大范围。
5. **占用中的文件会跳过**：被运行中的 WorkBuddy 锁住的（如 `plugins/cache`、`app/session`）
   会跳过并提示，不会报错中断。想清这些请先关闭 WorkBuddy。
6. **工作区目录按整目录评估**：会话目录里常含 `node_modules`、`.cache`、`__pycache__`、
   各类模型缓存（`.hf` 前缀目录）等可再生目录，
   工具不会自动删它们，只把整个会话目录列为「谨慎清理」交给你判断。

---

## 文档分工

| 文件 | 面向 | 内容 |
|---|---|---|
| `SKILL.md` | 模型 | 骨架：是什么 / 怎么跑 / 关键限制 / 风险提示 |
| `WORKFLOW.md` | 模型 | 详细流程：目录发现、规则表、清理步骤、故障排查 |
| `README.md` | 用户 | 你正在看的这份：能扫什么、怎么用、注意什么 |
| `docs/DEVLOG.md` | 维护者 | 版本演进记录 |

---

## 许可

MIT
