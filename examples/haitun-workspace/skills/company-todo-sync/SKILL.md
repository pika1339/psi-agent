---
name: company-todo-sync
description: "公司 TODO 管理体系·采集与派发 —— 周一三五定时从飞书 todo 看板表格采集全员目标/todo，判请假，建/更 LLM wiki 工作树快照，写各 mentor 独立 Bitable 台账，推报表，派发 TODO 卡与飞书任务。Use when the todo-cycle-sync schedule fires (fire=prompt, cron '0 15 * * 1,3,5'), or when a human explicitly asks to run one sync cycle by hand. Depends on company-todo-audit having already run this cycle (回流项已经算好) — do not run sync before audit in the same cycle. Companion skills: company-todo-review (mentor 评价回写), company-todo-audit (闭环判定与回流)."
category: productivity
---

# 公司 TODO 管理体系 · 采集与派发

按固定节奏（周一三五 15:00）把一张飞书 todo 看板表格采集成结构化数据，建/更 LLM wiki 工作树，
写各 mentor 的独立台账，推报表，把每条 todo 作为 TODO 卡 + 飞书任务派给负责人。

**运行顺序硬约束**：本技能必须在 `todo-cycle-audit`（`company-todo-audit` 技能）本周期跑完之后
运行 —— 未闭环回流项要先算出来才能进本次派发列表。两条 schedule 的 cron 已经错开
（audit `30 14 * * 1,3,5`，sync `0 15 * * 1,3,5`，audit 早半小时），正常情况下不需要手动排序；
若手动触发本技能，先确认今天的 audit 是否已经跑过。

## 四个存储的权威边界（先记住，冲突时按这个裁决）

| 存储 | 权威内容 | 谁写 |
|---|---|---|
| 飞书文档表格 | 每人填报的原始目标与 todo 文本 | 员工人工填写 |
| LLM wiki | 工作树结构、历史快照、mentor 评价 | 本技能写；人可随时改「当前目标」区块 |
| Bitable 台账 | 结构化 todo 行、状态、打分 | 本技能写 |
| 飞书任务 | 某条 todo 到底交付没有 | 执行人勾选（`feishu_todo_card_tick`） |

## 用到的工具

- `feishu_api`（GET `/open-apis/wiki/v2/spaces/get_node`）— wiki 链接换 `obj_token`（表格若挂在 wiki 下）
- `feishu_sheet_read` / `feishu_api`（GET `/open-apis/sheets/v3/spreadsheets/:spreadsheet_token/sheets/query`）— 探结构、定位本周期列、逐人取值
- `feishu_leave_query`（本方案新增）— 判请假
- `sync_org_tree`（本方案新增）— 按飞书组织架构（每人自带的 `leader_user_id` 直属上级字段）自动建/更工作树：每人一页写「组织关系」区块（上级+下属），根页《公司工作树》全量重写
- `wiki_read` / `wiki_write` / `wiki_links` — 建/更工作树历史快照
- `feishu_mentor_ledger_ensure`（本方案新增）— 幂等开通 mentor 台账
- `feishu_bitable_create_records` / `feishu_bitable_update_records` — 写台账行
- `feishu_chart` — 渲染统计卡 PNG
- `feishu_api`（POST `/open-apis/task/v2/tasks`）— 建飞书任务（见 `feishu-task` 技能表；main 上任务域已迁移为端点表，**没有** `feishu_task_create` 这个专用工具）
- `feishu_todo_card_send` — 派发可勾选 TODO 卡
- `schedule_manage`（`action=create`，`once_at` + `fire=tool`）— 建截止提醒

## 流程

### 1. 采集（R1）

1. 若表格挂在 wiki 下，先用 `feishu_api` 打 `GET /open-apis/wiki/v2/spaces/get_node` 换 `obj_token`。
2. `feishu_sheet_read` 或 `feishu_api`（`sheets/query`）探结构：读表头行认「日期列」、读首列认「人名行」，
   同时读出每人的 mentor 标注列。**结构每次现场探，不写死**——表头行/人名列/`SHEET_ID` 都可能变。
