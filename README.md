# kimi-advisor

**Kimi Code 专属「第二意见（Advisor）+ 防卡死与完工守卫」插件**

`kimi-advisor` 将 [Anthropic 官方 Advisor Strategy](https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/advisor-tool)、[`rpiv-advisor`](https://pi.dev/packages/@juicesharp/rpiv-advisor)、[`pi-advisor-flow`](https://pi.dev/packages/pi-advisor-flow) 与 [`pi-warden`](https://pi.dev/packages/pi-warden) 四者的核心机制完整移植并适配到 **Kimi Code CLI / Web**：

- **不使用子代理**：通过 `MCP Server + Hooks + Skill + System Prompt` 原生协同，零子代理冷启动开销，不割裂主会话上下文。
- **按需介入、不乱调**：只在主模型**遇到难题卡住**或**准备宣布任务完成**时介入，日常读写和常规步骤零打扰。
- **Jev 300ms 前置判别**：每次触发守卫或调用 Advisor 前，先用 TypeSafe AI 的快速判别模型 **Jev**（System 1 Judge）做毫秒级筛选，低风险、可自行解决的调用自动跳过。

---

## 解决什么问题？

在日常使用 Coding Agent 时，经常遇到三类痛点：

1. **主模型钻牛角尖（Stuck Loop）**：遇到报错后不看根因，反复微调参数重试同一个失败方案，白白消耗几十轮 Token。
2. **未验证就宣布完成（Unverified Done）**：改了多个文件后没跑测试/语法检查，或者检查脚本里用 `;` 掩盖了退出码，就直接回复“已完成”。
3. **第二意见要么太贵、要么不触发**：如果全靠提示词让主模型自己调 Advisor，它往往一路闷头干到底从不调用；如果每轮都强制调大模型审查，又会导致极高的延迟和 Token 开销。

`kimi-advisor` 用一套**确定性证据收集 + Jev 概率判别 + 高阶 Advisor 复核**的三层流水线解决上述问题。

---

## 核心功能与架构实现

```text
主模型 (Executor)
  │
  ├─► [工具调用失败 PostToolUseFailure]
  │      └─► pi-warden Stuck 守卫：AttemptWindow(12) + 离线 quickRepeat + Jev(same_strategy >= 0.7)
  │             └─► 判定卡死 → 下一次 PreToolUse(Bash/Edit/Write) 拦截 1 次，引导调用 ask_advisor({})
  │
  ├─► [主动或被引导调用 ask_advisor({})]
  │      ├─► 1. PreToolUse 钩子绑定 {session_id, tool_call_id, owner_pid}
  │      ├─► 2. 同模型检查（Executor == Advisor 时自动跳过）
  │      ├─► 3. Jev 预筛（~300ms，评估 stakes 与 self_answerable，低风险自动跳过；同轮二次调用可强制覆盖）
  │      └─► 4. 从 wire.jsonl 重建有界上下文（默认 60,000 字符）→ 调用 Advisor 模型返回指导
  │
  └─► [回合结束 Stop]
         └─► pi-warden Done-Check 守卫：
                ├─► 本轮无 Edit/Write 修改或子代理运行中 → 0 开销直接放行
                ├─► Jev doneQuestions 判定是否在宣布完成（claims_done >= 0.7）
                ├─► 修改后无通过的测试/检查 → 拦截并自动探测项目测试命令（pytest/npm/make/cargo/go）提示补跑
                └─► 开启 completion_gate 时 → Jev 评估改动风险，必要时要求先调 ask_advisor({}) 复核后再收尾
```

### 1. 有界全上下文 Advisor 咨询（`advisor_core.py` + `server.py`）

- **零手抄上下文**：主模型调用 `ask_advisor({})` 时无需手写摘要，MCP 服务端直接从当前会话的 `wire.jsonl` 重建真实对话分支（自动处理 `/undo` 回退记录、合并 Compaction 历史摘要、过滤系统注入噪音）。
- **智能窗口预算（默认 `60,000` 字符 ≈ 2 万 tokens）**：
  - 强制锚定保留**「第一条用户原始需求 + 会话压缩摘要 + 最新一条用户指令」**，保证全局目标不丢失；
  - 剩余空间按**从新到旧**装入最近的 15~25 步工具调用与结果，最终按时间顺序拼装；
  - **单条大输出头尾截断（`ADVISOR_CAPS`）**：单条 `tool_result` 上限 `8,000` 字符（保留前 40% 与后 60%），防止一次大日志把前文代码修改挤出窗口。
- **超大会话 4MB 尾部快读**：当 `wire.jsonl` 超过 4MB（如长会话达 200MB+）时，钩子走 `parse_wire_current_turn()` 仅倒序切片解析尾部 4MB，解析耗时从 **5.2 秒降至 ~80ms**；遇到 `/undo` 等跨切片边界情况自动安全回退全量解析。
- **闭环回复协议**：Advisor 只返回四类结果之一：`Verdict: sound`（方案/交付无误，一句话确认放行）、`plan`（下一步具体行动）、`correction`（指出错误路径并纠偏）、`stop`（停下并交由用户决策），严禁向主模型反问或挑刺无关痛痒的细节。
- **敏感信息自动脱敏**：内置 12 条脱敏正则，自动擦除 PEM 私钥、Bearer Token、OpenAI/Anthropic/GitHub/Google/AWS/Slack API Key、JWT 及 URL/配置中的密码。

### 2. Jev 毫秒级预筛与短熔断（`pi-advisor-flow` 移植）

- 调用 Advisor 前，通过 `jev-use judge` 同时评估两个维度：
  - `stakes`（决策风险：Negligible / Moderate / High）
  - `self_answerable`（主模型能否凭现有工具和上下文独立解决）
- 默认采用 `pi` 预设：仅当 `P(negligible) >= 0.85` 且 `P(self_answerable) >= 0.85` 时才跳过，既拦住琐碎调用，又不漏掉关键改动。
- **防雪崩短熔断（Judge Cooldown）**：若 Jev 连续 2 次超时或网络不可达，自动进入 60 秒熔断冷却期（fail-open），绝不阻塞 Kimi 正常工具执行。

### 3. Stuck 防卡死守卫（`pi-warden` 移植：`warden_guards.py` + `hooks/on_tool_failure.py`）

- **滑动窗口证据（`AttemptWindow = 12`）**：记录每次工具调用的参数指纹、归一化输出指纹、以及 `Edit`/`Write` 的行级 diff。
- **离线快速重复拦截（`quickRepeat`）**：同一命令以相同报错连续失败且中间没有任何状态修改时，不调模型直接拦截提醒。
- **Jev 同策略判定（`same_strategy >= 0.7`）**：当本轮失败次数 `>= 3` 且达到冷却步数（`cooldown = 3`，使用未截断真实累计步数，并在调用 `ask_advisor` 后自动重置冷却锚点）时，由 Jev 评估是否在用同一种无效思路反复试错。一旦确认为卡死，通过下一次 `PreToolUse` 钩子注入一次性引导（one-shot `pending_steer`），让主模型停手并调用 `ask_advisor({})`。

### 4. Done-Check 完工守卫与 Completion Gate（`pi-warden` 移植：`warden_guards.py` + `hooks/turn_gate.py`）

- **子代理隔离**：自动识别 `agent_id != "main"` 以及主代理当前轮未返回结果的 `Agent` / `AgentSwarm` / `TowerSpawn` 调用，子代理结束触发 `Stop` 时直接以 `subagent_active` 放行。
- **新鲜检查验证（`freshChecks`）**：
  - 严格追踪本轮最后一次 `Edit`/`Write` 之后是否运行并通过了测试、构建或语法检查；
  - 识别多行脚本：若检查命令后接了 `; rm ...` 等无 `set -e` 保护的后续命令（可能掩盖非零退出码），自动判定为无效检查；支持识别 `set -e` / `set -euo pipefail` 及 `&&` 链式检查。
- **自动发现项目测试命令（`project_check_command`）**：拦截未验证的完工声明时，自动检测工作区下的 `package.json`（自动区分 `pnpm`/`yarn`/`bun`/`npm` 的 `check`/`test` 脚本）、`Makefile`、`pyproject.toml`/`pytest.ini`、`Cargo.toml`、`go.mod`，在提示中直接给出具体命令（如 ``Run `pytest` on what you changed.``）。
- **先问 Jev，需要才调 Advisor**：无论是未验证拦截还是开启了 `completion_gate` 的完工门禁，都先由 Jev 判断该改动是否需要第二意见；琐碎修改（如改 typo）不会要求调用 Advisor，且每轮用户指令最多拦截一次（`done_checked_turns`），绝不死循环。

---

## 目录结构

```text
kimi-advisor/
├── kimi.plugin.json          # Kimi 插件清单（注册 MCP、3 个 Hooks、Skill、Commands、System Prompt）
├── SYSTEM.md                 # 注入主模型的 Advisor 调用时机与优先级规则（AGENTS.md 始终优先）
├── server.py                 # MCP Server（暴露 ask_advisor 与 advisor_config 两个工具）
├── advisor_core.py           # 核心实现：配置管理、wire 解析、上下文裁剪、脱敏、Jev 预筛、模型调用、日志统计
├── warden_guards.py          # pi-warden 移植：证据收集、freshChecks、doneQuestions、AttemptWindow、stuckQuestions
├── advisor_cli.py            # 零模型开销的命令行配置与状态查看工具
├── hooks/
│   ├── bind_call.py          # PreToolUse 钩子：会话精确绑定 + 消费一次性 stuck steer 拦截
│   ├── on_tool_failure.py    # PostToolUseFailure 钩子：AttemptWindow 收集与 Stuck 判定
│   └── turn_gate.py          # Stop 钩子：Done-Check 未验证拦截 + Completion Gate
├── commands/
│   ├── config.md             # /kimi-advisor:config 斜杠命令（交互式面板或直接传参修改配置）
│   └── review.md             # /kimi-advisor:review 斜杠命令（强制立即调用 Advisor 审查）
└── skills/
    └── advisor/
        └── SKILL.md          # 会话启动自动加载的 Advisor 技能说明
```

---

## 安装与依赖

### 1. 环境要求

- **Python 3.11+**（仅使用标准库，无第三方 pip 依赖）
- `~/.kimi-code/config.toml` 中已配置至少一个可用的模型别名（支持 `anthropic`、`openai`、`openai_responses` 三种 provider 类型）
- **可选（强烈推荐）**：安装 `jev-use` CLI 并在环境变量或 `~/.kimi-code/mcp.json` 中配置 `TYPESAFE_API_KEY`，以启用 Jev 预筛和 Jev 判别的 Stuck / Done-Check 守卫（若未安装 `jev-use`，插件会自动 fail-open 降级为直接调用 Advisor 与离线规则守卫）。

### 2. 安装插件

将仓库克隆到 Kimi Code 的插件目录：

```bash
git clone https://github.com/kim1232aa/kimi-advisor.git ~/.kimi-code/plugins/managed/kimi-advisor
```

然后在 Kimi Code 会话中执行：

```text
/plugins reload
```

（或在 Web 端执行 `/reload`）。

---

## 配置与使用

配置文件保存在 `~/.kimi-code/advisor/config.json`，运行日志保存在 `~/.kimi-code/advisor/log.jsonl`，钩子追踪保存在 `~/.kimi-code/advisor/hooks.jsonl`。

### 方式一：在 Kimi Code 内使用斜杠命令

- 查看或交互式修改设置（在允许提问的模式下会弹出内置选择框；在 Auto / Never Ask 模式下会展示当前配置表）：
  ```text
  /kimi-advisor:config
  ```
- 直接带参数修改设置：
  ```text
  /kimi-advisor:config model 9router/gpt-6.1-sol effort high
  ```
- 随时强制让 Advisor 对当前进展做一次复核：
  ```text
  /kimi-advisor:review 检查一下刚才的并发锁逻辑有没有死锁风险
  ```

### 方式二：在终端使用 CLI（不消耗任何模型 Token）

```bash
# 查看当前全部配置、模型及调用统计（咨询次数、Jev 跳过次数、守卫触发次数、平均延迟与 Token）
python3 ~/.kimi-code/plugins/managed/kimi-advisor/advisor_cli.py show

# 列出 config.toml 中的精选模型池（[secondary_model.models]）
python3 ~/.kimi-code/plugins/managed/kimi-advisor/advisor_cli.py pool

# 列出 config.toml 中所有可用的模型别名及支持的思考档位
python3 ~/.kimi-code/plugins/managed/kimi-advisor/advisor_cli.py models

# 修改任意配置项
python3 ~/.kimi-code/plugins/managed/kimi-advisor/advisor_cli.py set model 9router/gpt-6.1-sol
python3 ~/.kimi-code/plugins/managed/kimi-advisor/advisor_cli.py set effort high
python3 ~/.kimi-code/plugins/managed/kimi-advisor/advisor_cli.py set context 60000
python3 ~/.kimi-code/plugins/managed/kimi-advisor/advisor_cli.py set max-output 4096
python3 ~/.kimi-code/plugins/managed/kimi-advisor/advisor_cli.py set completion-gate on
```

### 全部 13 项配置说明

| MCP 参数名 | CLI `set` 键名 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `model` | `model` | `9router/cc/claude-opus-5-5` | Advisor 模型别名（支持单个别名或逗号分隔多个并行模型，如 `9router/gpt-6.1-sol,9router/gcli/grok-4.7`） |
| `effort` | `effort` | `off` | Advisor 推理思考档位：`off` / `low` / `medium` / `high` / `xhigh` / `max` |
| `jev_enabled` | `jev` | `on` | 是否开启 Jev 300ms 前置预筛 |
| `jev_preset` | `jev-preset` | `pi` | Jev 预筛松紧度：`strict`（0.95/0.45，最少跳过）、`pi`（0.85/0.35，原版默认）、`loose`（0.70/0.20，最省成本） |
| `stuck_enabled` | `stuck` | `on` | 是否开启 `pi-warden` Stuck 卡死检测守卫 |
| `done_enabled` | `done` | `on` | 是否开启 `pi-warden` Done-Check 完工未验证拦截守卫 |
| `completion_gate` | `completion-gate` | `off` | 开启后，改过文件且测试通过的完工回合也会先问 Jev 是否需要调 `ask_advisor` 复核 |
| `disable_same_model` | `same-model-skip` | `on` | 当主执行模型与 Advisor 解析到同一个底层模型时自动跳过咨询 |
| `context_max_chars` | `context` | `60000` | 单次发给 Advisor 的对话字符上限（`0` 或 `full` 表示按模型最大窗口全量发送） |
| `max_output_tokens` | `max-output` | `4096` | Advisor 单次回复的最大输出 Token 数（最小 `256`） |
| `max_calls_per_session` | `budget` | `unlimited` | 单个会话最多允许实际调用 Advisor 的次数（`0` 或 `unlimited` 为不限） |
| `redact_secrets` | `redact` | `on` | 发送给 Jev 和 Advisor 前自动擦除密钥、Token、私钥等敏感字符串 |
| `timeout_s` | `timeout` | `300` | 调用 Advisor 模型的 HTTP 超时时间（秒，最小 `10`） |

---

## 致谢与参考项目

- [Anthropic — The Advisor Strategy (`advisor_20260301`)](https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/advisor-tool)
- [`pi-advisor-flow`](https://github.com/philipbrembeck/pi-advisor) by Philip Brembeck（Jev 预筛分类器、截断策略与闭环提示词协议）
- [`pi-warden`](https://github.com/DevMortimer/pi-warden) by Ryan Gapac（`AttemptWindow` 卡死检测、`freshChecks` 完工验证与项目检查命令自发现）
- [`@juicesharp/rpiv-advisor`](https://pi.dev/packages/@juicesharp/rpiv-advisor)（无子代理全上下文 Advisor 思路）

## License

MIT
