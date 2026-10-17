# 研究材料证据包 · 2026-10-16

> **Harness 1.3.0 审查：** 请从 [harness-1.3.0/README.md](harness-1.3.0/README.md) 开始。该目录包含本地登记的 32 份原件、原始 ZIP 与哈希；此前的 `harness-v2/` 是上游提案目录。

本仓库保存按 P0／P1／P2 十项需求整理的研究材料。完整材料以 Release 附件提供，下载 ZIP 和同名 `.sha256` 文件后解压；从包内 `README.md`、`ANSWERS.md` 和 `DATA_DICTIONARY.md` 开始审阅。

## 下载文件

[打开下载页面](https://github.com/hanswang1998/research-evidence-20261016/releases/tag/evidence-2026-10-16) · [完整 ZIP](https://github.com/hanswang1998/research-evidence-20261016/releases/download/evidence-2026-10-16/research_evidence_20261016.zip) · [SHA256 校验文件](https://github.com/hanswang1998/research-evidence-20261016/releases/download/evidence-2026-10-16/research_evidence_20261016.zip.sha256)

| 文件 | 用途 |
| --- | --- |
| `research_evidence_20261016.zip` | 完整脱敏证据包，246,749,692 字节，47,565 个文件 |
| `research_evidence_20261016.zip.sha256` | ZIP 的 SHA256 校验值 |

ZIP SHA256：

```text
162c0b12b2a6219c192c9d723d3d4ec01692dac4c2a33ccb7ce96aba4131ed8e
```

## 内容

- 1,277 份方法比较报告原文，以及 decision、候选改动、拒绝原因、成本字段汇总。
- 4,137 条全研究线台账身份、2,826 条 D/O 执行尝试、922 条候选指标记录；台账含诊断、准备、工程和服务交付，不等于独立经济试验数。
- ML3 B86 improved、B85 有效负结果、B67 工程失败三个案例；包含案例目录全部 302 个原文件、保留记录中的可见对话与工具调用。
- 本地／聚宽原生对照、成交与容量假设、验证和封存规则、模型身份、指引读取记录、最近一周自动化回执。
- harness、所需文档、原件清单、逐文件指纹、脱敏记录、缺失说明和校验报告。

## 审阅边界

材料没有重新运行研究、回测或模型。不同原件按各自读取时刻形成快照。凭证在导出副本中脱敏，原始项目未修改；认证文件、浏览器会话库及完整应用数据库未纳入。

缺失的 ML3 comparison20／21、独立旧 ML 实际 harness 版本包、部分阶段时间与提交次数均在包内明示为缺失或未知。没有补造执行证据。完整隐藏推理、系统／开发者指令及压缩记录不在可见对话导出范围内。

验收报告通过，ZIP CRC 检查通过。B67 一份原本截断的 JSON 收据按原字节保留并列为来源格式例外。这是可审阅的证据包；大型公共行情和完整模型运行环境没有整体复制。

## harness-v2 分支

`harness-v2/` 是依据本证据包修改后的完整 harness 文件（按项目原路径存放，可直接覆盖），修改理由、证据与验证见 [harness-v2/CHANGES.md](harness-v2/CHANGES.md)。
