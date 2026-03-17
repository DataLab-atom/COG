# AI 阅读 ScienceOS 指南

## ⚠️ 强制要求：分片读取，禁止一次性读取整个项目

本项目文件较多，AI 助手在阅读项目时**必须按以下顺序分片读取**，**严禁**一次性将所有文件全部读入上下文。

### 推荐阅读顺序

**第一片：了解系统（必读）**
```
CLAUDE.md                  ← 开发规范（已在系统提示中）
README.md                  ← 系统概览、API、子系统说明
```

**第二片：入口与配置（了解运行流程）**
```
serve.py                   ← Web 服务入口，gate 逻辑
config.py                  ← Dynaconf 全局配置入口
settings.toml              ← 项目级配置（模型默认值、preset 定义）
graphs/arch_then_mcts_then_u2e_ppw.json  ← 顶层入口图
graphs/arch_then_mcts_then_u2e.json      ← 代码优化主图
```

**第三片：执行层框架（按需读取）**
```
utils/__init__.py          ← 公共 API（外部统一从此导入）
utils/_base.py             ← ToolResult 基类
utils/_graph.py            ← 计算图执行器
utils/_pipeline.py         ← 流水线执行器（步骤分发）
utils/_channel.py          ← 通道机制（gate 阻塞等待）
utils/_registry.py         ← 工具/输入源注册表
utils/_trace.py            ← 追踪/序列化（safe_serialize）
llm_config.py              ← LLM 调用核心库（含 preset 解析）
```

**第四片：业务子系统（按需读取）**
```
# 架构设计
utils/arch/scaffold.py     ← 代码脚手架
utils/arch/assemble.py     ← AST 组装
utils/arch/codegen_task_builder.py  ← 代码生成任务
graphs/arch_build.json     ← 架构设计图
graphs/arch_to_project.json ← 代码生成图

# MCTS 搜索
utils/mcts/tools.py        ← 搜索树操作
utils/mcts/inputs.py       ← 沙盒评估、gate
graphs/mcts_run.json       ← MCTS 主图

# U2E 进化
utils/u2e_tools.py         ← 进化算法工具
graphs/u2e_func_iter_patch.json  ← U2E 迭代图

# 论文写作
utils/papw_tools.py        ← 论文写作工具
utils/papw_input.py        ← 论文写作异步输入源
```

**第五片：前端（按需读取）**
```
frontend/src/types/index.ts              ← 类型定义与默认配置
frontend/src/components/phases/          ← 各阶段 UI 组件
```

### 快速定位指南

| 任务类型 | 需要读取的文件 |
|---------|--------------|
| 理解完整流程 | `README.md` → 顶层入口图 → 代码优化主图 |
| 调试架构设计 | `graphs/arch_build.json` → `utils/arch/` 相关模块 |
| 调试 MCTS | `graphs/mcts_run.json` → `utils/mcts/tools.py` |
| 调试 U2E | `graphs/u2e_func_iter_patch.json` → `utils/u2e_tools.py` |
| 调试论文写作 | `utils/papw_input.py` → `input/papw_*.json` |
| 调试 gate 阻塞 | `serve.py` → `utils/_channel.py` |
| 调试图执行 | `utils/_graph.py` → `utils/_pipeline.py` |
| 调试 LLM 调用 | `llm_config.py` → 对应 `configs/*.json` + `prompts/` |
| 修改配置/预设 | `settings.toml` → `config.py` → `llm_config.py` |
| 新增/改写 skills | `skills/` 某个示例（⚠️ 仅在此任务时读取 `skills/*.json`） |
| 新增 Agent | `CLAUDE.md` → `configs/` 某个示例 |
| 新增工具 | `utils/_base.py` → `utils/text_utils.py` → `tools/` 某个示例 |
| 前端修改 | `frontend/src/types/index.ts` → 对应 Phase 组件 |
