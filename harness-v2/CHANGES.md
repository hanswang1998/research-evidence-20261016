# Harness v2 修改说明（2026-10-16）

## 一句话

v1 的自我改进循环在结构上不可能放行任何改动：1,277 次方法比较、2,554 个候选、换版 0 次。v2 修好了评估环节，让“改得好不好”能被测出来；同时把晋级依据从“同一开发窗口上再次领先”换成开发期滚动验证，并把每批 25 分钟左右的读写开销压下来。

## 这个目录怎么用

`harness-v2/` 按项目原路径放文件，可直接覆盖到项目根目录：

| 路径 | 状态 |
|---|---|
| `engine/research_scheduler.py` | 修改 |
| `engine/research_harness.py` | 修改 |
| `engine/research_harness_default.json` | 修改 |
| `engine/research_outcomes.py` | 新增 |
| `engine/research_timing.py` | 新增 |
| `engine/research_walkforward.py` | 新增 |
| `engine/research_line_digest.py` | 新增 |
| `engine/research_online_ab.py` | 新增 |
| `config/line_digest/ml3.json` | 新增（示例配置） |
| `tests/test_harness_v2.py` | 新增（32 个测试） |
| `docs/Harness反思与改进.md`、`docs/研究调度提示词.md`、`docs/研究调度运行手册.md`、`docs/当前研究目标.md`、`docs/研究验收标准.md`、`docs/harness/目标与评价.md` | 修改 |
| `docs/harness/v2运行说明.md` | 新增 |
| 其余 `docs/`、`engine/research_guidance.py`、`tests/test_research_harness.py` | 未改，仅作对照 |

分支第一个提交是原版文件，第二个提交是全部修改，`git diff HEAD~1` 可以逐行看改了什么。

本地落地顺序：

1. 覆盖文件后先跑 `python -X utf8 -B -m unittest discover -s tests -p "test_harness_v2.py" -v`（这边 32 个全过）。
2. 再跑原有全套测试。原套件依赖 `tests/test_research_frontier.py`，它不在证据包里，所以我这边**没能运行**。预计会有一批用例需要按 v2 语义更新，见文末“已知限制”第 1 条。
3. 一切按你们的规则在**下一合法未冻结边界**生效：在途批次、旧 ENGINE、旧 REPORT 和旧哈希都不动。

## 证据摘要（全部来自 2026-10-16 证据包）

| 事实 | 数字 |
|---|---|
| 方法比较报告 | 1,277 份，换版 0 次；全部至少一个成本字段为空 |
| RRSI 候选 | 2,554 个；被拒的 2,330 个中 2,227 个（95.6%）带“回放走到未记录路径”，全部带“没有严格优势” |
| 候选实际改动 | 只有 repair／follow／explore 三类动作的先后顺序与最大深度 |
| 台账阶段计时 | 4,137 行中只有 139 行有计算结束时间 |
| ML3 三个案例 | 每批 33—48 分钟，工具调用 43—118 次，其中 64—76% 是读文件或搜索，读回 34—65 万字符；账户作业约 1.5 分钟 |
| 开发窗口上的账户 | 结构线 396 个，年化中位 275%、最高 336%；ML3 223 个，中位 299%，基线 E11J 316% |
| 唯一样本外 | ML2 2026-01-05—09-30 原生：E1002J 年化 57.6%（开发期 306%），H543 91.2%（开发期 294%） |
| 2026 接触 | 结构线 131 个、旧 ML 83 个账户在 2026-10-04 前用了 2020-01-02—2026-07-24 窗口；2026-10-14 一次误读 2026 公司行动记录 |
| 审查模型 | ML3 12 个审查会话都是 gpt-6.1-sol，与生成者相同 |
| 结果字段写法 | 谨慎线 370 种、结构线 262 种、数学线 122 种 |

---

## 逐项修改

### 1. 回放里“没记录的选择”改为悲观计分（`research_scheduler.py`：`replay`、`rrsi_compare`、`dominates`）

