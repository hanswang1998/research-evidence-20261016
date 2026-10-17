# Harness 反思与改进闭环

2026-10-15，接口 `research_harness.v1`。这是结构／旧 ML 调度的可选维护入口；接入其他线须适配本线消费者，不能仅凭这份文档宣称接通。

当前 Agent 根据证据修改版本包，Python 组装实际任务、校验生成物、比较结果并冻结下一批绑定。原 `next → observe → review-pilot → export` 继续执行。代码不另启模型、不付费调用、不提交实验、不恢复任何研究线，也不改已有冻结 ENGINE、PLAN、SPEC、批次或经济评分器。

## 1. 闭环中谁做什么

2026-10-16 补充：共同研究行为按[研判与选题核心](harness/研判与选题.md)及[研究能力规范第 3、6 节](研究能力改进规范.md)接续。**实验判断更新、执行工具修复、Harness 方法比较是三个不同动作。** 代理漏掉父本逻辑应先修正下一实验；现金／订单实现错误按原权限修工具；只有证据指向反思指令、候选生成或上下文组织的缺陷，才走下面的可选版本比较。协作／执行器改造不在当前三个组件接口内，须沿本线做有界适配。

Agent 不必等用户再次纠偏：发现明确方法缺陷时，在原反思写清触发原件、出错步骤、预期行为变化、保留行为及回退，直接推进权限内的提案与验证。一次有效负结果本身不是方法缺陷；原合同停止条件、用户暂停及预算继续有效。可选接口、独立复核或完整成本暂缺时，保留原方法并继续无依赖的已授权研究，不因方法支路未完成就停掉主循环。

`research_harness_default.json` 的反思指令和生成问题已同步这四步反馈要求；它只服务新的显式版本绑定。实际旧方法须按原件固化，不能把更新后的默认模板说成历史父版；已冻结 TASK／GUIDANCE／ENGINE／HARNESS 仍按其原版本执行。

```mermaid
flowchart TD
    A[已关闭批次的决策、结果、失败和成本] --> B[冻结问题、原始证据、基线、模型、预算和判据]
    B --> C[Agent 读取 REFLECTION_TASK 并提出一个具体改动]
    C --> D[独立复核提案：证据、成功行为、预测、停止条件]
    D --> E[旧版与候选分别组装实际 TASK]
    E --> F[Agent 按各自任务生成原格式候选池与 SPEC]
    F --> G[绑定调用证据、独立评价、五项成本]
    G --> H{完整配对比较}
    H -->|失败、未知或无优势| I[保留原版，保留失败反馈]
    H -->|开发比较有优势| J[仅获得有界试点资格]
    J --> K[下一未冻结边界绑定新任务、新候选池]
    K --> L[原 next 与 observe 执行并记录版本来源]
    L --> M[原 review-pilot 与 export 关闭批次]
    M --> A
    I --> B
```

Agent 不应只写“加强反思”。须回答：原判断及证据、反例和竞争解释、哪个具体方法步骤出错、哪些成功行为必须保留、改动如何改变下一次行为、预期改善什么、什么观察会证伪或触发回退。没有可支持的方法改动时保留原方法，不为凑候选重复实验。

运行入口是 [research_scheduler.py](../engine/research_scheduler.py)；新增逻辑在 [research_harness.py](../engine/research_harness.py)。[默认版本包](../engine/research_harness_default.json)是新试点的明确起点，**不代表复原了历史 Agent 的全部提示词**。需要旧版对照时，先把实际旧指令和组织方式如实固化为基线，不能拿新模板冒充旧行为。模板的 `branch` 为 `structure`；旧 ML 使用另存的 `ml` 版本，不原地改模板或他线输入。

## 2. 开放的修改面

