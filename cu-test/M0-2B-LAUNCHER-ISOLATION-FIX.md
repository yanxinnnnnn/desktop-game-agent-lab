# M0.2b Phase A1.6 — DSH_HOME 启动器缺陷修复与离线验证

| 项目 | 内容 |
|---|---|
| 结论 | 变量作用域 Bug **已修复**，跨阶段目录一致性 + fail-closed 校验已实现 |
| 合成测试 | **16/16 PASS** |
| 真实 DSH 运行 | **NOT RUN**（本轮禁止） |
| 模型请求 | **0** |
| 真实用户配置变更 | **NONE** |

---

## 1. 原始 Bug 根因

`joint_experiment.ps1` 原实现：

```powershell
if ($Phase -eq 'preflight') { $homeDir = Join-Path $exp 'dsh-home' }   # L13
if ($Phase -eq 'run')       { $psi.EnvironmentVariables['DSH_HOME'] = $homeDir }  # L67
```

两个 Phase 是**两次独立的脚本调用**，变量不跨调用保留 →
`run` 阶段 `$homeDir` 为 `$null` → `DSH_HOME` 被写成**空字符串** →
`resolveDshHome` 把空值视为"未设置" → 回退 `~/.dsh` →
`prepareProfile()` 写入真实 `~/.dsh/profiles/headless/cordis.yml`。

**与实测现象完全吻合**（内容 `[]`、mtime 落在启动时刻、用户 patch 未被改）。

---

## 2. 修复方案

### 2.1 单一真相源：实验根 + manifest

- 启动器新增**必填**参数 `-ExpRoot`（同一根目录贯穿两个阶段）；
- `preflight` 成功后写出 `manifest.json`：
  `{ schema, exp_root, home, base_sha256, created_at }`；
- `run` **必须**读取该 manifest，缺失即 `BLOCKED`，**不会**自行新建 Home。

### 2.2 Fail-Closed 校验（`launcher_guard.ps1`）

`Assert-ExperimentHome` 依次校验：

| # | 校验 | 失败码 |
|---|---|---|
| 1 | 非 null / 是字符串 | `E_HOME_NULL` / `E_HOME_TYPE` |
| 2 | 非空、非纯空白 | `E_HOME_EMPTY` |
| 3 | 绝对路径 | `E_HOME_RELATIVE` |
| 4 | 可规范化 | `E_HOME_INVALID` |
| 5 | 不等于真实 Home / `~/.dsh` | `E_HOME_IS_REAL_HOME` / `E_HOME_IS_REAL_DSH` |
| 6 | 位于 `-ExpRoot` 前缀内 | `E_HOME_OUTSIDE_ROOT` |
| 7 | 目录存在 | `E_HOME_MISSING` |
| 8 | 自身非 reparse point | `E_HOME_REPARSE` |
| 9 | 父链无 reparse point | `E_HOME_REPARSE_CHAIN` |

`Assert-Manifest`：schema 正确 + `exp_root` 与本次 `-ExpRoot` 规范化后一致，否则 `BLOCKED`。

`Assert-ChildEnvironment`（在 `Start()` **之前**调用）：

| 校验 | 失败码 |
|---|---|
| 存在 `DSH_HOME` | `E_ENV_NO_DSH_HOME` |
| 非空、绝对、可规范化 | `E_ENV_EMPTY_DSH_HOME` / `E_ENV_RELATIVE` |
| 与已验证 Home 一致 | `E_ENV_MISMATCH` |
| 不含真实凭据名（`OPENAI_API_KEY` 等） | `E_ENV_HAS_REAL_CREDENTIAL` |

任一失败 → 打印 `RUN=BLOCKED <code>` 并 return，**不启动子进程**。

---

## 3. 修复前后差异

| 项 | 修复前 | 修复后 |
|---|---|---|
| Home 来源 | Phase 内局部变量 | `-ExpRoot` + manifest |
| 跨阶段一致性 | 无 | manifest 的 `exp_root` 强校验 |
| 空值检测 | 无 | `E_HOME_EMPTY` |
| 真实目录检测 | 无 | `E_HOME_IS_REAL_HOME/.dsh` |
| 逃逸检测 | 无 | reparse point（自身 + 父链） |
| 子进程环境校验 | 无 | `Start()` 前强制 |
| 预检缺失 | 静默继续 | `E_NO_MANIFEST` 阻断 |