**改了什么。** 候选规则在回放中选到历史上没跑过的分支时，v1 把整个候选判为不可比较（`unknown_quality_or_unobserved_path`），同时把成本记为未知。v2 改为：这次选择不揭示任何结果、不增加已回答问题，但计一次请求（`requests = 揭示节点 + 未记录选择`），并按该世界已观测节点中的最高成本计费。一条回放里这类选择超过一半时，记 `insufficient_observed_coverage`，仍不可比较。

**为什么。** 你们的真实研究一次只派一个决策，所以记录下来的历史基本是一条直线；换任何一种规则，都会马上走到没记录的节点。这正是 95.6% 的被拒原因：比较本身根本没机会发生。你们参照的 Dream-RSI 原文对这种情况的处理是“不揭示、不计分”，比较照常进行；v1 的实现比原文严格得多。v2 在原文基础上更保守：没记录的选择不可能带来质量，也不可能显得更省（按最高成本计费、计入请求数）。

**验证。** `test_pessimistic_cost_charge_in_replay`、`test_unrecorded_choices_*`、`test_mostly_unrecorded_replay_stays_incomplete`。用 v2 规则重判证据包里已存的 2,330 个候选指标：可比较的从 0 个变为 634 个，其中 206 个在开发集上严格占优。**这 206 个全部是“同样质量、请求更少”的动作顺序调整，没有一个提高了质量。** 也就是说，修好评估后，调度层这套动作级规则语言的杠杆仍然很小，真正的杠杆在第 6—14 项。

### 2. 成本未知不再阻止试点资格（`rrsi_compare`、`comparable_cost`）

**改了什么。** v1 只要有一方成本未知就记 `unknown_total_cost` 并拒绝。v2：双方都测得 `total_seconds` 就用它；否则双方都测得 `measured_seconds` 就用它；都没有则在 `notes` 记 `cost_unknown_not_blocking`，不拒绝，但成本也不能算作优势。已知成本上升仍然拒绝。

**为什么。** 1,277 份报告全部有成本字段为空，所以“成本必须完整”等于永远不放行。成本是否节省应该在真实试点里测，而试点本来就要求冻结成本上限、缺项或超限即回退（`review_pilot` 原逻辑未改）。所以 v2 把“缺成本”的后果从“不许试”改成了“不许宣称节省”。第 11 项的计时工具负责让试点真的测到成本。

**验证。** `test_unknown_cost_is_flagged_not_blocking`、`test_known_cost_increase_still_rejects`、`test_measured_seconds_used_when_total_is_missing`。

### 3. 试点资格只要求保留世界干净（`cycle`）

**改了什么。** v1 要求**所有**世界（包括开发世界）都是 `prospective` 且没有任何限制，保留集还要严格占优。v2 改为：开发世界上严格占优，且至少有一个干净的保留世界（`prospective`、无限制）不退化。

**为什么。** 只要有一次人工覆盖决策，导出的世界就带 `policy override recorded` 限制，于是 v1 的资格条件几乎不可满足。开发世界只用来生成和排序候选，真正需要干净的是没参与生成的保留世界。保留集上要求“不退化”而不是再次“严格占优”，因为严格优势已经在开发集上要求过，最后还有真实试点把关。

**验证。** `test_pilot_offer_is_reachable_with_diagnostic_development_worlds`：同一个场景（开发世界带覆盖限制、父本回放有一次未记录选择、候选少用一次请求），**原版调度器给出 `retain_incumbent`，v2 给出 `eligible_for_bounded_pilot`**。两个版本都跑过。

### 4. 同一决策点可以开兄弟分支（`start_batch`、`next_decision`、`observe`、`ledger_all`）

**改了什么。** 边界可冻结 `branching: {siblings: 1—3}`。`next` 一次写出 k 个决策件，带 `sibling` 组号；非 frontier 模式下共用同一锚点和动作，frontier 模式下取排序前 k 个不同假设。全部回填前 `next` 返回 `pending_result` 和整组列表；`observe` 按 `decision_id` 接受组内任一决策，结果可乱序回填，节点号跟随决策号（D0005→n0005）。k 受 `max_attempts`、试点 `max_requests`、frontier `max_decisions` 共同约束。k=1 时和 v1 完全一致。