| 组件 | 实际作用 | 约束 |
| --- | --- | --- |
| `prompts` | 反思指令和生成指令进入每次 TASK；选用的反思指令也进入下一轮 REFLECTION_TASK | 两个固定槽位，每项非空、最多 16000 字符；不能覆盖固定执行约束 |
| `generation` | Agent 收到必须考虑的问题；生成池按候选上限检查 | `questions` 为 1—12 项；`max_candidates` 为 1—8；`min_families` 为 1—候选上限。不足机制族必须给真实缺口和补充动作 |
| `context` | 按声明顺序组装真实原件摘录、路径、SHA 和行号 | `goal/evidence/failures/successes/unknowns` 五节全部保留；单项摘录 128—12000 字符；截断明示，完整引用保留 |

每轮最多两项候选，每项只改一个组件，全部候选在任何一次评价任务准备前冻结；拒绝项也占名额且不删除。变更后采用新文件和新 SHA。Python 不接受任意候选代码、评分器、执行授权或交易表达字段。`cycle --rrsi` 原有的 `priority/max_depth` 修改仍是另一条入口，不能与 harness 试点同时改动。

`TASK.instructions` 是 Agent 必须实际使用的任务输入；`generation` 接口要求原始回复、调用／执行证据及生成池绑定该 TASK。程序校验结构和来源，不会代替 Agent 写候选，也不会只看到“我用了新版”就将其当作效果证明。文件哈希不能证明模型调用和独立复核的真实性，原调用记录与复核过程须如实保留。

## 3. 冻结一轮比较

在原 PLAN 绑定方法问题、授权、版本与预算；引用原证据，不复制大日志。准备一个 `research_harness_plan.v1` JSON：

| 字段 | 内容 |
| --- | --- |
| `schema / branch` | `research_harness_plan.v1`；`structure` 或 `ml` |
| `kind` | `synthetic`、`development` 或 `prospective_pilot` |
| `protocol / evidence` | 原方法计划引用；非空反思证据引用列表 |
| `incumbent` | 当前 harness 版本包引用 |
| `agent_model` | 实际模型身份，两组固定一致；不是切换模型的指令 |
| `limits` | `context_chars` 1000—120000；`resource_seconds` 正数；`candidates` 1—2 |
| `cost_contract` | 原 `research_cost_contract.v1` 引用 |
| `selector` | 原选择器的 `rule`；若为 `bottleneck_evidence_cost_v1`，同时绑定原 `selection_contract` |
| `cases` | 1—12 项，分别为 `{input: CASE引用, evaluation_contract: 阶段合同引用}` |
| `previous_review`（可选） | 上次 harness REVIEW，下一基线必须等于其 `next_harness` |
| `execution_feedback`（可选） | 已关闭执行批次的 PILOT_REVIEW；其实际 D/O 证据必须进入本轮 `evidence`，基线遵循其 `next_harness` |
| `pilot_offer`（前瞻必填） | 开发轮给出的试点资格 REVIEW；候选由该报告锁定，不再修改 |

引用一律 `{ "path": "原件绝对路径", "sha256": "原件真实SHA256" }`。不要使用占位哈希。公共输入、SPEC 和来源只引用；每轮仅冻结 ENGINE 与 HARNESS 两份小程序，旧的单文件 ENGINE 继续独立使用。

CASE 格式：

```json
{
  "schema": "research_harness_case.v1",
  "branch": "structure",
  "id": "problem_01",
  "question": "本问题要区分哪些机制？",
  "context": [
    {"id": "goal", "section": "goal", "source": {"path": "目标原件绝对路径", "sha256": "真实SHA256"}, "lines": [1, 8]},
    {"id": "failure", "section": "failures", "source": {"path": "失败原件绝对路径", "sha256": "真实SHA256"}, "lines": [10, 30]}
  ]
}
```

每个 CASE 最多 40 项上下文，必须包含 goal；原件为不超过 2 MB 的 UTF-8 文本或事先保留来源的小摘录。行号从 1 开始。没有提供某节时明示未知。总任务超出冻结字符预算直接拒绝，不静默丢弃失败或成功证据。

每个评价合同沿用 `research_stage_evaluation.v1`，`questions` 仅包含对应 CASE ID，冻结信息／工程判据及独立核验要求。冻结前选择能够区分方法优劣的实质判据，例如能否区分指定竞争解释、能否保留关键成功机制；字段齐全本身不能代替这些判断。实际生成阶段不能填经济分数。