3. 按今天日期在表头里定位本周期列；找不到就取最接近的一列，并在报表里注明「按 <日期> 列读取」。
4. 逐人取该列单元格，得到本次填报的整段文本（大目标/小目标/todo 三级）。
5. 按缩进与关键词切分三级，各自抽出标题、截止日期、验收人：

   | 层级 | 必填 | 选填 | 缺失时处理 |
   |---|---|---|---|
   | 大目标 | 标题、截止日期 | 友商对比、外部成果（用户数/金额） | 入库并标「待补」，报表高亮 |
   | 小目标 | 标题、截止日期 | 所属大目标 | 无归属则挂到该人当期唯一大目标 |
   | todo | 标题、截止日期、验收人 | 所属小目标、成果物形式 | 缺验收人则取该人 mentor 兜底，报表标注「验收人由 mentor 兜底」 |

6. 缺字段的照常入库（`needs_fix` 标记），**不阻塞整条流水线**——大目标必填的「友商对比」「外部成果」
   缺失时同样入库标「待补」。

### 2. 判请假（R5 的前置条件）

请假必须在派发前判定，否则会给休假的人派活并记逾期。

1. `feishu_leave_query(sheet_token, sheet_name="请假表", date_from=<本周期日期>, date_to=<本周期日期>, names_json=<本次覆盖名单>)`。
2. 按返回结果分三种处理：

   | 情形 | 卡片与任务 | 台账状态 | 报表呈现 |
   |---|---|---|---|
   | 整周期请假（`is_full_day=true` 且覆盖当天） | 不派卡、不建任务 | 请假顺延 | 该人整块标「请假」，不计入逾期 |
   | 部分日期请假 | 照常派，截止日顺延请假天数 | 请假顺延 | 该条标「截止顺延 N 天」 |
   | 请假中已交付 | 照常，不特殊处理 | 按实际状态 | 不标请假 |

3. `feishu_leave_query` 返回空 `results`（无请假表或本周期无人请假）时视为「无人请假」，不是错误。

### 3. 建/更 LLM wiki 工作树（R2 / R3）

**wiki_write 只有整页覆盖、没有 append 模式** —— 「只累加」必须由本步骤自己保证。工作树分两部分，
职责不重叠：**组织关系**（谁的上级是谁、谁的下属是谁）由 `sync_org_tree` 从飞书组织架构自动生成；
**历史快照/当前目标**由本步骤手工维护。两者写同一批人员汇总页，但各自只碰自己的区块。

0. 本周期第一次运行时（或组织架构发生变动后）先调 `sync_org_tree(root_department_id="0",
   boss_open_id=<boss 的 open_id>)`：它会读全公司成员，按每人自带的 `leader_user_id`（飞书通讯录里
   的直属上级字段，比按部门负责人反推更准——两者在"上级不是部门一把手"时会不一致）自动生成整棵树，
   写进每人汇总页的「组织关系」区块，并全量重写根页《公司工作树》。**这一步是幂等的**，组织架构没
   变时重复调用不会产生变化，可以每次同步都顺手调用一次，不需要额外判断"是否要建树"。
   返回里若有 `unresolved_leaders`（某人的上级在本次名单里找不到）或 `cycles_detected`（检测到环形
   上下级引用），如实报告给操作者，不要自己编一个上级或静默吞掉。
1. 每人写一页**独立快照页**：`《张三 todo <本周期日期>》`，内容是该次填报的完整目标与 todo。
   这一页此后不再被任何自动流程改写。同一人同一周期重复运行是幂等的（slug 相同，重写即覆盖同一次内容）。
2. 改写人员汇总页 `《张三》` 前，**先 `wiki_read` 取回全文**，只在「历史索引」区块顶部插入一条
   `[[张三 todo <本周期日期>]]`，其余字节原样写回——已存在该条则不重复插入，也不动 `sync_org_tree`
   写的「组织关系」区块。汇总页的「当前目标」区块本步骤**不碰**（人工改的口径不会被冲掉）；写入报表
   口径前先 `wiki_read` 一次拿到人工改后的当前目标。
