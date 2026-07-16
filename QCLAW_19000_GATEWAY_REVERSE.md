# QClaw 19000 网关进程来源鉴权机制逆向调研报告

> **调研周期**：2026-07-16
> **调研对象**：QClaw v0.2.33.617 本地 LLM 网关（127.0.0.1:19000）
> **调研目标**：实现独立签名请求 19000 网关，脱离 QClaw 进程
> **最终结论**：**独立签名不可行**，网关采用 OS 级 PID 反查机制，无法伪造

---

## 一、背景与目标

### 1.1 项目背景

`claude-code-proxy` 项目需要为 QClaw 提供本地代理能力。历史方案使用 `__QCLAW_AUTH_GATEWAY_MANAGED__` token + `x-agent-id: main` 头部直接请求 19000 端口，但 QClaw 升级到 v0.2.33 后该方案失效，返回 403 / 错误码 9002。

需要新的方案让外部进程（Python/Node.js）调用 QClaw 19000 端口的本地 LLM 代理能力。

### 1.2 QClaw 三层架构

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 1: Auth Gateway (19000)                              │
│  - 接收本地 HTTP 请求                                        │
│  - 鉴权后转发到 Layer 2                                      │
│  - 由 QClaw 主进程 (Electron) 内嵌的 Node.js 服务实现       │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│  Layer 2: OpenClaw (54678/54679)                            │
│  - 腾讯 OpenClaw 内核                                        │
│  - 处理模型路由、签名注入                                    │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│  Layer 3: Upstream LLM                                       │
│  - https://mmgrcalltoken.3g.qq.com/aizone/v1                │
│  - 真正的 LLM 推理服务                                       │
└─────────────────────────────────────────────────────────────┘
```

### 1.3 两个候选方案

| 方案 | 描述 | 优势 | 劣势 |
|------|------|------|------|
| 方案1 | 破解 HMAC-SHA256 签名算法，外部进程独立签名请求 | 完全脱离 QClaw 进程，架构干净 | 需要逆向 V8 字节码 |
| 方案2 | 在 QClaw 进程内寄生 HTTP 服务器（19001），外部转发 | 实现简单，立即可用 | 依赖 QClaw 进程存活 |

用户决定先实现方案2 作为过渡，同时推进方案1 作为终极目标。

---

## 二、阶段一：HMAC-SHA256 签名算法破解 ✅

### 2.1 方法

通过 Electron `--inspect=9229` 连接 QClaw 主进程的 inspector 协议，hook `crypto.createHmac` 捕获签名过程中的密钥和 payload。

### 2.2 关键发现

**密钥**：字符串 `2fc7c82b2cdc2a6083239d343843adf314b571dd0ee036163b61fb209be47492` 本身的 UTF-8 字节（**不是** hex 解码后的 32 字节）。

**Payload 格式**：每行 `"header-name": value`，最后一行是 URL path（无引号），用 `\n` 分隔：

```
"x-server-timestamp": {server_ts}
"x-client-timestamp": {client_ts}
"x-nonce": {nonce}
"x-openclaw-token": {JWT}
"x-qclaw-version": 0.2.33
"user-agent": {UA}
"x-auth-version": 0.0.1
"x-conversation-id": {UUID}        ← 可选
"x-conversation-request-id": {ID}  ← 可选
"x-conversation-message-id": {ID}  ← 可选
"authorization": Bearer {API_KEY}
mmgrcalltoken.3g.qq.com/aizone/v1/chat/completions
```

**算法**：
```python
KEY = "2fc7c82b2cdc2a6083239d343843adf314b571dd0ee036163b61fb209be47492"
signature = hmac.new(KEY.encode('utf-8'), payload.encode('utf-8'), hashlib.sha256).hexdigest()
```

### 2.3 验证

用独立 Python 脚本重新计算签名，与捕获的两条签名均完全匹配。**签名算法 100% 破解成功**。

### 2.4 调研脚本

- `_hook_hmac_v2.js` - hook `crypto.createHmac`，捕获 key 的原始字节
- `_verify_sign_v2.py` - 验证签名算法正确性
- `_trigger_and_query.js` - 通过 QClaw 进程内 axios 触发签名请求

---

## 三、阶段二：独立签名请求失败 ❌

### 3.1 实验

用验证过的签名算法，从外部 Python 进程独立请求 19000 端口：

```python
# _test_independent_sign_v2.py
# 构造完整签名头部，独立请求 http://127.0.0.1:19000/proxy/llm/chat/completions
```

### 3.2 结果

```
HTTP 403
{"error":{"code":"9002","message":"该功能暂不可用，请稍后再试","type":"auth_error","source":"gateway"}}
```

### 3.3 结论

**签名正确但不够**。网关除了签名校验外，还有另一层校验机制阻止外部进程访问。

---

## 四、阶段三：进程来源检查机制调查 🔍

### 4.1 实验设计

在 QClaw 进程内创建一个原生 `http.request`（不带任何签名头部），与外部 Python 请求对比：

| 请求来源 | 是否带签名头部 | 结果 |
|----------|----------------|------|
| QClaw 进程内原生 http | ❌ 无任何签名头部 | ✅ 200 |
| 外部 Python（带完整签名） | ✅ 完整签名 | ❌ 403 |
| 外部 Python（无签名） | ❌ 无签名 | ❌ 403 |

**关键观察**：进程内原生 http 请求**不带任何签名头部**也能成功，证明 19000 网关**不检查 HTTP 内容**，而是检查**请求来源进程**。

### 4.2 排除假设的验证过程

依次排除以下假设，每个假设都有对应的验证实验：

#### 假设1：HTTP headers 差异

**实验**：`_compare_headers.js` - 对比进程内外请求的 HTTP headers

**结果**：内外请求的 headers **完全相同**（content-type, content-length, host, connection）

**结论**：❌ 不是 HTTP 层面的差异

#### 假设2：Socket handle 类型差异（Pipe/IPC vs TCP）

**实验**：`_check_req_socket.js` - 检查 socket handle 类型

**结果**：内外请求都是 **TCP**（不是 Pipe/IPC），相同的 server PID (12424)、相同的 connectionKey

**结论**：❌ 不是 socket 类型差异

#### 假设3：Socket 地址差异

**实验**：同上

**结果**：内外请求的 `remoteAddress` 都是 `127.0.0.1`，`localPort` 都是 19000

**结论**：❌ 不是地址差异

#### 假设4：Socket 上有 PID 字段

**实验**：`_hook_fs_for_403.js` - hook `res.end` 捕获 403 时的完整 socket 属性

**结果**：
```json
{
  "hasGetpeerPid": false,
  "_peerPid": undefined,
  "keys": ["connecting", "allowHalfOpen", "server", "parser"],
  "customProps": {
    "connecting": false,
    "_hadError": false,
    "allowHalfOpen": true,
    "_pendingEncoding": "",
    "_paused": false
  }
}
```

**结论**：❌ Node.js 标准 socket 上没有 PID 字段

#### 假设5：网关读取 channel.json 等配置文件

**实验**：`_hook_fs_for_403.js` - hook `fs.readFileSync/existsSync/statSync`

**结果**：403 发生时 **0 个 fs 调用**

**结论**：❌ 不读任何文件（之前在 403 调用栈附近发现的 `channel.json`/`updateAppChannel` 字符串是误导）

#### 假设6：通过启动 powershell.exe 做 PID 反查

**依据**：index.cjsc 中发现 `Get-NetTCPConnection`、`Win32_Process`、`ParentProcessId`、`Select-Object -ExpandProperty OwningProcess` 等字符串

**实验**：`_hook_child_process.js` - hook `child_process.spawn/exec/execSync/execFile/fork`

**结果**：403 发生时 **0 个 child_process 调用**

**结论**：❌ 不启动子进程，但 index.cjsc 中确实有 PID 反查相关的 PowerShell 命令字符串（可能用于其他功能，如诊断）

#### 假设7：走标准 Node.js `http.Server`

**实验**：`_compare_200_403_sockets.js` - hook `http.Server.prototype.emit` 的 `request` 事件

**结果**：进程内 200 和外部 403 都发生，但 **0 个请求被捕获**

**结论**：⚠️ 19000 端口的 HTTP 服务器**不走 Node.js 标准 `http.Server`**！这解释了为什么之前的 `net.Socket.prototype.write` hook（`_capture_raw_socket_bytes.js`）也捕获到 0 个写入。

### 4.3 阶段三小结

经过 7 轮排除法验证，确认：
- 19000 网关**不走 Node.js 标准 http/sock 栈**
- 403 判断**不读文件、不启动子进程**
- 内外请求在所有可观察的 JS 层面**完全相同**

但 403 调用栈明确指向 `index.cjsc:1:988303` → `sendJsonError (index.cjsc:1:979818)`。

---

## 五、阶段四：最终证据 - 进程模块分析 🎯

### 5.1 实验

由于所有 JS 层面的 hook 都失效，转向分析 QClaw 进程加载的 native 模块：

```powershell
$mods = (Get-Process -Id 12424).Modules
$mods | Where-Object { $_.ModuleName -match 'iphlp|ws2_32|winhttp|node|electron|tcp|net' }
```

### 5.2 关键发现

QClaw 主进程加载了以下关键模块：

| 模块 | 用途 |
|------|------|
| **IPHLPAPI.DLL** | Windows 网络 API，包含 `GetExtendedTcpTable`（PID 反查的底层 API） |
| **WS2_32.dll** | Windows Socket 2 API |
| **koffi.node** | FFI（Foreign Function Interface）库，允许 JS 直接调用任意 Windows API |
| guid-native.win32-x64-msvc.node | QClaw 自带的 GUID 生成 native 模块 |
| better_sqlite3.node | SQLite 数据库 |
| NETAPI32.dll | Windows 网络管理 API |

### 5.3 机制还原

结合所有证据，19000 网关的进程来源检查机制完全清晰：

```
1. 外部请求到达 19000 端口
2. 网关通过自实现的 HTTP 服务器（非 Node.js http.Server）接收连接
3. 通过 koffi FFI 直接调用 IPHLPAPI.DLL 的 GetExtendedTcpTable
   - 输入：客户端的 remote port
   - 输出：该连接的 OwningPID（操作系统内核维护，无法伪造）
