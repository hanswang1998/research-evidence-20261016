# Claude 项目入口

执行 [AGENTS.md](AGENTS.md) 的分线导航、授权和并发规则；本文件不另存规则副本。只加载当前工作对象与动作需要的正文。

主策略只读查询为 `python -X utf8 -B engine/research_control.py status --line main_strategy`；ML2、专题与高涨幅使用各自入口，不因此继承主策略状态。旧记忆中的等待状态版本静止、等待 Rust 全部验收或跨线继承暂停均按 AGENTS 的现行边界处理。
