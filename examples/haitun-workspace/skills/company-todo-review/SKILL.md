---
name: company-todo-review
description: "公司 TODO 管理体系·评价回写 —— 负责人交付一条 todo 后，向其 mentor 发送 1-5 分评价卡；mentor 提交后把打分/评语写回台账并追加到该人 wiki 快照页对应 todo 之后。Use when a todo's Feishu task is marked complete (交付事件驱动，通常由 feishu_todo_card_tick 之后的下一轮触发本技能发评价卡), or when a <feishu_card_action> callback with dispatch.handler pointing at this skill's review card arrives. Companion skills: company-todo-sync (采集与派发), company-todo-audit (闭环判定)."
category: productivity
---

# 公司 TODO 管理体系 · 评价回写

一条 todo 交付后，让其 mentor 打分评语，回写权威台账并追加进本人 wiki 快照页——这一步是
闭环五要素里的第 4、5 项，缺了任何一半都停在「未闭环」（见 `company-todo-audit`）。

## 用到的工具

- `feishu_message_send_card` — 发 1-5 分评价卡（按钮 + 「打回重做」+ 评语表单）
- `feishu_bitable_update_record` / `feishu_bitable_search_records` — 定位并写回台账打分/评语字段
- `wiki_read` / `wiki_write` — 把评价追加到本人快照页对应 todo 之后

## 交付后发卡

1. 负责人交付（`feishu_todo_card_tick` 已把飞书任务 `completed_at` 写成非空）之后，向该条 todo 的
   mentor 私聊发一张评价卡：按钮为 1-5 分，另加一个「打回重做」按钮；评语走卡片 `form` 一次提交
   （不要用 standalone 选择器——SDK 1.2.0 无法完整区分选项变化）。
2. 按 AGENTS.md「Feishu interactive-card callback contract」的约定，发卡时必须同时传：
   - `business_context_json`：业务类型（`company_todo_review`）、`arrangement 或台账 record_id`、
     发起人（负责人 open_id）、当前状态等接收方（mentor）独立处理所需事实。
   - `action_handlers_json`：把每个按钮/表单的 `value.action` 映射到本技能的处理入口
     （评分按钮 1-5 各自不同 `value`，「打回重做」单独一个 `value`）。
3. 按钮 `value` 必须包含明确动作名和稳定业务 ID（如台账 `record_id`），不同按钮用不同值。

## 收到 mentor 提交后

当前最新消息是 `<feishu_card_action>` 且 `dispatch.handler` 指向本技能时：

1. 从回调 `business_context` 里取台账 `record_id`、对应人、对应 todo 标题——不臆造，读不到就
   `fail closed`。
2. `feishu_bitable_search_records` 或直接用回调携带的 `record_id` 定位那一行；
   `feishu_bitable_update_record` 写 `mentor打分`（1-5）、`mentor评语`（表单提交的文本）两个字段。
   若是「打回重做」，把状态改回「进行中」而不是写打分。
3. `wiki_read` 该负责人的**本周期快照页**（`《张三 todo <周期日期>》`，不是汇总页——评价追加进
   历史快照，不改「当前目标」区块），在对应 todo 条目之后插入一行评价（打分 + 评语 + 时间），
   其余内容原样保留，再 `wiki_write` 整页回写。
4. **agent 建议分只作参考**：按「是否按期、是否一次通过、成果物是否齐备」给 1-5，与 mentor 分
   并列存进台账的 `agent建议分` 字段，冲突时以 mentor 为准，不覆盖 mentor 的权威分。

## 卡片回调纪律（与 AGENTS.md 一致，此处不重复推导，只点名）

- 每张卡片按 `message_id` 只接受首个有效操作，随后把交互区替换为只读的「已评分: N 分」提示；
  再次需要评价必须发新卡片，不能对同一张卡片二次收集。
- 写操作成功且卡片已经承载全部必要信息时，本轮以零 assistant 文本结束；只有警告、部分失败、
  权限问题或必要后续步骤才回复。
- snapshot 缺失或损坏时 `fail closed`，不假定是旧卡片、不臆造已匹配的 handler。

## 边界

- 评价的权威落点有两个：Bitable 台账（供统计/闭环判定读取）与 wiki 快照页（供人工回溯）。
  两处都要写，缺一处闭环判定第 4/5 项就过不了。
- 只有该条 todo 的 mentor 本人的评分算数；不接受负责人自己给自己打分、也不接受群聊场景下的评价卡
  （评价卡只私聊发给 mentor 本人）。
- 「打回重做」不写打分/评语，只把状态改回「进行中」，等下一次真正交付后再发新的评价卡。
