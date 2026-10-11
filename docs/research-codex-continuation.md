# Hermes + Codex 首条 message 后提前停止：协议调查

> 调查日期：2026-08-19  
> Hermes 基线：`NousResearch/hermes-agent@2bef9e8f5171d232f78e410e0925e3293def9e92`  
> 用户 PR：[#88093](https://github.com/NousResearch/hermes-agent/pull/88093), head `dffa830acb243f4c58336a86d5df3b48a9c57324`  
> OpenAI Codex 一手实现：`openai/codex@3929c99a97d1aa0fb8000903a4b57b24fbabe742`

## 结论

**最可能的根因（高置信，但需一份真实 SSE 确认）是 Hermes 在 `openai-codex` Responses 流中丢弃了 `response.completed.response.end_turn`。**

OpenAI 官方 Codex 客户端把 `response.completed` 理解为“这一次模型采样完成”，不必然是“这一整个用户 turn 完成”。它从终端帧解析可选的 `end_turn`；当 `end_turn == false` 时将 `needs_follow_up` 设为 true，然后外层 loop 再发起一次模型请求。工具调用也以同样的 `needs_follow_up` 机制让 loop 持续。见 OpenAI Codex 的 [SSE `ResponseCompleted` 解析](https://github.com/openai/codex/blob/3929c99a97d1aa0fb8000903a4b57b24fbabe742/codex-rs/codex-api/src/sse/responses.rs#L112-L120)、[`response.completed` 到 `ResponseEvent::Completed`](https://github.com/openai/codex/blob/3929c99a97d1aa0fb8000903a4b57b24fbabe742/codex-rs/codex-api/src/sse/responses.rs#L464-L472)、[turn loop 处理 `end_turn=false`](https://github.com/openai/codex/blob/3929c99a97d1aa0fb8000903a4b57b24fbabe742/codex-rs/core/src/session/turn.rs#L2521-L2565)和[外层继续条件](https://github.com/openai/codex/blob/3929c99a97d1aa0fb8000903a4b57b24fbabe742/codex-rs/core/src/session/turn.rs#L376-L405)。

Hermes 的 `_consume_codex_event_stream()` 在终端帧中只提取 `usage` / `status` / `id` / `incomplete_details` / `error`，随后构造的 response 也没有 `end_turn`。全部 Codex Responses 路径中没有读取这个字段。见 [Hermes 终端帧处理](https://github.com/NousResearch/hermes-agent/blob/2bef9e8f5171d232f78e410e0925e3293def9e92/agent/codex_runtime.py#L1233-L1264) 与 [response 重建](https://github.com/NousResearch/hermes-agent/blob/2bef9e8f5171d232f78e410e0925e3293def9e92/agent/codex_runtime.py#L1293-L1305)。因此，如果 Codex 先返回一个进度/plan `message`，再以 `response.completed` + `end_turn:false` 表示应继续，Hermes 会把它归一化成普通 `finish_reason="stop"` 并结束用户 turn。这与“返回一个 message 然后就断了”的现象高度一致。

## PR #88093 为什么治不到根

PR #88093 的标题和实际 diff 都是 **`copilot-acp`**，不是 **`openai-codex` / `api_mode="codex_responses"`**。它只做了两件事：

1. 当 `agent.provider == "copilot-acp"` 时，把现有的“短意图确认句”文本启发式从 `off` 改成 `all`；
2. 把合成的 assistant/user nudge 标记为 ephemeral，避免写入持久会话。

见 [PR 描述](https://github.com/NousResearch/hermes-agent/pull/88093)和其主要提交 [`86e89d9`](https://github.com/NousResearch/hermes-agent/commit/86e89d925affa527035a3933442fbd00b6cd8ab0)、[`c85bf24`](https://github.com/NousResearch/hermes-agent/commit/c85bf240f62ecfe9584e1fd4de00d26374a8bfe5)。

对真正的 `openai-codex` 路径，`auto` 模式在 main 上本来就已经是 `codex_only`；PR 新增的 `provider == "copilot-acp"` 分支不会改变任何 Codex Responses 行为。该 PR 也不读 `response.status`、`response.output`、`phase`、`function_call`、`end_turn` 或 `previous_response_id`。

即使影响的真是 `copilot-acp`，这也只是有界的启发式补救：它根据英文短句的“I’ll inspect…”形态猜测是否应继续，而不是消费 provider 的显式 turn-state 信号。它可能缓解某些样本，但会漏掉其他语言/其他表达，也可能对真实短答误触发。

## 协议语义核对

### 1. `response.completed` 不等于外层 agent loop 必须停止

OpenAI Codex 会在一个用户 turn 内多次 sampling。每次 sampling 都以 `response.completed` 结束，但只有 `needs_follow_up == false` 才结束外层 turn loop。`needs_follow_up` 由工具输出处理和 `end_turn:false` 共同驱动：

- `response.output_item.done` 的 `function_call` / custom tool item 经处理后把 `needs_follow_up` 置为 true，工具 future 执行完后再采样；见 [OpenAI Codex output-item loop](https://github.com/openai/codex/blob/3929c99a97d1aa0fb8000903a4b57b24fbabe742/codex-rs/core/src/session/turn.rs#L2275-L2379)。
- `response.completed` 本身只结束当次流；`end_turn:false` 另外强制 follow-up；见 [OpenAI Codex completed handling](https://github.com/openai/codex/blob/3929c99a97d1aa0fb8000903a4b57b24fbabe742/codex-rs/core/src/session/turn.rs#L2521-L2565)。
- 若流在 `response.completed` 之前关闭，官方 Codex 把它当作错误，而不是正常结束；见 [official client stream guard](https://github.com/openai/codex/blob/3929c99a97d1aa0fb8000903a4b57b24fbabe742/codex-rs/core/src/client.rs#L2028-L2055) 及 [failure record](https://github.com/openai/codex/blob/3929c99a97d1aa0fb8000903a4b57b24fbabe742/codex-rs/core/src/client.rs#L2140-L2145)。

Hermes 已正确处理结构化 `function_call`：归一化器以 `call_id` 保留调用，conversation loop 执行工具后继续。但它没有 `end_turn` 这条第二继续通道。

### 2. `function_call` / `function_call_output` 必须用同一 `call_id` 闭环

OpenAI 官方 Function Calling 文档规定：模型输出中 `type="function_call"` 项带 `call_id`、`name`、JSON 字符串 `arguments`；应用执行函数后，将 `type="function_call_output"` 与相同 `call_id` 追加到下一次 input。官方示例同时演示了两种续接：

- 手工回放：先 `input += response.output`，再追加 `function_call_output`；
- 服务器链接：传 `previous_response_id=first_response.id`，当次 input 只传 `function_call_output`。

见 OpenAI Docs [Handling function calls](https://developers.openai.com/api/docs/guides/function-calling#handling-function-calls) 和 [Incorporating results into the response](https://developers.openai.com/api/docs/guides/function-calling#incorporating-results-into-response)。

Hermes 的手工回放在主要形状上是正确的：

- assistant `tool_calls` 被重建为 Responses `function_call` item；
- role=`tool` 被重建为 `function_call_output`；
- 两者使用同一规范化 `call_id`。

见 [Hermes message → Responses input](https://github.com/NousResearch/hermes-agent/blob/2bef9e8f5171d232f78e410e0925e3293def9e92/agent/codex_responses_adapter.py#L410-L727) 和 [Responses output → tool calls](https://github.com/NousResearch/hermes-agent/blob/2bef9e8f5171d232f78e410e0925e3293def9e92/agent/codex_responses_adapter.py#L1398-L1546)。

### 3. 缺少 `previous_response_id` 本身不是根因

Hermes 固定使用 `store:false`，并手工重放历史 output items；它没有在 HTTP Responses 请求中传 `previous_response_id`。这不违反官方协议：OpenAI Docs 明确支持手工状态管理，并要求“Add all response output items, including encrypted reasoning items”；也支持另一种 `previous_response_id` 链接方式。见 OpenAI Docs [Manually manage conversation state](https://developers.openai.com/api/docs/guides/conversation-state#manually-manage-conversation-state) 与 [Passing context from the previous response](https://developers.openai.com/api/docs/guides/conversation-state#passing-context-from-the-previous-response)。

官方 Codex 自身也使用混合策略：HTTP 请求传全量 `ResponseItem` 历史；WebSocket 连接可用上次 response id 发增量 items。见 [`ResponsesApiRequest` 与 WS request 结构](https://github.com/openai/codex/blob/3929c99a97d1aa0fb8000903a4b57b24fbabe742/codex-rs/codex-api/src/common.rs#L251-L308) 和 [WS 增量/全量选择](https://github.com/openai/codex/blob/3929c99a97d1aa0fb8000903a4b57b24fbabe742/codex-rs/core/src/client.rs#L1220-L1297)。

因此，不应把“加 `previous_response_id`”当成当前现象的首要修复。除非同时把 `store`/连接生命周期、失效 ID 回退、全量历史回放都设计清楚，否则仅加 ID 反而可能引入 404/连接绑定问题。

### 4. `phase` 是另一条已修复的边界，不是本 PR 的修复

Codex 可输出 `message` item，并通过 `phase="commentary"|"analysis"` 表示工具前说明/中间文本，`phase="final_answer"` 表示最终答案。Hermes main 已将 commentary/analysis 文本路由到 reasoning，并在只见 commentary 而没见 final answer 时归一化为 `finish_reason="incomplete"`，从而进入有界的 Codex continuation path。见 [Hermes phase 处理](https://github.com/NousResearch/hermes-agent/blob/2bef9e8f5171d232f78e410e0925e3293def9e92/agent/codex_responses_adapter.py#L1413-L1449) 和 [finish reason 判定](https://github.com/NousResearch/hermes-agent/blob/2bef9e8f5171d232f78e410e0925e3293def9e92/agent/codex_responses_adapter.py#L1638-L1673)。

这条历史 bug 已在 Hermes [#41293](https://github.com/NousResearch/hermes-agent/issues/41293) / [#24933](https://github.com/NousResearch/hermes-agent/issues/24933) 中有字段级证据，且维护者声明 main 已通过 PR #58868 修复。它证明“首条 message 可能只是 preamble/commentary”，但当前 main 已有 phase-aware 逻辑。剩余的明显缺口正是不带 commentary phase（或 phase 未传到客户端）但带 `end_turn:false` 的终端帧。

## 证据链：当前失败是怎样发生的

```text
Codex SSE
  output_item.done: message("I’ll inspect that now")
  response.completed: { status: "completed", end_turn: false }
             |
             v
Hermes _consume_codex_event_stream
  保留 message/status，丢弃 end_turn
             |
             v
_normalize_codex_response
  无 function_call + 非 commentary/incomplete => finish_reason="stop"
             |
             v
conversation_loop
  当成最终 assistant answer，停止当前 user turn
```

对照 OpenAI Codex，同一个终端帧会走 `end_turn == Some(false) -> needs_follow_up = true -> continue`。

## 反证与其他可能性

### A. 反证：若真实 SSE 没有 `end_turn:false`，则不能用该缺口解释

`end_turn` 是 ChatGPT Codex backend 的可选扩展信号，不是公开 Responses API 每个终端都必有的字段。OpenAI Codex 源码也将它定义为 `Option<bool>`。如果用户实际抓到的帧为 `end_turn:true` 或缺失，并且 message 是正常 final answer，那么 Hermes 停止符合协议，不应强制续跑。这是为什么修复前必须保存一份真实终端 SSE 帧。

### B. 次可能：provider 真的只返回了普通 completed message

若没有 `function_call`、没有 commentary phase、没有 `end_turn:false`、且 `response.status="completed"`，协议层没有“必须继续”的信号。这时问题更可能是模型未遵循工具指令或 provider 丢了结构化 tool call。PR #88093 的文本启发式可以作为有界容错，但不应代替协议信号，也不应被描述为 Codex Responses 根因修复。

### C. 次可能：结构化 tool call 丢失或泄漏到纯文本

Hermes 已有两种容错：

- 文本中出现 `to=functions.<name>` 但没有结构化 `function_call` 时，当作 incomplete 重试；
- `finish_reason="tool_calls"` 但解析后的 tool_calls 为空时，进入 dropped-tool-call recovery。

如果原始帧里确有 `function_call` 而 Hermes normalized response 为空，则应优先调查 `response.output_item.done` 收集/类型解析，而不是短句启发式。

### D. PR #88093 可能是另一个 ACP 问题，不是本 Codex 问题

`copilot-acp` 是外部 `copilot --acp --stdio` 子进程适配器，它不是 Hermes 的 `codex_responses` transport。如果复现配置中的 provider 实际是 `copilot-acp`，则本文的 `end_turn` 根因不直接适用；应分开记录 ACP `session/prompt` 返回的 `stopReason`、tool-call block 和 permission request。PR #88093 至少需要在描述中明确它是 ACP-specific workaround，不能用来证明 Codex Responses 已修复。

## 建议的下一步（先验证，再修复）

1. 对失败轮次保存脱敏 SSE 事件，至少保留：
   - 每个 `response.output_item.added/done` 的 `type` / `status` / `phase` / `call_id` / `name`；
   - 终端 `response.completed` 的 `id` / `status` / `end_turn` / `incomplete_details`；
   - Hermes normalized `finish_reason` 和 tool_calls 数量。
2. 写一个最小回归测试：`message item + response.completed(end_turn=false)` 必须产生继续信号，而 `end_turn=true/None` 保持现有终止语义。当前 `tests/run_agent/test_run_agent_codex_responses.py` 有多个 `response.completed` fixture，但没有任何 `end_turn` 覆盖。
3. 修复点应在 Codex SSE/normalization contract，而不是再加文本 regex：
   - `_consume_codex_event_stream` 从终端 response 保留 `end_turn`；
   - transport/normalizer 把 `end_turn is False` 映射为明确的 follow-up / `finish_reason="incomplete"`；
   - conversation loop 复用现有 Codex incomplete 有界继续路径，保留该 message item/phase，不需要猜测文本。
4. 如果脱敏帧确认没有 `end_turn:false`，再转向检查 tool-call item 是否被丢失；最后才考虑短意图确认句的有界启发式。

## 置信度

- **HIGH**：PR #88093 修的是 `copilot-acp`，对 `openai-codex/codex_responses` 不生效。
- **HIGH**：Responses 工具闭环需要回放 `function_call` + 匹配 `call_id` 的 `function_call_output`；Hermes 主要结构符合官方文档。
- **HIGH**：手工回放 output items 与 `previous_response_id` 都是官方支持的续接方式；Hermes 未使用后者不构成当前 bug 的充分证据。
- **HIGH**：OpenAI Codex 官方实现解析 `end_turn` 并在 false 时继续；Hermes Codex Responses 路径丢弃它。
- **MEDIUM-HIGH**：丢弃 `end_turn:false` 就是用户这次“一个 message 后停止”的实际根因。要升为 HIGH，仍需一份该失败轮次的脱敏 `response.completed` 帧。