```powershell
python -X utf8 -B engine/research_scheduler.py harness-init --spec '计划绝对路径' --out '新的本轮目录'
```

这会写 `CONTRACT.json`、`REFLECTION_TASK.json`、`ENGINE.py`、`HARNESS.py`。后续都用**本轮冻结 ENGINE**：

```powershell
python -X utf8 -B '本轮目录/ENGINE.py' harness --round '本轮目录' --request '操作请求JSON绝对路径'
```

冻结模型、原始信息和每次预算保持一致。资源秒数是实际活动成本；本入口核验回执和预算结果，不能替代现有执行器的超时控制。采用同一 Agent 时，两组输入隔离，不把另一组输出加入 CASE；需要另一个模型、付费执行或外部服务时继续按原授权边界处理。

## 4. 操作请求与真实生成物

| `operation` | 请求中的其他字段 | 结果 |
| --- | --- | --- |
| `propose` | `proposal` 与 `review` 两个引用 | 保留候选和独立复核；通过才可评价 |
| `prepare` | `bundle`、`case` 引用；默认 `purpose=evaluation` | 返回实际 `instructions` 和 TASK 引用 |
| `generation` | `task`、`response` 引用 | 校验 Agent 输出并保存 GENERATION，引用实际 FRONTIER |
| `result` | `task`、`status`、非空 `evidence`；完成时带 `generation/stage_evidence`；成本见下节 | 保存不可覆盖的 RESULT |
| `review` | 可附 `evolution_cost_receipt` | 核对全部配对结果，保存 REVIEW；该轮评价关闭 |

提案 `research_harness_proposal.v1` 绑定 `parent`、`reflection_task`、`evidence`、`bundle`、`component`，并写非空 `diagnosis / alternative_explanation / preserve / stop_rule`。`expected_effect` 当前支持 `{metric: passed_checks, direction: increase}` 或 `{metric: total_seconds, direction: decrease}`，用来核对预测而非自报成功。

独立复核 `research_harness_critic.v1` 绑定 `proposal`、本轮 `contract`、非空 `evidence`、`verifier` 和 `independent=true`；`checks` 恰为四个布尔项：`evidence_grounded / bounded_change / preserves_success / testable_prediction`。须实际复核，不用程序自动填四个 true。

Agent 读完 TASK 后使用其反思、生成和上下文要求，输出原 `research_frontier.v1` 与 SPEC。FRONTIER 的 `generation_evidence` 必须含 TASK 引用，仍执行原来源、身份、依赖、短缺说明等校验；新选择器还须满足其原 SPEC 格式。再保存 `research_harness_response.v1`：

```json
{
  "schema": "research_harness_response.v1",
  "task": {"path": "实际TASK绝对路径", "sha256": "真实SHA256"},
  "agent": "实际执行者身份",
  "agent_model": "与冻结计划一致的实际模型",
  "reflection": "基于本次证据的判断、改动依据、保留行为及证伪条件的简要说明",
  "trace_evidence": [{"path": "实际调用或执行记录的绝对路径", "sha256": "真实SHA256"}],
  "frontier": {"path": "实际生成池绝对路径", "sha256": "真实SHA256"}
}
```

完成的 RESULT 使用 `status=completed`，`stage_evidence` 仍为 `{finding, verification}`；finding 额外绑定本次 `generation`，其 `question` 是 CASE ID。独立 verification 按原阶段接口计算各项判据。`failed/unknown` 必须有 `failure` 说明和原始证据，不得附通过的阶段结果；有已生成的产物仍保留原文件。缺失配对不能关轮，需继续收取，或按实际情况登记 failed/unknown；不通过重写 RESULT 消除失败。

## 5. 比较、成本与回退

父本与每个通过提案复核的候选都在全部冻结 CASE 上完成一次任务和结果登记，再统一比较：原来通过的**每个案例中的每项判据**不得退化；双方总成本必须完整，任何一次不得超出冻结预算；候选总成本不得增加，并且至少有一项判据新增通过或总成本严格下降。两候选互有优劣时保留父本；等价候选按内容哈希稳定选取，不依赖注册／完成顺序。失败、未知、被拒绝版本和预测命中／落空均保留。