**为什么。** 第 1 项只能让比较“发生”；比较要有意义，历史记录就得是树而不是一条线。账户计算本身只要约 1.5 分钟，在同一状态下并行跑 2—3 个不同机制的成本很低。Dream-RSI 的真实探索本身就是多工作区并行的，这是它的回放能比较策略的前提，而 v1 缺了这个前提。

**验证。** `test_siblings_build_a_tree_that_replay_can_branch_on`（开 2 个兄弟、乱序回填、导出世界有两个根子节点、回放能走到第二个兄弟）、`test_single_decision_flow_is_unchanged`、`test_branching_bounds`。

### 5. 开发期滚动验证（新增 `research_walkforward.py`；`start_batch`、`observe` 接入）

**改了什么。** 新增滚动验证打分器。默认合同：完整公开可见窗口 2020—2022；2023、2024、2025 各一个验证折；至少 2 折年化胜过父本、平均差不小于 0、每折原始回撤不超过 20%。面向 Agent 的输出只有可见窗口的完整数字，以及每折胜负、回撤是否合格和差值区间；完整数字用 `--detail-out` 写到 Agent 不读的位置。2026 起的数据行一律不读。各折之间、各折与可见窗口之间不得重叠，否则相减就能还原被遮蔽的数字（我第一版就有这个漏洞，已改掉）。边界冻结 `walkforward_contract` 后，`observe` 只在“账户评分改善且滚动验证通过”时接受 `valid_improvement`，否则必须登记为 `valid_negative`。

**为什么。** 约 1,100 个账户候选都在同一个 2020—2025 窗口上反复比较。结构线中位年化 275%、ML3 中位 299%，ML3 还在要求比 316% 的基线再高 5 个百分点，10-15 到 10-16 一天多跑了 60 来批全部失败。唯一样本外的 ML2 账户，开发期 306% 的策略在 2026 年 1—9 月只有 57.6%；而且迭代更多的 E1002J 比更早的 H543 少赚，累计收益差 20.9 个百分点。在产生父本的同一窗口上继续追高，主要是在挑噪声（RRSI、Rethinking Harness Evolution 都报告了进化集过拟合）。遮蔽验证数字，等于把验证段变成只给结论的“私有榜单”，能减缓新候选继续过拟合。

**边界说明。** 2023—2025 已被反复看过，现有父本在这些年份通过只是弱证据；这仍是开发证据，不是独立确认，与你们验收标准里“已暴露数据不能再称独立”一致。遮蔽靠约定，不是技术隔离。

**验证。** `test_validation_folds_decide_and_are_masked`（可见窗口更好、验证 1/3 折胜出 → fail；遮蔽版里没有任何验证数字；2026 行被忽略）、`test_overlapping_or_sealed_folds_are_rejected`、`test_development_gain_without_walkforward_pass_is_not_an_improvement`。

### 6. H3 配对比较改成统计判定（`research_harness.py`：`compare_attempts`、`decision_rule`、`review_round`）

**改了什么。** v1 规则是：任何案例的任何判据从通过变成不通过就拒绝；成本必须完整；有一次失败或未知就整轮不可比较。v2 规则 `paired_sign_wilson_v1`：
- 每个案例比较双方通过的判据个数，多为胜、少为负、相等为平；
- 候选失败或未知记负，父本失败的案例不配对（未配对超过 20% 不能关轮）；
- 胜率的 Wilson 下界（默认 0.9 置信）须大于 1/2，16 个分出胜负的案例至少要 12 胜；
- 只有事前声明为 `critical_checks` 的判据保留一票否决；
- 已知成本上升仍拒绝，无负案例且成本下降也算优势，成本未知不阻止试点资格；
- 多个候选同时通过时，按 Wilson 下界、已知成本、内容哈希依次挑选。