3. 首次运行时建本周期索引页 `《TODO 周期 <日期>》`（列出本次覆盖的所有人 `[[链接]]` 与统计数）。
   组织根页《公司工作树》由 `sync_org_tree` 负责，本步骤不再单独建。
4. tags 统一打 `person` / `cycle` / `project` 三类。
5. **按人串行处理**，不要并发改写同一个人的汇总页——后写的会覆盖先写的索引插入。

### 4. 写 mentor 台账（R4）

1. 对本周期出现的每个 mentor 调 `feishu_mentor_ledger_ensure(mentor_open_id, mentor_name, folder_token, template_app_token, boss_open_id, user_key, identity)`
   拿到 `app_token`/`table_id`。**首次调用若返回 `need_identity_choice`/`need_auth`，按提示向操作者要一次
   身份确认**——这是 Feishu `/copy` 端点要求真人身份的硬约束，不是可以绕过的报错。
   返回里 `bot_access` 恒为 `"not_granted"`：本工具**不能**把机器人本身加成协作者（Feishu 权限接口的
   `member_type` 枚举里没有「app 自己」这一种），若后续机器人需要在没有真人 token 时读写该台账，
   需要人工在客户端「更多 → 协作者管理」里把应用加一次。
2. 把本次每条大目标/小目标/todo 写成台账行（`feishu_bitable_create_records`）：
   `周期日期 / 负责人 / mentor / 层级 / 父项 / 标题 / 截止日期 / 状态 / 闭环五要素 / mentor打分 / agent建议分 / mentor评语 / 外部成果 / 友商对比 / 任务GUID`。
   状态默认「待开始」；被请假顺延的写「请假顺延」。
3. `feishu_chart` 渲染本周期统计卡（进度条/分组柱）：大目标/小目标/todo 各多少条、请假顺延多少条。
4. 报表**只推给该 mentor 本人**（私聊 `feishu_message_send_card`），不进群、不群发。boss 不写 todo、
   不收派发，只在需要时看全公司汇总（跨 mentor 的统计要逐个台账 base 读取后合并，不是一次查询）。

### 5. 派发 TODO 卡与飞书任务（R5，顺序不能颠倒）

对每条 todo（非请假顺延的）：

1. **先建飞书任务**：`feishu_api`（POST `/open-apis/task/v2/tasks`，见 `feishu-task` 技能表；
   `due.timestamp` 是**毫秒**字符串，不要传秒），拿到 `task_guid`。**没有专用的 `feishu_task_create`
   工具**——main 上任务域整个迁移为端点表 + `feishu_api`，创建时按验收人（缺则 mentor 兜底）设
   `members`。
2. **再发 TODO 卡**：`feishu_todo_card_send(receive_id=<负责人 open_id>, items_json=<本人本周期全部 todo,
   每条带 task_guid>)`。卡片行链接指向任务，任务不存在就没有可点的目标——顺序不能颠倒。
   `task/v2` 返回体没有 web URL，行链接用官方 applink 协议拼（`feishu_todo_card_send` 已经处理，
   不需要自己拼）。
3. **回写台账**：把 `task_guid` 存进对应记录的「任务GUID」列（`feishu_bitable_update_records`）。
4. **建截止提醒**：`schedule_manage(action="create", once_at=<截止前一天>, fire="tool", tool="feishu_message_send", tool_args=<私聊本人>, visibility="silent")`。
   `once_at` 一次性任务**必须**同时给 `fire="tool"`，框架不接受 `fire="prompt"` 型一次性任务——
   一次 create 就要完整传齐，不要先 `fire=prompt` 再 patch。

## 边界

- 大目标必填字段（友商对比、外部成果）缺失照常入库、标「待补」，不阻塞整条流水线。
- boss 只读汇总，从不参与写 todo，也从不被派发。
- 同一人的 wiki 汇总页按人串行处理，绝不并发写。
- 请假判定只信 `feishu_leave_query`（人工填报的请假表），不用打卡记录反推——缺卡同时对应出差/外勤/
  忘打卡/请假，反推方向是「放宽考核」，比漏判更难被发现；宁可漏填时报逾期，由本人当场申诉。
- 派发飞书任务前必须先判过请假；整周期请假的人不建任务、不发卡。