每次 RESULT 的 `cost_receipt` 沿用五阶段合同（生成、执行、核验、维护、沟通），`kind=harness_attempt`，`subject=TASK引用`。事件引用真实计时原件及字段；零支出阶段明确 `[]`，未测阶段 `null`，没有整个回执也保持未知。不得在旧版和候选之间复用同一计时事件。

反思、提案、程序维护等轮次公共开销另用同一成本合同，`kind=harness_evolution`、`subject=CONTRACT引用`，通过 `review.evolution_cost_receipt` 传入；不要把同一开销再次分摊到任务中。报告分别保留单次比较、`evolution_cost` 和 `whole_round_cost`。公共开销未测时整轮总成本未知，即使任务阶段有优势也不能宣称整体节省。

`synthetic` 只给夹具结果，不准入真实部署；`development` 有优势只得到 `eligible_for_bounded_harness_pilot`。`prospective_pilot` 锁定开发轮胜出候选、模型、选择器、成本与预算，使用新问题及新证据；重命名／重排同一输入不算新样本，既有评价判据不能放宽。通过仅记 `supported_in_bounded_pilot`，失败或未知回退父本。所有报告保留 `efficiency_improvement_claimed=false` 和 `economic_promotion=false`：局部生成效果不等于完整研究效率或独立经济改善。

## 6. 进入原执行链，再反馈

已获资格的 REVIEW 可准备**一次**新池：`prepare` 请求增加 `purpose=deployment` 与 `review` 引用，`bundle` 必须等于报告的 `next_harness`，CASE 必须使用未参与本轮评价的新输入。当前 Agent 执行该 TASK 并登记 `generation`。不能拿评价时的池直接冒充新试点。

在下一合法未冻结边界，按原流程使用同版本调度 ENGINE 产生的 cycle 报告，保留原 scheduler policy 与选择器，BOUNDARY 增加：

```json
{
  "harness_deployment": {
    "contract": {"path": "本轮CONTRACT绝对路径", "sha256": "真实SHA256"},
    "review": {"path": "本轮REVIEW绝对路径", "sha256": "真实SHA256"},
    "generation": {"path": "新池GENERATION绝对路径", "sha256": "真实SHA256"}
  }
}
```

BOUNDARY 的 `frontier_pilot.initial_frontier` 必须精确引用该 GENERATION 的池，成本合同与 harness 一致，其余冻结字段沿原 `start-batch`。这要求 cycle 报告保留原 policy，不能同时试新调度规则。已有旧版 cycle／ENGINE 继续按原合同完成；新接入使用新边界，不改旧回执中的预期 SHA。

之后仍运行原 `next → observe`，D/O 自动携带 harness 版本、任务、生成记录和回退父本。批内禁止临时换池。原 `review-pilot` 关闭后给出 `next_harness` 回退指向，原 `export` 继续保留开发诊断资格；执行批次的表现不会自动被当作方法因果优势。

下一轮 `harness-init` 绑定该 PILOT_REVIEW 为 `execution_feedback`，其 D/O 及原证据进入新 `evidence`；程序把真实执行反馈送入下一份 REFLECTION_TASK。仅比较生成阶段的轮次则用 `previous_review` 接回预测、失败及退化信息。Agent 据此提出新改动，或者明确保留原版；形成“证据改变方法、方法改变实际输入、结果再改变判断”的闭环。

## 7. 产物与验收口径

每轮最多两个候选、12 个配对问题及一次部署任务；SOURCE、SPEC、模型记录和外部计时沿原引用复用。CONTRACT 保存产物上限／估计字节，REVIEW 的 `artifact_storage` 只累计本轮自有评价文件的实际逻辑字节和文件数，明确不含报告自身、后续部署及共享外部原件。冻结文件拒绝覆盖。中断留下的部分目录不等于已完成，按原任务身份检查，保留失败，不重新提交研究。

