# 任务：RSI 研究看板三页改版（研究进展 / 全局运行 / RSI Harness 架构流程）

> 给本机 agent。站点：`https://rsi-research-console.pages.dev`（数据由本机服务的 `/api/snapshot`、`/api/runtime` 提供）。
> 改的是看板前端代码，不动研究数据、STATE、实验文件。

## 本目录文件

| 文件 | 用途 |
|---|---|
| `prototypes/研究进展.dc.html` | 研究进展新版原型。布局、文案、配色、交互都在里面；`renderVals()` 里是 10/16 的真实数据快照，仅作对照 |
| `prototypes/全局运行.dc.html` | 全局运行新版原型 |
| `prototypes/Harness架构流程.dc.html` | Harness「架构流程」tab 的新版流程图原型 |
| `现状-*.png` | 改版前截图 |
| `风格参照-策略回测.png` | 目标风格参照（站内现有页面） |

`.dc.html` 是设计原型格式：页面结构是普通 HTML + 行内样式，`{{xxx}}` 是数据占位，`<sc-for>` 是循环，`<sc-if>` 是条件。照着结构和样式在站点里实现，不要把原型文件直接塞进站点。

## 总要求

1. **风格统一**：研究进展、全局运行现在是「期刊」风（米色底、衬线大字、黑色粗分隔线），要改成和策略回测 / 工具管理 / 研究指挥一致。直接复用站内已有 CSS 变量和类（`.badge` `.badge.good` `.badge.warn`、`.bt-stats`、`.tc-card`、分段按钮等），不要另起一套。
2. **一屏放下**：1440×900 视口下，每个 tab 不需要滚动主区域。内容多就拆 tab，不要堆。
3. **小字收起**：解释性文字放悬停提示或点开的详情，图上只留必要标签。
4. **窄屏可用**：手机宽度下卡片和两栏布局要能纵向堆叠。

### 风格要点（取自站内现有 CSS）

- 底色 `#fff`，侧栏 `#f6f6f6`；字体沿用站内 `Inter, "Segoe UI", "Microsoft YaHei", sans-serif`
- 页面标题 17px / 650 / `#243957`；卡片标题 14–15px / 650 / `#243957`
- 卡片：`1px solid #eaeaea`，圆角 12px，无阴影
- 指标数字 25px / 600，`tabular-nums`，单位小一号
- 标签：中性 `#f2f5fb / #5d6b84`，蓝 `#edf3ff / #5876b3`，好 `#eaf8f2 / #138565`，警示 `#fff6e5 / #94681f`，危险 `#fcefef / #b0474b`
- 图表色：蓝 `#496af2`、红 `#da6b6e`、绿 `#20a782`、琥珀 `#e8a747`、灰 `#b4bdcf`
- 次要文字用 `#66758f`（站内 `#7a8aa4` 在白底小字上对比度不够）
- 分段按钮：浅灰底容器 `#f5f6f8`，选中项白底

## 一、研究进展（`#overview`）

**去掉**：「当前保留策略 —」那块空横线；不同口径混在一起的散点「收益趋势」；累计计数的「迭代进度」；大圆环「实验结果」；「各线进展」tab（跨线比较归全局运行）；底部那排「策略指标说明 / 关键改动 / …」链接（改成就近的「查看原件」）。

**页头**：`<线名> 研究进展` + 状态标签（`line.status.label`），右侧一排研究线标签按钮（替换原下拉框，样式同策略回测的线标签），每个标签带状态色点。

**四个 tab**：

1. **概览**
   - 结论一句（按数据生成）：没有候选获准替代时写「还没有候选获准替代父本 X」。
   - 两个可点的提示：回撤合格（≤20%）且年化 ≥ 父本、但 `result=unknown` 的候选数 → 点了切到「收益 × 回撤」并高亮；最近连续回撤超限的完整账户数 → 点了跳研究记录。
   - 4 个指标格：保留父本（年化、回撤、Calmar）｜最好候选（回撤合格中年化最高，显示与父本差 pp）｜最近账户（与父本差、回撤，超限标红）｜获准替代 `improved / (improved+negative)`，副行写待核实数。
   - 主图「每个候选离父本有多远」：横轴批次序号，纵轴年化；父本一条虚线；回撤 ≤20% 实心蓝点、超限红色空心点；顶部浅绿带标「高于父本」。分段切换「收益 × 回撤」：横轴回撤，加 20% 竖虚线，左上角浅绿区是「回撤合格且不低于父本」。点悬停显示：编号、年化、与父本差、回撤、原裁决。
2. **研究记录**：左表最近 10 批（批次 / 研究问题 / 结果标签 / 与父本 pp / 完成时间），右侧选中行详情（年化、与父本、回撤、结论、计划/结果/反思原件链接），底部「全部 N 条 ↗」。
3. **实验与节奏**：三卡并排——实验臂去向（一条分段条：待核实 / 有效负结果 / 未评分 / 获支持，大字写待核实占比）｜每天完成批次柱状图｜需要注意（`line.issues`，标题 + 一句说明）。
4. **策略逻辑**：`story.logic` 的 summary、modules（保留策略 / 研究改动 / 买卖与资金）、limitations、sources。

