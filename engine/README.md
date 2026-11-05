# CU Desktop Engine

Windows PowerShell 5.1 的最小化桌面自动化引擎，源自 POC-001 ~ POC-004b 的验证代码，
在 POC-005 完成工程化。**当前只服务 2048 测试场景，不含任何《梦幻西游》逻辑。**

```
engine/
├── Desktop.psm1    通用桌面层：窗口定位 / 截图 / 坐标换算 / 键盘鼠标输入
├── Game2048.psm1   2048 棋盘定位、颜色+字形双通道识别、规则模拟与转移校验
├── Run2048.ps1     自动执行入口（默认 30 次有效移动 / 15 分钟上限）
├── Tests.ps1       19 项回归测试（5 项依赖本地证据截图，缺失时 SKIP）
└── README.md
```

## 运行环境

| 项 | 值 |
|---|---|
| OS | Windows 11（POC 验证机 26200） |
| Shell | Windows PowerShell 5.1 |
| 依赖 | 仅 `System.Drawing` + `user32/dwmapi/kernel32/advapi32`，无第三方包 |

## ⚠️ 执行策略

POC 验证机 `Get-ExecutionPolicy` = **Restricted**：`.ps1` 与 `Import-Module` 都会被拒绝。
仓库**不修改任何系统设置**，推荐内存加载：

```powershell
$base = '<repo>\engine'
. ([scriptblock]::Create((Get-Content "$base\Desktop.psm1" -Raw -Encoding UTF8)))
. ([scriptblock]::Create((Get-Content "$base\Game2048.psm1" -Raw -Encoding UTF8)))
```

> 必须带 `-Encoding UTF8`。PS 5.1 的 `Get-Content` 默认按 ANSI/GBK 读取，直接读 UTF-8 文件会乱码并导致解析失败。

若你的环境策略允许，也可以正常 `Import-Module .\Desktop.psm1`。

## Desktop 模块

| 函数 | 说明 |
|---|---|
| `Get-DpiInfo` | 返回 DPI / 缩放比例 |
| `Get-WindowInfo -TitleLike '2048'` | 按标题子串定位窗口；**每次调用重新查询几何**；返回 HWND、PID、矩形、进程名、完整性级别、是否前台 |
| `Get-WindowScreenshot -WindowInfo $w.Data [-SaveTo png]` | 截取窗口当前矩形，返回 `Bitmap`（调用方 `Dispose`） |
| `Set-TargetWindowFocus -WindowInfo $w.Data` | 置前，最多 2 次尝试 |
| `Invoke-KeyPress -WindowInfo $w.Data -Key Down` | **输入前校验前台**；非前台直接拒绝（`E_NOTFOREGROUND`） |
| `Invoke-MouseClick -WindowInfo $w.Data -X -Y` | 屏幕物理坐标点击；越界拒绝 |
| `ConvertTo-ScreenPoint -Shot $s.Data -X -Y` | 截图内坐标 → 屏幕物理坐标 |

**约定**：所有函数返回 `@{ Ok; Code; Message; Data }`，不抛异常、不无限重试；坐标一律为物理像素（进程启动时已 `SetProcessDPIAware`）。

## Game2048 模块

| 函数 | 说明 |
|---|---|
| `Find-2048Board -Bitmap $bmp [-Region @{X;Y;W;H}]` | 扫描棋盘底色 `#bbada0` 动态定位；自动把矩形校正为正方形（排除同色的计分框） |
| `Read-2048Board -Bitmap $bmp -BBox $bb [-LearnTemplates]` | 逐格识别，返回 `Values`(int[16]) / `Cells`(含 Confidence) / `LowConfidence` |
| `Move-Board2048` / `Validate-2048Move` / `Get-LegalMoves` | 纯规则模拟（不触碰 UI） |
| `Verify-2048Transition -Before -After -Dir` | 校验移动前后是否符合规则，**允许 1 个随机新方块（2 或 4）** |
| `Reset-2048Templates` / `Get-2048TemplateStats` | 字形模板管理 |

### 识别：颜色 + 字形双通道

1. **底色**：格内多点取众数颜色 → 匹配 12 档官方调色板。
2. **字形**：以该格底色为基准，提取"亮度差 > 55"的墨迹 → 归一化 8×8 签名 → 与学习到的模板比对（汉明距离）。

置信度规则：有底色无字形 / 有字形无底色 → `Low`；该数值已有模板但字形强指向另一数值（距离 ≤ 4）→ `Low`；
该数值无模板且颜色距离较大 → `Medium`；其余 `High`。`Run2048.ps1` 遇到 `LowConfidence > 0` 立即停止。

## Run2048 使用

```powershell
$env:CU_ENGINE_DIR = '<repo>\engine'
$sb = [scriptblock]::Create((Get-Content "$env:CU_ENGINE_DIR\Run2048.ps1" -Raw -Encoding UTF8))
& $sb -MaxValidMoves 30 -MaxMinutes 15
```

每轮：`OBSERVE → CHECK → DECIDE → FOCUS → ACT → VERIFY`

- 仅当窗口不在前台时才置前，不每轮点击棋盘
- 每步后用 `Verify-2048Transition` 判定是否真生效（**不看 `SendInput` 返回值**）
- 连续 2 次失败 / 低置信 / 棋盘定位失败 → 停止并写日志

**决策策略（引擎内置，非模型逐轮决策）**：对 4 个合法方向各模拟一次，
按 `合并得分 × 1.5 + 空位数 × 0.6 + 最大块在右下角 × 2` 打分取最高。
POC-005 的 30 步即由该函数驱动。

## 测试

```powershell
$base = '<repo>\engine'
. ([scriptblock]::Create((Get-Content "$base\Desktop.psm1" -Raw -Encoding UTF8)))
. ([scriptblock]::Create((Get-Content "$base\Game2048.psm1" -Raw -Encoding UTF8)))
. ([scriptblock]::Create((Get-Content "$base\Tests.ps1"   -Raw -Encoding UTF8)))
```

共 19 项：DPI / 窗口定位 / 身份校验 / 截图尺寸 / 坐标换算 / 几何变化后重新定位 /
棋盘定位 / 2·8·16 识别 / 白字瓦片 / 纯白图与遮挡图优雅失败 / 2 与 4 调色板区分 /
合成 4 瓦片 / 字形模板学习与跨图匹配 / 合并 / 非法移动 / 随机新方块 / 异常新块 / 多处差异。

其中 **18 项可离线运行**：13 项纯逻辑/窗口测试 + 5 项依赖 `tests/fixtures/2048/` 下已审查的 3 张截图
（`b-final.png`、`b-env3.png`、`board-live.png`）。
剩 1 项（"窗口几何变化后仍能重新定位"）需要本机存在标题含 `2048` 的浏览器窗口。
条件不满足时对应用例标记 **SKIP**（不误报 FAIL）。

## 已知限制

- 每次运行模板库为空，首次遇到 16/32/64 只能依赖颜色（`High` 但未经字形确认）。
- 每数值最多保存 6 个字形签名，运行期学习。
- 2 与 4 的底色 RGB 差仅 23，极端渲染条件下可能混淆 —— 这正是引入字形通道的原因。
- 决策为固定权重打分、无 lookahead；能稳定跑完 30 步，但长期不保证最优。

## 安全边界

- 不提权、不修改注册表 / 执行策略 / 安全设置
- 只操作标题匹配的测试窗口
- 不读 DOM / JS，全部基于屏幕像素
- 未知弹窗、页面跳转、识别低置信 → 立即停止