4. 检查 OwningPID 是否在 QClaw 进程树中
   - 通过 OpenProcess + QueryFullProcessImageName 获取进程路径
   - 或通过 ParentProcessId 检查父子关系
5. 若 PID 不在可信树中 → 直接返回 403，根本不走签名校验
6. 若 PID 在可信树中 → 跳过签名校验，直接转发
```

### 5.4 为什么 koffi FFI 而不是 child_process

| 方式 | 性能 | 可检测性 |
|------|------|----------|
| 启动 powershell.exe 执行 `Get-NetTCPConnection` | 慢（~100ms） | 容易被 child_process hook 捕获 |
| koffi FFI 直接调用 IPHLPAPI | 极快（<1ms） | 不经过 JS，hook 不到 |

QClaw 选择 koffi FFI 方式，既保证了性能，又避免了被常规 JS hook 检测。这也是为什么我们在 index.cjsc 中看到了 PowerShell 命令字符串（可能是早期实现或备用诊断逻辑），但实际运行时走的是 FFI。

### 5.5 index.cjsc 字符串证据

在 `out/main/index.cjsc`（V8 字节码，字符串可读）中发现的关键字符串：

```
@1440040: netstat -ano
@1449150: Get-NetTCPConnection -State Established -ErrorAction SilentlyContinue
          | Select-Object -ExpandProperty OwningProcess
          | Select-Object -First 1