**数据映射（`/api/snapshot` → `lines[i]`）**

| 用途 | 字段 |
|---|---|
| 父本 | `story.logic.id`、`story.logic.label`、`story.logic.status`；父本年化/回撤取 `economic.rows` 中 id 等于父本的行（ML3 为 E11J：316.42% / 15.55%），没有则取 `trend.points[].baseline_annual_return_pct` |
| 散点 | `trend.points[]`：`sequence`、`annual_return_pct`、`mdd_pct`、`result`(negative/unknown/unscored)、`id`、`metric_status`；只画 `annual_return_pct` 非空的点 |
| 去向计数 | `trend.counts`：`total` `improved` `negative` `unscored` `unknown` |
| 研究记录 | `trend.history.records[]`：`id` `title` `status` `completed_at` `kind` `financial_points`（关联到 points 取年化/回撤）`financial_gap_reason` `sources` |
| 每天批次 | 按 `records[].completed_at` 分天计数（**按北京时间分天**） |
| 需要注意 | `issues[]`：`title` `detail` `severity` |
| 上下文 | `story.context_events`、`story.coverage_note`、`method_evidence` |

`records[].status` 显示文案建议：`complete_risk_limit_failed`→回撤超限，`prepared_or_under_review`→准备中，`complete_negative_control`→负对照，`complete_below_strong_reference`→低于强对照，`complete_rejected_candidate(s)`→候选被否，`development_batch_complete`→完成·待核，`closed_*`→关闭未执行；其余原样显示并补映射。

**没有父本或没有完整账户的线**（数学线、因子线、高涨幅等 `trend.counts.total=0`）：概览显示空状态「这条线还没有完整账户结果」+ 当前任务与下一步（来自 runtime），不要画空图。

## 二、全局运行（`#runtime`）

**页头**：「全局运行」+ 右侧小字「按执行证据判断 · 聊天活动不算实际推进」。

**汇总条**（可点，点了过滤下面列表）：研究线总数｜卡住｜记录不一致｜24 小时内有推进｜超过 24 小时没推进。

**分组规则（`/api/runtime` → `lines[]`）**
- 卡住：`state === 'blocked'` 或 `blocker` 非空
- 记录不一致：`issues` 非空（且不属于卡住）
- 24 小时内有推进：`last_progress.at` 距 `observed_at` ≤ 24h
- 超过 24 小时没推进：其余（含 `last_progress` 为空，显示「无记录」）

**左表**：按上面四组分段（组标题带色点和数量），每行：研究线｜当前任务（`task`，单行省略）｜状态标签｜最近实际推进（相对时间，>1 天或无记录标红）。

**右侧详情**（点行切换，默认选第一条卡住的）：线名 + 标签 + 阶段（`stage`）；完整 `task`；卡点（`blocker`）或不一致说明（`issues`）用浅色块突出；正在做什么（`activity`）；下一步（`next_action`）；最近实际推进（`last_progress.at` + `label`）；Agent 最后活动（`agent.last_activity_at`）；链接：看研究进展 / 去研究指挥 / 查看依据。

`activity`、`next_action` 里有 Markdown、本地路径、`<heartbeat>` 之类原文：详情里去掉 Markdown 标记和 `<…>` 标签，绝对路径折叠成文件名；不要截断成一行。

## 三、RSI Harness →「架构流程」tab

按 `prototypes/Harness架构流程.dc.html` 实现：01–06 围成环形主循环，06 后的「方法缺口？」判断分出 H1–H5 改进支路，H2/H3/H4 的失败回退画成线连回 06；节点只留编号和标题，说明放悬停和侧边面板；「显示各线位置」开关。配色改用上面的站内风格（原型里的绿/紫/橙换成站内蓝/灰/琥珀/红，保持「主循环 / 改进支路 / 失败路径」三类可区分，且不只靠颜色区分——线型或标签也要不同）。不改「环节与规则」tab 和右侧「各线运行」栏。

## 验收

- [ ] 三页在 1440×900 下主区域不出现纵向滚动条（Harness 架构流程除外，可滚）
- [ ] 研究进展切换每条研究线都能正常显示；无数据的线显示空状态
- [ ] ML3 概览数字与原型一致：父本 316.42%，最好候选 B86:H86 318.33%（+1.91 pp），最近账户 B129:P129 267.14%（−49.28 pp，回撤 24.42%），获准替代 0 / 56，待核实 170
- [ ] 全局运行分组数与当时数据一致（10/16 17:24 为 卡住 3 / 不一致 2 / 有推进 4 / 没推进 2）
- [ ] 视觉与策略回测、工具管理同一套：同字体、卡片、标签、按钮
- [ ] 390px 宽度下不出现横向滚动
- [ ] 改完截三页截图放回本目录 `after-*.png`，并在本文件末尾写一段「改了哪些文件 / 没做的 / 偏离原型的地方」