---

## 4. 合成测试（16 项）

| # | 场景 | 期望 | 实际 |
|---|---|---|---|
| 1 | 正确 preflight→run | OK | ✅ OK |
| 2 | Home 未初始化 | BLOCKED | ✅ `E_HOME_EMPTY`（`$null` 绑定到 `[string]` 退化为空） |
| 3 | Home 为空 | `E_HOME_EMPTY` | ✅ |
| 4 | 仅空白 | `E_HOME_EMPTY` | ✅ |
| 5 | 相对路径 | `E_HOME_RELATIVE` | ✅ |
| 6 | 目录不存在 | `E_HOME_MISSING` | ✅ |
| 7 | manifest 根不一致 | `E_MANIFEST_ROOT_MISMATCH` | ✅ |
| 8 | manifest 缺失 | `E_MANIFEST_MISSING` | ✅ |
| 9 | 指向真实 `~/.dsh` | `E_HOME_IS_REAL_DSH` | ✅ |
| 10 | junction 逃逸 | `E_HOME_REPARSE` | ✅ |
| 11 | 子进程无 `DSH_HOME` | `E_ENV_NO_DSH_HOME` | ✅ |
| 12 | 子进程 Home 不一致 | `E_ENV_MISMATCH` | ✅ |
| 13 | 子进程 Home 正确 | OK | ✅ |
| 14 | 合成替身收到 `DSH_HOME` | OK | ✅ |
| 15 | 替身回显值与期望一致 | MATCH | ✅ |
| 16 | 不携带真实凭据 | OK | ✅ |

**16/16 PASS**（含 junction 用例，本机可创建 junction，未 SKIP）。
替身使用 `cmd.exe /c echo %DSH_HOME%`，**未调用 `dsh.cmd` 或 Electron**。

---

## 5. 脱敏检查结果

- 测试中 `DSH_HOME` 指向 `%TEMP%\launcher-test-*\exp\dsh-home`（临时）；
- 断言其**不等于** `%USERPROFILE%` 与 `%USERPROFILE%\.dsh`；
- 未读取、未打印任何真实环境变量值或凭据；
- 未触碰真实 `~/.dsh`。

---

## 6. 修改文件

| 文件 | 变更 |
|---|---|
| `cu-test/tools/joint_experiment.ps1` | 重写：`-ExpRoot` 必填、manifest、`Start()` 前校验 |
| `cu-test/tools/launcher_guard.ps1` | **新增**：路径与子进程环境守卫 |
| `cu-test/tools/test_launcher_isolation.ps1` | **新增**：16 项合成测试 |

---

## 7. 剩余风险与 Unknown

| # | 项 | 说明 |
|---|---|---|
| R1 | 真实 DSH 未验证 | 校验只证明"启动器构造正确"，不证明 DSH 运行时隔离 |
| R2 | `--profile headless --help` 是否 boot | 仍 Unknown（不影响本轮结论） |
| R3 | manifest 可被手工伪造 | 仅本地工具，威胁模型为"脚本 bug"而非恶意 |
| R4 | reparse 检测依赖 FileAttributes | 极少数挂载点可能不可见 |

---

## 8. 下一次真实实验的前置条件（建议）

1. 修复后的启动器 **preflight 必须返回 `PREFLIGHT=OK`**；
2. `run` 必须打印 `RUN=OK home=<临时目录>` 才真正启动；
3. 实验中确认 `cordis.yml` 出现在**临时 home** 而非 `~/.dsh`；
4. 仍需 **Founder 单独授权**（本轮未运行）。

---

## 9. 验收表

| 验收项 | 结果 |
|---|---|
| 变量作用域 Bug 修复 | **PASS** |
| 跨阶段目录一致性 | **PASS**（manifest + 根校验） |
| 路径安全检查 | **PASS**（9 项） |
| 子进程环境校验 | **PASS**（`Start()` 前） |
| 合成测试 | **16/16 PASS** |
| 真实 DSH 运行 | **NOT RUN** |
| 模型请求 | **0** |
| 真实用户配置变更 | **NONE** |

---

## 10. 边界

未启动 DSH/Electron、未调用模型（0 Token）、未读 `~/.dsh` 内容/凭据/进程环境、
未修改 DSH 安装目录、未 commit/push/PR。
本轮仅修改 `cu-test/tools/` 下三个本地实验工具文件。
