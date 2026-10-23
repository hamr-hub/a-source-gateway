# CLAUDE.md

本仓库的完整智能体操作手册在 **[`AGENTS.md`](AGENTS.md)**。请先完整阅读它。

Claude Code 用户特别提示:

- 加数据源的标准流程是 `AGENTS.md` §2 的九步:`recon → 补语义 → scaffold → aliases → validate → verify_source → 部署 → demo → check`。
- 交付物必须包含 `scripts/verify_source.py` 与 `scripts/demo.py` 产出的**真实请求输出**,
  不能只声称"应该可用"。
- 加源只改 `data/` 下的数据文件,**按 `国别` 分流**:海外源 → `source_inventory.csv`;
  国内源(`国别` = 中国)→ `source_inventory_cn.csv`;端点定义 → `endpoints.json`;
  别名 → `aliases.json`。同一个源只出现在一份清单里,不改 `src/` 下的 Python 代码。
- 合法性优先:只接入官方公开、允许程序化访问的数据源;不绕限速、不加代理池。
- 不要编造字段值 —— 每个字段都要能追溯到上游文档、真实请求,或维护者的明确说明。

仓库语言约定:文档与注释用中文,代码标识符/路径/命令保持原生形态。