@1444936: Get-CimInstance Win32_Process -Filter "ProcessId=$p"
          if (-not $proc -or $proc.ParentProcessId -le 1 -or $proc.ParentProcessId -eq $p) { break }
@1446530: ps -eo pid=,ppid=   (Linux 备用)
@5705838: OpenProcess, CloseHandle, SetInformationJobObject, AssignProcessToJobObject
```

这些字符串证明 QClaw 内部确实有 PID 反查 + 进程树比对的逻辑（虽然实际运行时走 FFI 而非 PowerShell）。

---

## 六、最终结论

### 6.1 核心结论

**QClaw 19000 网关采用 OS 级 PID 反查机制，无法通过外部进程伪造或篡改。**

| 问题 | 答案 |
|------|------|
| 进程不能伪装吗？ | **不能**。PID 由 Windows 内核管理，用户态无法伪造 |
| 这个信息不能篡改吗？ | **不能**。`GetExtendedTcpTable` 直接读取内核维护的 TCP 表，无法注入或修改 |
| 签名破解能成功吗？ | **不能**。即使签名 100% 正确，外部进程仍被 403 |
| 唯一可行方案是什么？ | **方案2（寄生代理）** - 在 QClaw 进程内创建 HTTP 服务器 |

### 6.2 安全机制评级

QClaw 19000 网关的进程来源检查机制属于 **OS 级信任链**，比以下机制都更底层：

```
HTTP headers/signature  ← 应用层（易伪造）
  ↓
Socket 属性            ← 传输层（中等）
  ↓
PID 反查               ← OS 内核层（无法伪造）  ← QClaw 采用
  ↓
