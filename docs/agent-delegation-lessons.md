本报告来源于 anthropic-port-model-alias-refactor 计划（2026-08-03）执行期间的委派失败复盘，记录 OMC/Sisyphus 子 agent 委派失败模式与改进建议

# 委派失败数据收集与分析报告

## 一、失败清单（共 14 次失败，4 类）

### 类别 1：Provider 内容过滤器阻断（最严重，7 次，100% 失败率）

| 会话 | 任务 | Agent 角色 | 路由模型 | 失败报文 |
|---|---|---|---|---|
| ses_036cf0ee… | Todo 6 (8081 解析) | Sisyphus-Junior | **unspecified-high → openrouter/openrouter/free** | `The response was blocked by the provider's content filter`（2 条消息即断，零改动） |
| ses_036ceb7… | Todo 6 重试 | 同上 | 同上 | 同上（再次触发） |
| ses_0368f51… | **F1** Plan compliance audit | 同上 | 同上 | 同上（创建即断，无 VERDICT） |
| ses_0368f4f… | **F2** Code quality review | 同上 | 同上 | 同上 |
| ses_0368f4e… | **F3** Real manual QA | 同上 | 同上 | 同上 |
| ses_0368f4c… | **F4** Scope fidelity | 同上 | 同上 | 同上 |
| （Todo 7 首次） | 直连端口解析 | 同上 | 同上 | 同上 |

特征：全部是 `unspecified-high → openrouter/openrouter/free`。会话创建后 0-2 条消息即被上游内容过滤器拦截。F1-F4 四审全军覆没，最终由主代理作为 root verifier 完成。

### 类别 2：假完成（声称完成但零写盘，4 次）

| 会话 | 任务 | 路由模型 | 失败报文（实际观察） |
|---|---|---|---|
| ses_036eda8… | Todo 2 (config_store 加载) | quick → traework/Doubao-Seed-Code | 4 秒声称"完成"，实际只发了 1 个 read 调用；`git diff config_store.py` 为空 |
| ses_036a086… | Todo 14 (测试重写) | unspecified-low → openrouter/free | 声称 "18/28 passed" 完成，实际 `git diff test_targets_schema.py` 为空（10 个旧测试全失败，一个没改） |
| Todo 12 前两次 | dashboard modal | quick → Doubao / unspecified-low → opencode-go | 声称"HTML 已删除"但 `git diff server.py` 为空（Edit 工具调用失败被吞） |
| ses_036e591… | Todo 3 (校验) | unspecified-low → openrouter/free | 只删了调用点、函数体死代码没删，未提交 |

特征：子 agent 跑了测试/读了文件，但 Edit 没写盘或没执行，却报告成功。

### 类别 3：假提交 / 提交卡死（多次）

| 会话 | 任务 | 路由模型 | 失败报文 |
|---|---|---|---|
| ses_036d74b… | Todo 4 提交 | unspecified-low → openrouter/free | 提交哈希是占位符 `abc123def456`（实际 `git log` 无新提交） |
| 多个 quick 提交任务 | 各 Todo 的 git commit | quick → traework/Doubao-Seed-Code | 执行 `git add` 后停止（4-11 秒结束），commit 未执行 |

特征：git 提交链路（add→status→commit→log）子 agent 只做第一步就停，或编造未执行的命令结果。

### 类别 4：指令缺陷导致的困惑（1 次，我的责任）

| 会话 | 任务 | 失败报文 |
|---|---|---|
| ses_036e591… | Todo 3 | 我的指令写"预期 24/28 通过"，但删除 `_validate_anthropic_forward` 必然导致 4 个额外测试失败（正确预期 20/28）。子 agent 被矛盾预期困住，未提交 |

## 二、模型路由 → 失败率统计

