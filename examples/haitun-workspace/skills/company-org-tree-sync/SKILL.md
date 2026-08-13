---
name: company-org-tree-sync
description: "自动刷新 LLM wiki 工作树的组织关系——调用 sync_org_tree，按飞书通讯录每人自带的 leader_user_id 重算全公司上下级关系，写进每人 wiki 汇总页的「组织关系」区块与根页《公司工作树》。Use when the org-tree-refresh schedule fires (fire=prompt), or when a human explicitly asks to refresh the org tree by hand (e.g. 有人入职/离职/调岗后). Independent of company-todo-sync's own step-0 call to the same tool — this skill exists so the org tree can be kept current WITHOUT depending on the rest of the company-todo 体系（todo 看板表格/请假表/mentor 台账模板）就绪，那些还没配好时也能先跑这个。Companion: company-todo-sync (它在自己的采集周期里也会顺带调一次同一个工具，两边幂等、互不冲突)。"
category: productivity
---

# 组织工作树 · 自动刷新

只做一件事：把飞书通讯录的组织架构同步进 LLM wiki 工作树。不采集 todo、不判请假、不派卡、
不建飞书任务——那些是 `company-todo-sync` 的职责，依赖 todo 看板表格等尚未配置好的前提。
这个技能刻意跟那条流水线解耦，好处是组织架构这一件事可以先自动跑起来，不用等其余环节就绪。

## 用到的工具

- `sync_org_tree`（已有，`tools/sync_org_tree.py`）——本技能唯一要调用的工具。

## 呈现形式（内容优先，不做额外渲染）

输出就是结构化文本：每人一个 wiki 页面里的「组织关系」区块（上级/下属，用 `[[wikilink]]`
表示），加一个根页《公司工作树》的缩进链接列表。不额外建飞书 Wiki 节点树、不画组织架构图——
如果后续这个格式不够用，再考虑让 `sync_org_tree` 顺带落一份自定义 JSON，供以后的可视化界面读取；
可视化本身不在这个技能的范围内。

## 流程

1. 调用 `sync_org_tree(root_department_id="0", boss_open_id=<公司 boss 的 open_id，不知道就先留空>, user_id_type="open_id")`。
   - 留空 `boss_open_id` 时，工具会用组织架构里自己解析出的根节点（无上级的人）当锚点；
     如果解析出多个「无上级」的人，结果里的 `anomalous_roots` 会列出来——如实报告，不要自己挑一个当 boss。
2. 读返回结果，按需要向操作者报告三类异常，不要吞掉：
   - `unresolved_leaders`：某人的上级在本次名单里找不到对应记录（常见于上级不在
     `root_department_id` 范围内，或被通讯录权限范围排除）。
   - `cycles_detected`：检测到环形上下级引用（数据异常），已在树里截断显示，需要人工核实飞书通讯录。
   - `anomalous_roots`：除锚点外，还有其他没有可解析上级的人，需要核实是否也该汇报给某人。
   - `pages_failed`：某些人的 wiki 页写入失败，逐条列出失败原因。
3. 幂等：组织架构没变时重复调用不会产生变化，可以放心按 schedule 定期重跑，不需要先判断"是否要建树"。

## 边界

- 不处理 todo/请假/mentor 台账/任务派发——那些是 `company-todo-sync` 的职责，本技能不越界。
- 不建飞书 Wiki 节点、不画组织架构图、不建可视化界面——展示层的工作留到以后单独做。
- `boss_open_id` 不确定时留空让工具自己解析，不要臆造一个人当 boss；解析出多根时如实上报
  `anomalous_roots`，不要自己挑一个吞掉其余的。
- 这是 wiki 汇总页里唯一被本技能触碰的区块（`## 组织关系`）；不动同一页面上 `company-todo-sync`
  维护的「历史索引」「当前目标」区块。

## 待办：还需要人工做一次的事

这个 SKILL.md 只是"遇到触发条件时该做什么"的说明，实际的自动触发（schedule）需要在真实运行的
session 里手动建一次——即调用 `schedule_manage(action="create", cron=<你定的频率，如 "0 15 * * 1,3,5"
跟其余周期对齐，或更频繁/更稀疏皆可，组织架构变动不频繁时按周甚至按月都够用>, fire="prompt",
prompt="运行 company-org-tree-sync 技能")`。这一步不在代码仓库里，是部署时的运行时操作，不是这次
改动的一部分。
