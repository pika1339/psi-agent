---
name: feishu-todo-card
category: knowledge-base
description: 给某人发一张「今日 TODO」卡片：一张卡列多条待办，每条一个勾选形状(○●/□■/◇◆/△▲/☆★/☐☑)、一个详情链接、一个对应的飞书任务。勾一条只结那一条，其余仍可勾，卡片原地更新；点错了还能撤销、撤销后能再勾（有轮数上限，见下）。可选联动 mentor 台账的「状态」字段。用于每日/每周待办推送、清单式派活、以及任何「一条消息里要办好几件事」的场景。也讲清多选卡与普通单次卡的区别、per-row 幂等边界、以及 task_guid 怎么来。
---

# 飞书 TODO LIST 卡片

一张卡多条待办，**逐条勾选**。这跟普通卡片的根本区别：普通卡片点一次就整张退休（防重复提交），
TODO 卡把「一次性」的粒度从**整张卡**降到**每一行**。

## 用哪个工具

| 场景 | 工具 |
|---|---|
| 一张卡列多条待办、逐条勾 | `feishu_todo_card_send` |
| 勾选后的回调（标飞书任务完成、发起撤销窗口） | `feishu_todo_card_tick`（卡片自动派发，不用手调） |
| 撤销后的回调（重开飞书任务、发起再次可勾） | `feishu_todo_card_untick`（卡片自动派发，不用手调） |
| 一张卡只要一个答案（同意/驳回、选一项） | `feishu_message_send_card`，**别用** TODO 卡 |
| 自己拼多选卡（非待办形状） | `feishu_message_send_card` + `multi_use=True` |

## 标准流程

**先建飞书任务，再发卡。** 每行的链接指向它自己的飞书任务，任务不存在就没有可点的目标。

1. 对每条待办 `POST /open-apis/task/v2/tasks`（见 `feishu-task` 技能），拿回 `task_guid`。
2. 组 `items_json`，把 `task_guid` 填进对应行。
3. `feishu_todo_card_send(receive_id="ou_...", items_json=...)`。

```text
feishu_todo_card_send(
  receive_id="ou_xxx",
  title="8月5日 待办",
  subtitle="来源: 团队 TODO 表 | mentor: 张三",
  items_json='[
    {"title":"写周报","task_guid":"abc-123","detail":"周五 18:00 前","shape":"square"},
    {"title":"改设计文档","task_guid":"def-456","detail":"评审前完成","shape":"circle"}
  ]')
```

## 每行可配的字段

- `title` — 待办文字（必填；空的会变成「任务 N」）。
- `task_guid` — 对应的飞书任务，渲染成 applink 链接。
- `link` — 显式 URL，**覆盖** applink（想链到文档而非任务时用）。
- `shape` — 该行的形状：`circle` ○● / `square` □■ / `diamond` ◇◆ / `triangle` △▲ /
  `star` ☆★ / `check` ☐☑。不填则用卡片级 `shape`。**按任务类型区分形状**就靠这个字段。
- `detail` — 标题下的第二行（截止时间、验收标准）。
- `done` — 预置已完成：渲染成删除线且不给按钮——这类行**没有进入** tick/untick 流程，
  没有撤销能力（它本来就不是通过点击完成的）。
- `ledger_record_id` — 这一行对应的 mentor 台账（Bitable）记录 id。配合
  `feishu_todo_card_send` 的 `ledger_app_token`/`ledger_table_id` 参数一起给，勾选/撤销
  时会把该记录的「状态」字段同步写成「已交付」/「待开始」。三者缺一不联动，静默跳过，
  不报错。

未完成 = 空心 + 加粗；已完成 = 实心 + 删除线 + 一个「撤销」按钮（除非已到轮数上限，见下）。

## 撤销：点错了能点回去，但有轮数上限

飞书卡片的一个 action_id **点一次就永久失效**（墓碑机制），而编辑一张已发出的卡片**不能**
给它追加新的可分发 id——分发表是发卡那一刻就冻死的。所以「撤销后还能再勾」必须在发卡时就把
后面每一轮会用到的 tick/untick id **全部预注册好**，不是用到才补。

`feishu_todo_card_send` 因此给每一行预注册 `_todo_card_impl._UNDO_ROUNDS`（20）轮的
tick/untick id 对，但初始卡片只渲染第 0 轮的「标记完成」按钮——其余按钮随着用户点击，由
`feishu_todo_card_tick`/`feishu_todo_card_untick` 在事后用 `feishu_message_edit_card`
补上。20 轮对"手滑点错"这种场景足够宽裕；轮数用尽后该行锁定为最终态、不再出现任何按钮，
这是明确的上限，不是隐藏截断。

**在这个改动上线之前发出去的卡没有这个能力**——它们的分发表里只注册了裸的
`todo_tick_<行号>`，没有轮次、没有 untick id，撤销按钮永远不会出现在那些卡上。

## 关于链接：applink，不是 web url

`task/v2` 的返回体里**没有** web URL 字段，别去等它。工具用官方客户端跳转协议拼：

```text
https://applink.feishu.cn/client/todo/detail?guid=<task_guid>
```

要跳到别处（文档、多维表格记录）就填 `link` 覆盖掉。

## 幂等边界（会踩的地方）

**逐行 at-most-once，不是逐卡。** 勾第 1 行不影响第 2 行；重复勾第 1 行恰好被拒一次
（跨进程、跨重启都有效，靠 per-action 墓碑文件）。并发同时勾两行也各自成立。

**每行的 `action` 必须唯一且规范**（无前后空格）。工具自动生成 `todo_tick_<行号>_r<轮次>` /
`todo_untick_<行号>_r<轮次>`，自己拼卡时务必照办 —— 撞名会互相顶掉。**没有可用 action id 的行
会退回整卡去重**，也就是退化成普通单次卡：点一下整张卡就没了。