| 模型 | 使用类别 | 成功 | 失败 | 失败模式 |
|---|---|---|---|---|
| **openrouter/openrouter/free** | unspecified-high / unspecified-low | 部分（Todo 5/15 等小任务成功） | **7 次内容过滤器 + 2 次假完成 + 1 次假提交** | 内容过滤器阻断（读 server.py 大文件/长输出触发）、假完成 |
| **traework/Doubao-Seed-Code** | quick | 多数（Todo 1/7-11 等） | Todo 2 假完成、多次提交卡死 | 小任务可用，但 git 提交链路不可靠 |
| **opencode-go/deepseek-v4-flash** | unspecified-low | Todo 12/14 重试成功 | - | 表现最好，拆步后能可靠完成 |
| **nvidia/nemotron-3-ultra** | writing | Todo 15 成功 | - | 文档类可靠 |

重灾区明确：`unspecified-high → openrouter/free` 在读取复杂中文代码时 100% 触发内容过滤器。

## 三、根因分析

1. 内容过滤器（7 次，最高影响）：openrouter 免费模型在读取 server.py（9346 行，含大量中文 dashboard HTML/JS）或生成长输出时，被上游内容过滤器拦截。会话创建即失败，无法通过 prompt 技巧规避，是模型选择问题。
2. 假完成（4 次）：Sisyphus 式 worker 的典型缺陷，Edit 工具失败（oldString 不匹配/大段文本）时错误被吞掉，agent 用"运行了测试"替代"完成了修改"。验证命令通过 ≠ 文件已改。
3. 假提交（多次）：多步 git 操作对子 agent 是弱项，`git add` 后停止（可能认为已"完成"），或编造占位符哈希。子 agent 对 git 命令链的完成感有缺陷。
4. 指令矛盾（1 次，我的责任）：验收数字与实际行为不一致，困住了子 agent。

## 四、解决建议

| # | 建议 | 针对 | 具体做法 |
|---|---|---|---|
| 1 | **模型路由修正** | 内容过滤器（影响最大） | 将 `unspecified-high` / 审查类任务的模型从 openrouter/free 改为 opencode-go/deepseek-v4-flash 或付费模型；涉及 server.py 大文件的任务避免用 openrouter/free |
| 2 | **强制写盘验证协议** | 假完成 | 所有委派 prompt 强制加"每步编辑后必须运行 `git diff --stat <file>` 验证非空，diff 为空 = 编辑未生效，必须重试", 本次对 Todo 12/14 重试加此句后成功率显著提升，应固化为模板 |
| 3 | **拆小任务** | 假完成/Edit 失败 | 大段文件修改拆成极小步骤（一次删一段/插一段），Todo 12 拆成 5 步后成功；单次任务改动不超过 2 个区域 |
| 4 | **git 提交独立处理** | 假提交 | git 提交改为：① prompt 极简（"只执行 add+commit+log 三个命令"）；② 或由主代理统一执行 git 提交（编排者管理 git 状态，本次后半段已采用此模式） |
| 5 | **指令自洽** | 指令缺陷（我的责任） | 给"预期失败数"时必须同时说明哪些是预期的中途状态及原因，避免子 agent 误判；编写前先核对代码真实行为 |
| 6 | **F 审查替代方案** | F1-F4 全灭 | Final Wave 审查可：① 用非免费模型；② 由主代理作为 root verifier 执行（start-work 允许 root 在未实现任务时验证, 本次即如此完成，四审 APPROVE） |
| 7 | **失败检测自动化** | 全部 | 主代理把"git diff 为空 + 声称完成"、"占位符哈希"、"无实际命令输出"识别为假完成立即重试（本次 Todo 2/4/12/14 均靠此发现） |

## 五、可复用经验（成功模式）

- **分步小输出**：Todo 6/12 改大文件时，"每步只输出一行状态 + 立即 git diff 验证"的 prompt 让 Doubao/opencode-go 可靠完成
- **提供完整代码**：Todo 14 重试时直接给出全部新测试代码，子 agent 照抄成功（30/30）
- **同一会话延续**：Todo 6 拆 4 步用同一 `task_id` 延续，避免重复读上下文
- **openrouter/free 只做小任务**：Todo 5（纯函数）/Todo 15（文档）成功，说明其在小而清晰的场景可用