工程验收覆盖真实任务组装、原执行链接线、反馈回流、配对比较、独立核验、缺失成本、冻结和回退。临时夹具不能计作真实 Agent 使用或研究效果；具体证据见 [本次验收目录](../analysis/research_method_supervision_20260922/harness_loop_20261015/)。当前仍需下一合法批次的实际 Agent 输入／输出、独立语义复核和完整成本，才能做前瞻方法比较；策略的独立经济确认继续走原协议。


## 2026-10-17：补齐观测，保留硬判据

按证据需要使用[可选诊断工具](harness/可选诊断工具.md)：01 的有界摘要、04 的当场计时、05 的开发期分段检查，以及 06／H3 的原结果归类与已关闭批次对照。工具先明确来源、完整性和未知，再服务于“原判断→关键证据→判断更新→下一动作”，不新增必须逐轮执行的仪式。

H3 的原成功判据不得退化；失败或未知检查不能被其他案例胜出抵消。先检查每项已知成本及下界是否超预算，再检查完整成本；缺项不能掩盖已知违规。开发比较和前瞻试点都不能把未知成本当成功。程序不接受未实施的 decision_rule 放宽项。

分段诊断不改各线目标、不代表滚动重训；线上统计仅描述已核闭合记录，重复批次不充样本，账户负结果即使分段通过也不算成功。自动采用仍留在原冻结试点边界。共同工程版本登记和各线候选方法的实际采用分开，未执行的工具不标为已使用或已提效。


## 2026-10-17：重算、历史对照与删减提示（1.3.0）

新冻结的调度 ENGINE 从原 cycle／BOUNDARY 重建 BATCH，从当时可见的结果与 CONTEXT 重建每项 D，从原输入重建每项 O；再逐字段核对。改选择、预算、裁决或成本后重算引用哈希仍会被拒绝；决策编号缺口、孤立结果、结果未回传便追加决策、前后时序冲突也拒绝。原件引用仍须校验，旧冻结件不改。工程检查防止派生记录与绑定原件不一致，不能替代原件真实性、真实调用或独立核验。

新 REVIEW 的 `case_matrix` 保存每个案例、每个版本的完成／失败／未知状态、通过和失败判据，以及原 RESULT 引用。下一轮绑定 `previous_review` 后，REFLECTION_TASK 增加：

- `lineage`：沿原引用读取最近最多 8 轮，保留每项候选、提案、独立复核、预测命中／落空、拒绝和未知；更早内容按最早保留合同引用追溯，不另抄历史库。
- `contrast`：上一轮同一案例中，一版通过、另一版失败的原结果配对，并列当前父版反复失败的检查。每组最多展示 5 个案例；两种方向都保留，帮助定位缺陷与保护成功行为。旧报告没有矩阵时保持未知，不补写旧 REVIEW。
- `prune_hints`：仅当最近连续 3 轮对同一组件都有已完成的比较，且这些比较均无合格收益时，提示考虑删减／缩短的单组件候选。未知和复核拒绝不能充作完成比较；提示保留来源，不执行删除。

Agent 先打开相关提案、复核和同案例原结果，沿“原判断→关键差异→更新诊断→下一项最小验证”推进；实际送入本轮任务的历史引用可用于提案证据，未送入的材料须先在新计划明确绑定。避免只抄失败标签或重复已否定的修改。`incumbent_size / size_change` 仅是提示词、问题字符和摘录预算的体积代理，不是实际 token、计时或节省；更短的版本仍须按原 H2／H3 完整验证，不能自动采用。

保留本地严格门槛：成功行为逐案例逐判据不退化，失败／未知不能用其他胜出抵消，已知成本超限先拒绝，完整成本缺失不得取得优势。`decision_rule` 与 `decision_rule_resolved` 在计划、合同和直接比较入口均不受支持，不能通过插入缓存字段放宽判据。闭合批次去重与原件重验、曲线完整日历、账户评分与分段来源绑定继续有效。各线目标及原评价标准不变；1.3.0 登记与真实采用、研究提效分别记账。
