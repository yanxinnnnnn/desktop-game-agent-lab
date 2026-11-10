# Desktop Game Agent Lab

Windows 桌面游戏自动化的实验仓库：记录从"能不能看见"到"能不能稳定动手"的完整验证过程，
并沉淀一套可复用的 PowerShell + Win32 桌面自动化引擎。

> 本仓库当前处于 **baseline 导入阶段**：迁移 POC-001 ~ POC-005 的已验证成果，
> **尚未包含**任何新的实现工作。下一步见 [docs/roadmap.md](docs/roadmap.md)。

## Windows 开发环境

从 2026-11-10 起，**PowerShell 7.6.9（x64，LTS）是 Windows 上的主要开发 Shell**。Windows PowerShell **5.1 继续保留**，用于现有引擎与脚本的兼容性验证；不能把在 7.6.9 下通过的测试视为已通过 5.1 验证。

| 用途 | Shell | 启动方式 |
|---|---|---|
| 新功能开发与日常离线测试（默认） | PowerShell 7.6.9 | 显式启动下列已安装的 `pwsh.exe` |
| 旧版兼容性验证 | Windows PowerShell 5.1 | `powershell.exe` |

```powershell
& "$env:LOCALAPPDATA\Programs\PowerShell\7.6.9\pwsh.exe"
# 验证当前 Shell
$PSVersionTable.PSVersion.ToString()
```

7.6.9 与 Windows PowerShell 5.1 **并存**，上述方式不依赖修改 PATH。对于涉及 PowerShell 的代码变更，在适用时分别运行 7.6.9 与 5.1 的离线测试，并独立记录 PASS / FAIL / SKIP；未验证的平台不能声称已通过。历史 POC 的实测环境及限制以各自报告为准。安装 Shell 不代表授权运行真实游戏、浏览器、DSH 或其他实机实验。

## 这个仓库证明了什么

| 结论 | 证据 |
|---|---|
| 屏幕截图 + 视觉识别可以稳定读取游戏 UI | POC-001 / POC-002 |
| 视觉驱动的决策闭环可以完成真实任务节点 | POC-002（完成 1 个《梦幻西游》新手任务节点） |
| 合成输入被 Windows 完整性隔离（UIPI）阻断 | POC-001 / POC-003 |
| 提权来源可以只读定位到启动器 Manifest | POC-003 |
| 纯视觉驱动的 2048 可以连续自主游玩 | POC-004 / POC-004b（10/10） |
| 引擎化后可以零干预跑完 30 步 | POC-005（30/30，25.4 秒） |

## 仓库结构

```
.
├── README.md
├── AGENTS.md                 面向 AI Agent 的仓库约定
├── .gitignore
├── docs/
│   ├── roadmap.md            路线图（POC-006 为 proposed）
│   ├── evidence/README.md    未随仓库分发的本地证据清单
│   └── pocs/
│       ├── POC-001.md        截图/识别/输入兼容性（《梦幻西游》）
│       ├── POC-002.md        连续视觉决策（完成一个任务节点）
│       ├── POC-003.md        进程提权来源只读调查
│       ├── POC-004.md        2048 自主游玩（首次尝试，含失败）
│       ├── POC-004b.md       2048 自主游玩（环境恢复后 10/10）
│       └── POC-005.md        引擎工程化 + 30/30 自主移动
├── engine/
    ├── Desktop.psm1          通用桌面层（无游戏逻辑）
    ├── Game2048.psm1         棋盘识别与规则校验
    ├── Run2048.ps1           自动执行入口
    ├── Tests.ps1             19 项回归测试（18 项可离线运行）
    └── README.md
└── tests/
    └── fixtures/2048/        3 张已审查的 2048 棋盘截图（回归测试用）
```

## 快速开始

```powershell
$base = '<repo>\engine'
. ([scriptblock]::Create((Get-Content "$base\Desktop.psm1" -Raw -Encoding UTF8)))
. ([scriptblock]::Create((Get-Content "$base\Game2048.psm1" -Raw -Encoding UTF8)))
. ([scriptblock]::Create((Get-Content "$base\Tests.ps1"   -Raw -Encoding UTF8)))
```

> 执行策略为 Restricted 时需要上面的内存加载方式（仓库不修改任何系统设置）。
> 必须带 `-Encoding UTF8`，否则 PS 5.1 会按本地代码页误读并解析失败。

自动执行（会操作真实浏览器窗口，详见 `engine/README.md` 的安全边界）：

```powershell
$env:CU_ENGINE_DIR = '<repo>\engine'
$sb = [scriptblock]::Create((Get-Content "$env:CU_ENGINE_DIR\Run2048.ps1" -Raw -Encoding UTF8))
& $sb -MaxValidMoves 30 -MaxMinutes 15
```

## 设计原则

1. **感知与执行分离**：`Desktop.psm1` 只负责窗口与输入，`Game2048.psm1` 只负责棋盘与规则。
2. **不缓存坐标**：每轮重新查询窗口几何、重新定位棋盘。
3. **不信任 API 返回值**：`SendInput` 成功不等于游戏响应，必须用前后帧 + 规则校验判定。
4. **低置信即停**：识别不确定时停止并报告，不猜。
5. **最小权限**：不提权、不改系统设置、不注入、不读 DOM/JS。

## 重要说明

- **POC-005 的决策由规则函数完成**，不是模型逐轮决策；该轮"模型决策耗时"不可测，报告中记为 N/A。
- **POC-005 存在一个未解决的计分差异**：引擎累计 +300，页面实际 +304。保持为 open issue，未修正、未美化。
- **原始截图与运行日志不随仓库分发**（含本机环境信息）。仅 3 张经审查、不含账号信息的 2048 棋盘截图作为回归 fixture 入库（`tests/fixtures/2048/`）；其余证据按 [docs/evidence/README.md](docs/evidence/README.md) 单独审批导出。

## 合规边界

本仓库所有实验均遵守：不读写进程内存、不注入 DLL、不逆向/拦截网络协议、不修改游戏文件、
不修改系统安全设置、不执行交易/充值/删除等不可逆操作、不读取账号密码。

## 路线图

见 [docs/roadmap.md](docs/roadmap.md)。POC-006 目前状态为 **proposed（未开始）**。

## 许可

内部实验仓库，暂未选择开源许可证。如需对外发布，请先与 Founder 确认。