**已勾状态会回写快照。** 否则第二次勾会从原始卡渲染，把第一行的完成状态覆盖回未完成。这一步
是框架做的，但如果日志里出现 `failed to persist ticked card`，就说明后续勾选会显示错行 —— 重发一张新卡。

**防重放靠两层，各管一段：**

| 层 | 挡什么 | 作用域 |
|---|---|---|
| 墓碑文件 `{message_id}.{action}.consumed` | 同一行被重复消费（含飞书 at-least-once 重投、进程重启后重投） | 跨进程、跨重启 |
| 每卡一把 `anyio.Lock`（读-改-写临界区） | 两行同时勾时交错覆盖彼此的完成状态 | 单进程内 |

墓碑用 `Path.touch(exist_ok=False)`，在 CPython 上**就是** `os.open(O_CREAT|O_EXCL|O_WRONLY)`，
是原子的（`exist_ok=True` 才会走非原子的 `utime` 路径）。

锁只在单进程内有效，这是**够的**：一个飞书 app 只能有一条 WS 长连接消费者，这是飞书平台的限制
而非本项目的选择（本机起两个实例会互相抢连接）。所以同一张卡的并发勾选必然落在同一个进程里。
真要出现多进程分别收到同一张卡的回调，锁失效但墓碑仍然成立 —— 退化后果是「某一行的完成状态可能
被另一进程的回写覆盖」，不是重复执行动作。

**自己拼多选卡时别用 `feishu_message_edit_card` 给它加新按钮。** 编辑一张已发出的卡片不会
重新注册**分发表**（action id → handler 的映射），凭空加进去的 action id 永远是死的——
`feishu_todo_card_tick`/`feishu_todo_card_untick` 之所以能安全调用它，是因为它们只引用
**发卡那一刻就已经预注册好**的下一轮 id（见上面「撤销」一节），不是临时现造一个。

**但 `feishu_message_edit_card` 现在会同步更新 multi_use 卡片的快照**（`edit_card_impl`
成功后调用 `rewrite_card_snapshot`），所以只要 action id 本身是发卡时就注册好的，编辑后的
卡片内容不会和框架自己保存的"这张卡长什么样"脱节——不会再出现"下一次点击核对不上快照、
退化成一张写死的「已提交」占位卡、还要多等几秒去问飞书要最新内容"这种情况。这条修复也是
`feishu_todo_card_tick`/`_untick` 点完之后能保持原本 TODO 卡样式、响应速度和第一次点击
一致的原因。

## 连点会被合并成一个回合

卡片是立刻重绘的，但你这一轮处理要几秒，而每个 session 只有一把锁。用户等不及连勾 5 条，
本来会排成 5 个回合、回 5 条消息。框架因此加了合并闸：**在途回合期间到达的点击，全部并进
下一个回合**，按 `(message_id, 点击者)` 分键 —— 群卡里两个人各点各的，互不合并、各自回复。

合并后你会收到一个批量壳，里面是多条 `<feishu_card_action>`：

```
<feishu_card_action_batch count="3">
<feishu_card_action>...</feishu_card_action>
<feishu_card_action>...</feishu_card_action>
<feishu_card_action>...</feishu_card_action>
</feishu_card_action_batch>
```

**每条都要处理**（逐条调 `feishu_todo_card_tick`，一条都不能漏 —— 合并只省回复，不省动作），
但**只回一条消息**总结，或者干脆 `NO_REPLY`。不要一条点击回一段话。

## 不要用飞书原生 checker

飞书有 `checker` 组件（Card 2.0），看着最像 todo 勾选框，但框架只把 `action`/`button`/`form`
当交互元素 —— **`checker` 不在其中**，点了不会被消费机制识别，一次性保证和「已完成」回显都不生效。
所以这里用文本形状字符 + 按钮，形状反而更自由。

## 完成/撤销后发生什么

勾选 → 框架先把该行改成 `● ~~文字~~` 的即时占位并原地更新卡片 → 派发 `feishu_todo_card_tick`
→ `PATCH /open-apis/task/v2/tasks/:task_guid` 写 `completed_at`（**毫秒**）+
`update_fields` → 若该行带 `ledger_record_id` 就同步台账「状态」→ 已交付 → 再编辑一次卡片，
把占位换成真正的完成态 + 下一轮的「撤销」按钮（除非轮数已到上限）。

撤销 → 同样先经框架的即时占位 → 派发 `feishu_todo_card_untick` → 把任务
`completed_at` 写回字符串 `"0"` 重开 → 台账状态（若有）改回「待开始」→ 编辑卡片把该行还原成
未完成态 + 下一轮的「标记完成」按钮。

漏了 `update_fields` 飞书会返回成功但一个字段都不改。这两个工具都已经带上了，自己调 API 时注意。

行内没有 `task_guid` 时，返回 `task_updated: false` 并说明原因 —— 这不是错误，只是没有任务可动；
台账联动同理，缺 `ledger_record_id`/`ledger_app_token`/`ledger_table_id` 三者之一就静默跳过。

**卡片会被再编辑一次，但不用你自己发。** 两个工具内部已经调用了 `feishu_message_edit_card`
把「撤销」/「标记完成」按钮补上去；不要再手动发一遍、不要复述点击动作；只有任务更新失败才
需要回话。

## 权限

标任务完成默认走机器人 token。**机器人不是任务成员时会被拒** —— 这时传 `user_key`（点击者的
open_id），以本人身份完成。定时批量建任务读公共表的权限问题见 `feishu-unattended-access`。

## 上限

单卡最多 40 条，超了先拆卡。行数多时飞书客户端会折叠，重要的放前面。