**为什么。** 每个案例只生成一次，LLM 输出有随机性。“任一判据退化即拒”会因为噪声拒掉真实改进，“至少一项新增通过”又会因为噪声放过假改进。RHI 论文的作者自己承认，只和上一版比较是很嘈杂的信号；RRSI 用的是 120 个进化任务加 40 个留出任务。

**验证。** `test_one_noisy_regression_no_longer_vetoes_a_clear_gain`（13 胜 1 负 2 平：v1 会拒，v2 接受）、`test_small_or_balanced_samples_do_not_pass`、`test_critical_check_is_a_hard_stop`、`test_failed_candidate_attempt_counts_as_loss`（11 胜 5 失败不通过）、`test_known_cost_saving_without_losses_is_a_gain`、`test_wilson_lower_bound_thresholds`。

### 7. 候选上限 2→4，案例下限：开发 16、试点 8（`initialize`、`decision_rule`）

**为什么。** 你说得对，任务不贵。名额不再是瓶颈，统计功效才是：少于 16 个案例时，Wilson 规则几乎不可能通过。这个下限对真实轮次不能调低，只有合成夹具可以用 1 个案例。代价是每轮要多做生成；Rethinking Harness Evolution 的提醒是，这类搜索本身也要算进成本，所以第 14 项用在线交替对照，和“直接多跑几轮”做对比。

### 8. 候选版本包的泄漏扫描（`bundle_leakage`、`proposal_record`）

**改了什么。** 候选版本包里**新增**的提示词或生成问题，只要出现证券代码（600519、sz000001、600519.XSHG 等）或具体日期（2024-02-05、20240205、2024年2月），提案就记 `accepted=false`，并在 `leakage` 字段列出位置，不进入评价。年份本身（如“2023 年验证段”）和普通数字（如“300000 字符”）不算。

**为什么。** harness 改动应该改的是方法，而不是把某批任务的答案写进下一版指令。RRSI 的 leakage critic 正是拦这一类改动；在没加这道正则化时，它的进化集分数是 92.8，分布外只有 40.3，和基线 39.7 几乎一样。证据包也显示，出题 Agent 能看到股票代码、日期和事件，B86 甚至直接讨论了事后挑出的三个事件。

**验证。** `test_codes_and_dates_in_a_new_prompt_are_flagged`、`test_plain_numbers_and_years_are_not_flagged`、`test_default_bundle_is_leak_free`。

### 9. 独立复核必须换模型家族（`proposal_record`，`research_harness_critic.v2`）

**改了什么。** 开发和试点轮次的复核文件必须是 v2，写明 `verifier_model`，且它和生成 Agent 不能是同一模型家族：gpt-6.1-sol 和 gpt-6-astra 同属一家，claude-* 或 deepseek-* 算不同家。合成夹具仍可用 v1。

**为什么。** ML3 的 12 个审查会话全部是生成者自己用的 gpt-6.1-sol，B86 也写明审稿人不是独立作者。同一个模型换个角色，盲点是一样的。你们 ML2 已经在用 Claude，DeepSeek 也有现成的调用记录。

**验证。** `test_model_families`；完整的提案流程（含 v2 复核文件）需要在本地全套测试里补用例。

### 10. 默认版本包（`research_harness_default.json`）和固定约束（`GUARD`）

在不改结构的前提下，调整提示词和生成问题：
- 反思先读状态摘要；
- 说明结果在验证段上支持什么；
- 连续在同一父本附近微调时，把“过拟合”列为必须正面回答的竞争解释；
- 候选要写验证段上的预期表现，不得写具体证券、日期或事后挑出的事件，先查已关闭表达清单；
- 生成问题从 4 个增加到 6 个。

GUARD 相应增加了三行固定约束。理由同第 5、8、12 项。

### 11. 计时工具（新增 `research_timing.py`）

