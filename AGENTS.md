# AGENTS.md

面向在本仓库工作的 AI Agent（以及人类协作者）的约定。**违反其中任何一条，PR 会被直接拒绝。**

---

## 1. 这个仓库是什么

桌面游戏自动化实验的归档 + 引擎仓库。内容分两类：

| 类型 | 位置 | 性质 |
|---|---|---|
| 历史实验报告 | `docs/pocs/` | **只读档案**。除脱敏错误外不得改写结论、数据、失败记录 |
| 可执行引擎 | `engine/` | 可维护代码。改动需对应 Issue、测试与说明 |

POC 报告记录的是"当时真实发生了什么"，包括失败与未解问题。**不许美化历史。**

---

## 2. 硬性禁止

1. **禁止提交**：原始截图、录屏、运行日志（`*.csv` / `summary-*.json` / `*.png`）——**唯一例外**是 `tests/fixtures/2048/` 下经 Founder/CTO 批准、且逐张审查过的 3 张棋盘截图；
   本机路径、Windows 用户名、主机名、账号 / 角色 / 服务器标识、令牌、Cookie、代理配置。
2. **禁止**修改 Windows 安全设置、执行策略、UAC、注册表安全项、系统代理。
3. **禁止**请求提权（管理员权限）运行任何脚本或工具。
4. **禁止**在 CI、迁移、文档任务中执行真实的游戏 / 浏览器自动化操作。
   `Run2048.ps1` 只能由人类在授权环境下手动运行。
5. **禁止**自动合并 PR、自动 push 到 `main`、强推（`force push`）、改写已推送历史。
6. **禁止**通过 DOM / JavaScript / 调试端口读取或修改页面状态来"帮助"识别。

---

## 3. 工作流

```
Issue（含验收标准） → 分支 → 实现 + 测试 → ready-for-review PR → Founder 人工合并
```

- 分支命名：`poc-006/<short-topic>`、`fix/<short-topic>`、`docs/<short-topic>`
- PR 描述必须包含：动机、改动范围、测试结果、安全影响、回滚方式
- 首次 baseline 提交由 Founder 审核后再 push

---

## 4. 环境与运行

| 项 | 值 |
|---|---|
| Shell | Windows PowerShell **5.1**（不是 pwsh 7） |
| 依赖 | 仅 `System.Drawing` + `user32/dwmapi/kernel32/advapi32` |
| 执行策略 | 验证机上为 `Restricted` —— **不要修改它**，用内存加载 |

```powershell
$base = '<repo>\engine'
. ([scriptblock]::Create((Get-Content "$base\Desktop.psm1" -Raw -Encoding UTF8)))
. ([scriptblock]::Create((Get-Content "$base\Game2048.psm1" -Raw -Encoding UTF8)))
. ([scriptblock]::Create((Get-Content "$base\Tests.ps1"   -Raw -Encoding UTF8)))
```

**编码陷阱**：PS 5.1 的 `Get-Content` 默认按 ANSI/GBK 读取。仓库内所有 `.ps1` / `.psm1` / `.md`
均为 UTF-8，读取时必须显式 `-Encoding UTF8`，否则中文乱码并触发解析错误。

---

## 5. 测试

```powershell
# 从仓库根目录运行（fixtures 位于 tests/fixtures/2048/）
. ([scriptblock]::Create((Get-Content "$base\Tests.ps1" -Raw -Encoding UTF8)))
```

- 19 项用例：**18 项可离线运行**（含 `tests/fixtures/2048/` 的 3 张已审查截图），
  1 项需要本机存在标题含 `2048` 的浏览器窗口。
- 条件不满足时对应用例标记 **SKIP**，不是 FAIL。
- **不允许**为了让测试变绿而放宽断言、删除用例或伪造 fixture。

---

## 6. 代码约定

- 所有公开函数返回 `@{ Ok; Code; Message; Data }`，**不抛异常、不无限重试**。
- 坐标一律物理像素；进程启动时 `SetProcessDPIAware`。
- 不缓存窗口矩形；每轮重新查询。
- 输入前必须校验目标窗口在前台，否则返回 `E_NOTFOREGROUND`。
- 新增"游戏专属"逻辑只能进对应模块（如 `Game2048.psm1`），**不得污染 `Desktop.psm1`**。
- 注释与文档使用中文；代码标识符用英文。

---

## 7. 已知问题（不要"顺手修掉"）

| 问题 | 状态 |
|---|---|
| POC-005 计分差异：引擎累计 +300 vs 页面 +304 | **Open**，原因未明。保留原样并在报告中记录 |
| 字形模板冷启动：首次遇到 16/32/64 只靠颜色 | **Open**，已知限制 |
| 2 与 4 底色 RGB 差仅 23 | **Open**，字形通道为补偿手段 |
| `Find-2048Board` 依赖棋盘底色 `#bbada0` | **Open**，主题变更会失效 |

修以上问题需要单独 Issue + 证据，不允许在文档或迁移 PR 中夹带。

---

## 8. 提交前自检

- [ ] `git diff --cached` 中无绝对路径、用户名、账号标识、令牌
- [ ] 除 `tests/fixtures/` 下已批准并审查的 fixture 外，无 `*.png` / `*.csv` / `logs/` 进入暂存区
- [ ] 测试结果已附在 PR 描述中（含 SKIP 项说明）
- [ ] 未修改 `docs/pocs/` 中的历史结论
- [ ] 未触碰系统设置、执行策略、游戏文件
