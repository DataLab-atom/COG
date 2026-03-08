# COG — Code Generation Pipeline

从用户需求描述到完整 Python 项目的全自动代码生成流水线。

---

## 概览

COG 分两条主图顺序执行：

```
用户需求（requirement）
        │
        ▼
  arch_build（架构构建）
        │  输出：架构树（tree）
        ▼
  arch_to_project（代码生成）
        │  输出：Python 项目文件
        ▼
    磁盘上的完整项目
```

---

## 主图一：arch_build

**输入**：`requirement: str`
**输出**：`tree: list[dict]`（架构树）、`ok`、`errors`、`warnings`

```
module_split          arch_module_splitter   将需求划分为模块（文件夹）列表
      │
validate_module_imports                      检测模块导入环（失败则重试 module_split，最多 3 次）
      │
file_split_raw  [map]  arch_file_splitter   为每个模块并发生成文件列表
      │
file_backfill          arch_backfill_files  回填 module.files，汇总全量文件列表
      │
validate_file_imports                        检测文件导入环（失败则重试 file_split_raw，最多 3 次）
      │
leaf_define_raw [map]  arch_leaf_definer    为每个文件并发定义 type:: / fn:: id 列表
      │
leaf_backfill          arch_backfill_leaves 回填 file.types / file.functions，汇总全量节点
      │
internal_fanout        arch_internal_fanout 两阶段全量并发：定义所有 type 和 fn 的详细结构
      │
resolve_needs          arch_resolve_needs   将 fn.needs 中的未知辅助函数注入为 stub
      │
backfill ──────────── arch_backfill         推导 file.imports.names / module.exports / module.dependencies / entrypoint
validate_calls_dag ── arch_validate_dag     检测 fn.calls 环（保证调用图为 DAG）
      │
static_check           arch_static_check   全量静态检查（9 项）
```

### internal_fanout 并发设计

两阶段执行，全程 `asyncio.gather` + `asyncio.Lock` 保护共享状态：

- **Phase 1**：并发定义所有 `type::`，同时建立 `init_fn → overload fn` 的 `asyncio.Event` 依赖关系
- **Phase 2**：并发定义所有 `fn::`
  - `init_fn`：无等待，立即执行
  - 类的 overload 方法：等待对应 `init_fn` 的 Event（确保 init 先定义）
  - 独立函数：无等待，立即执行

---

## 主图二：arch_to_project

**输入**：`tree: list[dict]`、`output_dir: str`
**输出**：`ok`、`written`（写入文件列表）、`syntax_errors`

```
scaffold          arch_scaffold           静态生成目录结构和代码骨架
                                          函数体位置用 {{body:<fn_id>}} 占位
      │
fn_codegen        arch_fn_codegen_fanout  全量并发生成所有函数体
      │
assemble          arch_assemble           将函数体填入骨架并写入磁盘
      │
syntax_check      arch_syntax_check       py_compile 语法检查
```

### fn_codegen 并发设计

每个 fn 一个 `asyncio.Event`，三步依赖等待：

1. **calls 依赖**：fn A calls fn B → A 等待 B 的 Event，生成时以 B 的实现为上下文
2. **类方法依赖**：类方法（非 init）等待同类 `init_fn` 的 Event（以 init 实现为上下文）
3. **死锁防护**：若 `init_fn.calls` 包含本 fn，则跳过步骤 2 的等待，避免循环等待死锁

失败时 fallback 为 `pass # generation failed`，并强制 set Event 解除下游等待。

---

## 架构树节点格式

| kind | 关键字段 |
|---|---|
| `module` | `id (mod::*)`, `root`, `files`, `imports`, `exports`, `dependencies` |
| `file` | `id (file::*)`, `root`, `types`, `functions`, `imports`, `constants` |
| `type` | `id (type::*)`, `fields`, `overloads`, `init_fn`, `base_class`, `constants` |
| `function` | `id (fn::*)`, `params`, `returns`, `calls`, `needs`, `is_async`, `description` |
| `entrypoint` | `fn (fn::main)` |

### root 字段语义

- `module.root=true`：`__init__.py` 放在 `output_dir/` 根，不创建子目录
- `file.root=true`：`.py` 文件放在 `output_dir/` 根，import 路径仅用文件名（无模块前缀）

---

## 目录结构

```
COG/
├── graphs/
│   ├── arch_build.json          主图一：需求 → 架构树
│   └── arch_to_project.json     主图二：架构树 → Python 项目
│
├── arch/                        所有图步骤的 Python 实现
│   ├── internal_fanout.py       两阶段并发定义 type / fn
│   ├── fn_codegen_fanout.py     调用图依赖并发生成函数体
│   ├── scaffold.py              静态生成代码骨架
│   ├── assemble.py              填充占位符，写入磁盘
│   ├── resolve_needs.py         needs → stub fn 注入
│   ├── backfill.py              推导 imports / exports / dependencies / entrypoint
│   ├── backfill_files.py        module.files 回填
│   ├── backfill_leaves.py       file.types / file.functions 回填 + ID 冲突检测
│   ├── static_check.py          全量静态检查（9 项）
│   ├── validate_dag.py          有向图环检测（DFS）
│   └── syntax_check.py          py_compile 语法验证
│
├── configs/
│   ├── arch_module_splitter.json
│   ├── arch_file_splitter.json
│   ├── arch_leaf_definer.json
│   ├── arch_internal_definer.json  target_id 单项调用，enable_history=false
│   └── arch_codegen.json           含 calls_bodies_json 上下文
│
└── prompts/
    ├── arch_module_split/
    ├── arch_file_split/
    ├── arch_internal_define/   target_id 驱动，无 depth/history_defined
    └── arch_codegen/           含 calls_bodies_json / init_body 上下文
```

---

## 静态检查项（arch_static_check）

1. 所有 id 引用可解析（imports.from、overload.fn、param.type、fn.calls）
2. module.imports 无环
3. file.imports 无环
4. fn.calls 无环
5. overloads.args 与对应 fn params 名称一致
6. fn.needs 全部清空（arch_resolve_needs 已处理）
7. entrypoint 存在且 fn::main 可达
8. module.exports 合法（只能包含本模块旗下 file 中的 id）
9. 死函数检测（warning：从 entrypoint 不可达的 fn）

---

## needs 机制

LLM 在定义函数时可声明尚不存在的辅助函数：

```json
"needs": [
  {
    "description": "将张量归一化到 [0,1]",
    "location": "file::utils/tensor_utils",
    "params": [{"name": "x", "type": "torch.Tensor"}],
    "returns": {"type": "torch.Tensor"},
    "depth": 1
  }
]
```

`arch_resolve_needs` 将其转换为真实的 `fn::` stub 节点，注入到目标文件，并回填 `fn.calls`。`depth > 3` 时记录错误并跳过，防止无限展开。