**改了什么。** 计时在工作发生的当下记录，不再事后手填：
- `run` 包裹执行器命令，原样返回退出码；
- `start`／`stop` 给 Agent 的生成、复核、沟通等非子进程工作记时；
- `receipt` 直接组装调度器要的五阶段 `research_cost_receipt.v1`。没计时的阶段写 `null`，确认没有活动才写 `[]`；同一资源上时间重叠的计时拒绝组装，防止重复计费。计时文件写一次就不可覆盖。

**为什么。** 4,137 行台账里只有 139 行记了计算结束时间，而成本未知恰恰是拦住所有比较的原因之一。

**验证。** `test_run_start_stop_and_receipt_feed_the_scheduler`：生成的回执能被调度器原有的 `read_cost_receipt` 读进去。另有 `test_overlapping_timers_on_one_resource_are_rejected`。

### 12. 本线状态摘要（新增 `research_line_digest.py`、`config/line_digest/ml3.json`）

**改了什么。** 按配置从 STATE 中只取决定下一步的字段，列出全部已关闭表达，附最近几次反思；每段都带原件路径和 SHA256，并受总字符预算约束。每批先读这一份，只在需要时打开原件。

**为什么。** 每批 64—76% 的工具调用在读文件，读回 34—65 万字符，账户计算只要 1.5 分钟。ML3 的 STATE.json 有 182 个键、约 39 万字节，大部分是已关闭任务。在真实 ML3 STATE 上，摘要只有 14,632 字符，58 个已关闭表达全部列出，大约是原件的 1/23。ACE 的教训是：上下文应该按条目增量维护，不能整段重写压缩，所以摘要是从原件机械抽取的，不让模型去改写。

**验证。** `test_digest_is_bounded_and_lists_closed_expressions`，以及在证据包 ML3 STATE 上实跑。

### 13. 结果统一分类（新增 `research_outcomes.py`）

**改了什么。** 结果统一为八类：`improvement`、`negative`、`risk_limit`、`information`、`engineering_failure`、`evidence_gap`、`not_executed`、`unclassified`。新记录直接写其中一类；旧记录用规则尽量映射，判断不了的记 `unclassified`，不硬猜。信息阶段的否定结论算 `information`（问题已经回答了）；账户记录只写了 completed 的，看不出结论，记 `unclassified`。

**为什么。** 光谨慎线一条线就有 370 种写法，命中率、未出结论率都没法算，自然也没法比较两个版本谁好。

**验证。** 在 4,137 行台账上实跑，`unclassified` 为 3.8%；对应测试是 `test_canonical_mapping`、`test_summary_shares`。

### 14. 在线交替对照（新增 `research_online_ab.py`）

**改了什么。** 新旧调度规则或新旧 harness 在相邻批次交替使用，每组至少积累 30 个观察，再比较两项指标：
- 主指标：滚动验证通过的改进率（观察带滚动验证结论时）或改进率；
- 次指标：未出结论率。

区间用 Newcombe 法，结论分四种：`prefer_candidate`、`prefer_incumbent`、`no_difference`、`insufficient_data`。

**为什么。** 离线回放只能评估历史上走过的路；你们的任务便宜，直接在真实批次上交替对比更可靠。这也是 Rethinking Harness Evolution 要求的“和同等预算的简单做法比”。次指标的判法做过调整：第一版要求未出结论率差值的置信上界不超过 5%，可两组都是 0 次失败、各 40 个样本时，上界也有 6.3%，等于永远过不了；现在改成“点估计不超过 5% 且没有检测到显著上升”。

**验证。** `test_decisions`。

### 15. 文档

- `docs/Harness反思与改进.md`：开头加 v2 说明；更新候选上限、案例数、`decision_rule` 字段、复核 v2 和泄漏检查；配对比较规则整段重写。
- `docs/研究调度提示词.md`：开头加 v2 说明；更新第 1.1 节第 5 条（兄弟分支）、第 2 节（悲观计分）、第 3 节第 4 条（试点资格）；新增第 9 节（滚动验证、交替对照、计时、状态摘要、统一分类）。
- `docs/研究调度运行手册.md`：开头加 v2 说明；更新第 3 节（兄弟分支接口）、第 8 节（准入与成本语义，附重判结果）；新增第 13 节（新命令）。
- `docs/当前研究目标.md`、`docs/harness/目标与评价.md`：晋级依据改为滚动验证；披露 2026 的接触情况。
- `docs/研究验收标准.md`：新增“开发期滚动验证”；新增本地与原生偏差抽检（每 10 个本地候选至少抽 1 个原生对照）；补充容量与涨停成交的敏感性要求。
- `docs/harness/v2运行说明.md`：新工具的用法。
- 两份原本是 Windows 换行（CRLF）的文档，保持原换行格式。