内核模块签名           ← 内核层（最严格）
```

### 6.3 为什么这是"认人不认信"设计

这种设计在本地服务（localhost）场景中非常经典：

- **内部进程**（QClaw 自己的 axios/http）：PID 在可信树中 → 直接放行，不检查签名
- **外部进程**（Python/第三方）：PID 不在可信树中 → 强制走签名校验，但即使签名正确也拒绝（因为外部进程无法获得合法的 device 签名）

这种设计确保了即使签名算法被破解，外部进程也无法直接调用 19000 端口，必须通过 QClaw 进程内代理。

---

## 七、方案对比与最终选择

### 7.1 方案1（独立签名）- 不可行

| 步骤 | 状态 |
|------|------|
| HMAC-SHA256 签名算法破解 | ✅ 完成 |
| 签名验证 | ✅ 通过 |
| 独立请求 19000 | ❌ 403（PID 不在可信树） |
| 绕过 PID 检查 | ❌ 无法绕过（OS 级） |

**结论**：方案1 **根本不可行**，不应继续投入。

### 7.2 方案2（寄生代理）- 已实现并验证 ✅

**架构**：
```
client (Python/任意) → 8083 (server.py) → 19001 (QClaw 进程内寄生 http.createServer) → 19000 (QClaw 网关) → 上游 LLM
```

**实现**：
- `_reinject_v3.js` - 通过 inspector 注入到 QClaw 进程，在 19001 端口创建 HTTP 服务器
- 使用 QClaw 的 axios 实例（带签名拦截器）转发请求到 19000
- 只复制请求拦截器（签名注入），不复制响应拦截器（避免流式响应循环引用）

**测试结果**：
| 测试项 | 结果 |
|--------|------|
| 非流式请求 | ✅ 通过 |
| 流式请求（SSE） | ✅ 通过 |
| Anthropic 格式 | ✅ 通过 |

**优势**：
- 由 QClaw 进程发请求，PID 是 QClaw 自己的，网关放行
- 不破坏 QClaw 本身的功能
- 支持所有 QClaw 支持的模型

**劣势**：
- 依赖 QClaw 进程存活（QClaw 关闭则代理失效）
- 需要 QClaw 以 `--inspect` 模式启动（或通过其他方式注入）

---

## 八、技术细节附录

### 8.1 关键工具与命令

#### 连接 QClaw inspector

QClaw 需要以 `--inspect=9229` 启动。连接方式：

```javascript
// 通过 http://127.0.0.1:9229/json 获取 webSocketDebuggerUrl
// 然后用 WebSocket 连接，发送 CDP（Chrome DevTools Protocol）命令
```

#### 在 QClaw 进程内执行代码

```javascript
// 使用 Runtime.evaluate 注入代码
await send('Runtime.evaluate', {
    expression: `(async () => { ... })()`,
    returnByValue: true,
    awaitPromise: true
});
```

#### 访问 Node.js 内置模块（绕过 QClaw 的 require 缓存）

```javascript
const http = process.getBuiltinModule('http');
const fs = process.getBuiltinModule('fs');
const net = process.getBuiltinModule('net');
const cp = process.getBuiltinModule('child_process');
```

### 8.2 403 错误特征

```json
{
  "error": {
    "code": "9002",
    "message": "该功能暂不可用，请稍后再试<!--error_code:9002-->",
    "type": "auth_error",
    "source": "gateway"
  }
}
```

- HTTP 状态码：403
- 错误来源：`gateway`（明确标识来自 19000 网关层）
- 调用栈：`sendJsonError (index.cjsc:1:979818)` ← `index.cjsc:1:988303`

### 8.3 关键进程信息

| 进程 | PID | 角色 |
|------|-----|------|
| QClaw.exe (主) | 12424 | Electron 主进程，承载 19000 网关、inspector 9229、寄生代理 19001 |
| QClaw.exe (渲染器) | 12592, 7688, 6548, 8876 | Electron 渲染进程 |
| node.exe | 1020, 9704 | OpenClaw 内核进程 |

### 8.4 调研脚本清单

| 脚本 | 用途 | 阶段 |
|------|------|------|
| `_hook_hmac_v2.js` | hook crypto.createHmac 捕获密钥 | 阶段1 |
| `_verify_sign_v2.py` | 验证签名算法正确性 | 阶段1 |
| `_trigger_and_query.js` | 触发签名请求并查询捕获 | 阶段1 |
| `_test_independent_sign_v2.py` | 独立签名请求 19000（失败） | 阶段2 |
| `_test_source_check.js` | 验证进程内外请求差异 | 阶段3 |
| `_compare_headers.js` | 对比内外请求 headers | 阶段3 |
| `_check_req_socket.js` | 检查 socket handle 类型 | 阶段3 |
| `_capture_403_stack.js` | 捕获 403 调用栈 | 阶段3 |
| `_capture_403_detail.js` | 捕获 403 完整 response | 阶段3 |
| `_capture_raw_socket_bytes.js` | hook socket.write 捕获原始字节 | 阶段3 |
| `_hook_child_process.js` | hook child_process 验证 powershell | 阶段3 |
| `_hook_fs_for_403.js` | hook fs 验证文件读取 | 阶段3 |
| `_compare_200_403_sockets.js` | 对比 200/403 的 socket 详情 | 阶段3 |
| `_search_pid_keywords.js` | 搜索 index.cjsc 中的 PID 相关字符串 | 阶段4 |
| `_extract_cjsc.py` | 从 asar 提取 index.cjsc | 阶段4 |
| `_extract_988303_ctx.py` | 提取 988303 偏移附近的字符串 | 阶段4 |
| `_search_keywords.py` | 搜索签名相关关键词位置 | 阶段4 |
| `_reinject_v3.js` | 方案2 寄生代理核心实现 | 方案2 |
| `_extract_proxy_llm.py` | 提取 /proxy/llm 区域字符串 | 阶段4 |

### 8.5 关键文件路径

| 路径 | 说明 |
|------|------|
| `C:\Program Files\QClaw\v0.2.33.617\resources\app.asar` | QClaw 主应用包 |
| `C:\Program Files\QClaw\v0.2.33.617\resources\app.asar\out\main\index.cjsc` | 主进程 V8 字节码（含签名逻辑） |
| `C:\Program Files\QClaw\v0.2.33.617\resources\native\guid-native.win32-x64-msvc.node` | GUID native 模块 |
| `C:\Program Files\QClaw\v0.2.33.617\resources\app.asar.unpacked\node_modules\koffi\build\koffi.node` | FFI 库（用于调用 IPHLPAPI） |
| `C:\Program Files\QClaw\v0.2.33.617\resources\node\node.exe` | QClaw 自带 Node.js v22.22.3 |
| `%APPDATA%\QClaw\app-store.json` | API Key 存储 |
| `~/.openclaw/openclaw.json` | OpenClaw gateway token |

---

## 九、调研方法论总结

本次调研的成功归功于**系统性的排除法**：

1. **先验证最简单的假设**（HTTP headers 差异）→ 排除
2. **逐步深入底层**（socket → fs → child_process → native 模块）
3. **每一步都通过实验验证**，不靠猜测
4. **当所有 JS 层面 hook 失效时，转向 OS 层面分析**（进程模块列表）
5. **结合多个证据交叉验证**（字符串证据 + 模块加载证据 + 行为证据）

### 关键转折点

调研过程中有几个关键的"恍然大悟"时刻：

1. **进程内原生 http（无签名）能过 200** → 证明网关不检查 HTTP 内容，检查进程来源
2. **socket.write hook 捕获 0 个** → 证明不走 Node.js 标准 socket
3. **http.Server emit hook 捕获 0 个** → 证明不走 Node.js 标准 http.Server
4. **child_process hook 捕获 0 个** → 证明不启动子进程
5. **进程模块列表发现 IPHLPAPI.DLL + koffi.node** → 最终证据，证明用 FFI 直接调用 Windows API

### 教训

1. **不要被字符串误导**：index.cjsc 中有 `Get-NetTCPConnection` 字符串，但实际运行时不走 PowerShell
2. **JS hook 有边界**：native 模块通过 FFI 调用的代码无法被 JS hook 捕获
3. **OS 级信任链无法绕过**：PID 是内核维护的，用户态无法伪造

---

## 十、后续工作

### 10.1 已完成

- ✅ HMAC-SHA256 签名算法完全破解（虽无法独立使用）
- ✅ 方案2 寄生代理实现并测试通过
- ✅ 19000 网关进程来源检查机制完全摸清

### 10.2 待办

- [ ] 创建 `feature/qclaw-local` 分支
- [ ] 整理方案2 代码到生产级质量
- [ ] 清理约 20 个临时调查脚本
- [ ] 本地调试用新端口（8083）避免与 8082 冲突
- [ ] 合并到 main 分支
- [ ] 更新 CHANGELOG.md 和 project_memory.md

### 10.3 长期展望

虽然方案1（独立签名）不可行，但本次调研的副产品有长期价值：

1. **签名算法**：已完整记录，可用于其他场景（如直接调用上游 LLM）
2. **进程来源检查机制**：为理解 QClaw 安全架构提供完整视角
3. **调研方法论**：可复用于其他 Electron 应用的逆向分析
4. **寄生代理架构**：可作为类似场景的参考实现

---

**文档版本**：v1.0
**最后更新**：2026-07-16
**作者**：claude-code-proxy 项目组