---

## 需要你确认的三件事

1. **ML3 的线内目标。** 我在 `当前研究目标.md` 里把“在同一窗口上比 E11J 再高 5 个百分点”这类目标改成了“滚动验证通过且不低于父本”，原文本保留为历史合同。改线内目标属于你的授权范围，你不同意的话，删掉那一段即可。
2. **复核换模型家族会增加调用成本。** 每次 harness 复核要多调一次 Claude 或 DeepSeek。
3. **结构线和旧 ML 的 2026 不完全干净。** 将来对这两条线做 2026 确认时，需要在结论里披露这一点。

## 没有改的

- 经济评分器 `review-account`、账户内核、交易表达、执行授权、聚宽提交流程。
- `local_replay/src/broker.rs` 和 `matching.py`：容量用全天成交量、涨停只按价格条件成交，这两点只在验收标准里要求做敏感性检查，代码没动。
- 旧冻结件、旧 REPORT、旧哈希、各线的暂停状态。

## 已知限制

1. **原有测试套件没有跑。** `tests/test_research_harness.py` 依赖证据包里没有的 `test_research_frontier.py`。预计需要更新的用例：
   - 用 `kind=development` 加 1 个案例的计划，v2 要求至少 16 个案例，或者改用 `kind=synthetic`；
   - 真实轮次的复核文件要换成 v2，并带一个和 `synthetic-test-model` 不同家族的 `verifier_model`；
   - 断言“任一判据退化即拒”“成本未知即拒”“单未决策”的用例。
2. **悲观计费是近似。** 用已观测节点的最高成本代替真实成本，宁可高估。第 1 项的重判没法用这个计费（已存指标里没有逐节点成本），只能把成本当未知，所以 634/206 是保守估计。
3. **还没有干净的保留世界。** 试点资格需要至少一个 `admission=prospective`、没有限制的保留世界，而现有导出世界大多带限制。建议每三个批次留一个不做人工覆盖、按 `--usage heldout` 导出。
4. **遮蔽靠约定。** Agent 有文件读取权限，技术上能打开完整数字，需要在记录里如实写明是否被打开。
5. **滚动验证的输入。** 打分器需要候选和父本的逐日权益序列，各线的 RESULT 格式不同，需要本地 agent 接一层适配（打分器支持 JSON 或 CSV）。

## 对应的前沿工作

| 改动 | 依据 |
|---|---|
| 1、4 | Dream-RSI（arXiv 2609.14858）：回放只揭示记录过的结果、未记录的选择不计分；真实探索是多工作区并行的 |
| 5、7、8 | RRSI（arXiv 2609.24972）：进化集过拟合，leakage critic 和 pruner 提升分布外表现；Rethinking the Evaluation of Harness Evolution（arXiv 2607.12227）：进化集和评估集共用会过拟合，要和同预算基线比 |
| 6 | Recursive Harness Self-Improvement（arXiv 2607.15524）：只和上一版成对比较是很嘈杂的信号 |
| 9 | Darwin Gödel Machine（arXiv 2505.22954）：同一系统自评时出现过刷分（删掉幻觉检测标记） |
| 12 | Meta-Harness（arXiv 2603.28052）：提案者要能看到原始轨迹；ACE（arXiv 2510.04618）：上下文按条目增量维护，避免整段重写导致塌缩 |
| 5 | Profit Mirage（arXiv 2510.07920）：LLM 交易 agent 的回测收益在模型知识截止日之后大幅缩水 |
