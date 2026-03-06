import sys
import os
import logging
import argparse
from pathlib import Path
from types import SimpleNamespace

# 1. 确保能导入你的模块
# 假设当前脚本在项目根目录，添加当前目录到系统路径
current_dir = Path(__file__).resolve().parent
sys.path.append(str(current_dir))

# 导入你的模块 (根据你的实际文件夹结构调整 import)
# 假设 TreeCoTModule 在 modules/tree_cot.py 或类似位置，请根据实际情况修改
try:
    # 尝试导入，你需要根据你实际的文件名修改这里的 import
    # 例如如果你的类定义在 `modules/tree_cot.py` 中:
    # from modules.tree_cot import TreeCoTModule
    
    # 如果就在当前目录下或者你已经整理好了包结构:
    from tree_cot.initial_cot import TreeCoTModule # <--- 请修改这里为你存放 TreeCoTModule 的文件名
except ImportError as e:
    print(f"导入错误: {e}")
    print("请确保 test_tree_cot_module.py 在项目根目录，且 import 路径正确。")
    sys.exit(1)

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("TestRunner")

def create_mock_config():
    """
    模拟 Hydra 的配置对象 (cfg)。
    这里的值对应你 yaml 文件中的配置。
    """
    # 模拟 cfg.models.tree_cot
    tree_cot_models = SimpleNamespace(
        structure_model="gpt-4.1-mini",  # 或者 'gpt-4o', 根据你的 llm_config
        doc_model="gpt-4.1-mini",
        code_model="gpt-5-mini",    # 代码生成模型
        temperature=SimpleNamespace(
            structure=0.7,
            code=0.2,
            doc=0.5
        )
    )
    
    # 模拟 cfg.models
    models = SimpleNamespace(tree_cot=tree_cot_models)
    
    # 模拟 cfg.parameters
    params = SimpleNamespace(
        tree_cot=SimpleNamespace(
            retry_times=2 # 测试时设置少一点，节省时间
        )
    )
    
    # 模拟 cfg.problems (任务相关配置)
    problems = SimpleNamespace(
        task_name="test_task_001", # 测试任务名
        metric="accuracy",
        demand="Create a simple calculator that supports add, subtract, multiply, and divide.",
        dataset_path="" # 假路径，如果不涉及真实数据读取可以随便写
    )

    # 组装由于 cfg
    cfg = SimpleNamespace(
        models=models,
        parameters=params,
        problems=problems,
        # 兼容性处理：有些代码可能通过 cfg.problem 访问
        problem=problems 
    )
    
    return cfg

def setup_test_environment(project_root: Path, task_name: str):
    """
    创建必要的输入文件，模拟上游模块的输出 (Implementation Plan)
    """
    # 1. 创建 reports 目录
    reports_dir = project_root / "problems" / task_name / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    
    # 2. 写入假的 Implementation Plan (这是 TreeCoT 的输入)
    imply_plan_path = reports_dir / "implementation_plan.md"
    
    if not imply_plan_path.exists():
        logger.info(f"创建测试用的 Implementation Plan: {imply_plan_path}")
        dummy_plan = """
# Implementation Plan for Calculator

## 1. Overview
This project implements a basic calculator using Python.

## 2. Modules
- **main.py**: Entry point of the application.
- **calculator/core.py**: Contains the Logic class for math operations.
- **calculator/utils.py**: Helper functions for input validation.

## 3. Class Design
- `Calculator`: Methods `add`, `sub`, `mul`, `div`.
        """
        imply_plan_path.write_text(dummy_plan, encoding="utf-8")
    else:
        logger.info("Implementation Plan 已存在，跳过创建。")

def run_bug_repro(cfg, project_root: Path, attempts: int = 3):
    """
    复现原始报错场景：走 chat_with_tools + memory_tools 的调用路径，
    多次请求以观察是否仍出现异常响应（如 None/缺 choices）。
    """
    try:
        from LLM_MODULE.llm import OpenAILLM
        from LLM_MODULE.tools import load_plugins, tool_spec, make_tool_handler
    except Exception as e:
        logger.error(f"加载 LLM_MODULE 依赖失败: {e}")
        raise

    active_task = prepare_memory_tools_task(cfg, project_root)

    llm = OpenAILLM(
        model=cfg.models.tree_cot.code_model,
        module_name="tree_cot",
        agent_name="initial_coder_agent",
    )
    tools = tool_spec("memory_tools")
    handler = make_tool_handler(toolset="memory_tools", enforce_policy=True)

    if not tools:
        logger.warning("memory_tools 为空，请检查插件是否成功加载。")

    system_prompt = (
        "你必须调用 read_file 工具读取文件内容，"
        "且 file_path 只能填写 implementation_plan.md。"
        "读取后只返回文件第一行，不要自行编造。"
    )
    user_prompt = "请调用 read_file 读取 implementation_plan.md，然后输出第一行。"

    for i in range(max(1, int(attempts))):
        logger.info(f"[BUG-REPRO] 尝试 {i+1}/{attempts}")
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        try:
            reply = llm.chat_with_tools(
                messages,
                tools=tools,
                tool_handler=handler,
                temperature=0.2,
                max_tool_loops=5,
                need_append_msg=True,
            )
            preview = (reply or "").strip().splitlines()[0] if reply else ""
            logger.info(f"[BUG-REPRO] 返回预览: {preview[:200]}")
        except Exception as e:
            logger.error(f"[BUG-REPRO] 触发异常: {e}")
            raise

def run_deep_repro(cfg, project_root: Path, attempts: int = 2):
    """
    更接近原始调用路径：
    - 初始化 TreeCoTModule
    - 使用 generate_code_system_prompt + memory_tools
    - 走 _test_and_fix_code 触发 LLM + 子进程执行
    """
    try:
        from LLM_MODULE.tools import load_plugins
        from LLM_MODULE.tools_plugins import read_file as read_file_plugin
        from tree_cot.prompts.work_flow_prompt_EN import generate_code_system_prompt
    except Exception as e:
        logger.error(f"加载依赖失败: {e}")
        raise

    prepare_memory_tools_task(cfg, project_root)

    logger.info("初始化 TreeCoTModule...")
    module = TreeCoTModule(cfg, project_root)

    # 准备一个尽量接近真实的目标路径
    out_dir = project_root / "problems" / cfg.problems.task_name / "output_programs" / "deep_repro"
    out_dir.mkdir(parents=True, exist_ok=True)
    target_file = str(out_dir / "main.py")

    messages = [{"role": "system", "content": generate_code_system_prompt()}]
    user_prompt = (
        "请严格按以下要求生成 Python 代码并用 ```python ...``` 包裹：\n"
        "1) 必须调用 read_file 工具读取 implementation_plan.md。\n"
        "2) 将读取内容的第一行打印出来。\n"
        "3) 程序应当可直接执行。\n"
    )
    messages.append({"role": "user", "content": user_prompt})

    logger.info("调用 _test_and_fix_code（接近原始路径）...")
    code_messages, stop = module._test_and_fix_code(
        retry_times=max(1, int(attempts)),
        messages=messages,
        folder_path=target_file,
    )
    if stop:
        raise RuntimeError("deep_repro 失败：超过最大重试次数。")
    logger.info("deep_repro 完成。")

def run_ultra_repro(cfg, project_root: Path):
    """
    最接近完整流程：加载插件 + 对齐 memory_tools 任务名 + 直接运行 module.run()。
    """
    prepare_memory_tools_task(cfg, project_root)
    logger.info("初始化 TreeCoTModule...")
    module = TreeCoTModule(cfg, project_root)
    logger.info("开始运行 run()...")
    output_path = module.run()
    logger.info("=" * 50)
    logger.info("ULTRA 模式完成！")
    logger.info(f"输出目录: {output_path}")
    logger.info("=" * 50)

def prepare_memory_tools_task(cfg, project_root: Path):
    try:
        from LLM_MODULE.tools import load_plugins
    except Exception as e:
        logger.error(f"加载 LLM_MODULE 依赖失败: {e}")
        raise

    logger.info("加载工具插件...")
    loaded = load_plugins(on_error="raise")
    logger.info(f"已加载插件: {loaded}")

    active_task = None
    try:
        from LLM_MODULE.tools_plugins import read_file as read_file_plugin
        active_task = getattr(read_file_plugin, "TASK_NAME", None)
    except Exception:
        active_task = None

    if active_task:
        logger.info(f"memory_tools 的任务名: {active_task}，将确保对应 reports 下有测试文件")
        setup_test_environment(project_root, active_task)
        cfg.problems.task_name = active_task
        if hasattr(cfg, "problem"):
            cfg.problem = cfg.problems

    return active_task

def parse_args():
    parser = argparse.ArgumentParser(description="TreeCoT 测试脚本")
    parser.add_argument(
        "--mode",
        choices=["bug", "deep", "ultra", "full"],
        default="ultra",
        help="bug: 轻量复现 chat_with_tools；deep: 接近 _test_and_fix_code；ultra: 最接近完整流程；full: 原始完整 TreeCoT",
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=2,
        help="bug/deep 模式下的尝试次数",
    )
    return parser.parse_args()

def main():
    # 设置项目根目录 (假设当前脚本在根目录)
    project_root = Path(__file__).resolve().parent
    
    # 1. 获取模拟配置
    cfg = create_mock_config()
    task_name = cfg.problems.task_name
    
    # 2. 准备测试环境 (文件/文件夹)
    setup_test_environment(project_root, task_name)

    args = parse_args()
    if args.mode == "bug":
        logger.info("进入 BUG 复现模式（chat_with_tools + memory_tools）...")
        run_bug_repro(cfg, project_root, attempts=args.attempts)
        return
    if args.mode == "deep":
        logger.info("进入 DEEP 模式（更接近 _test_and_fix_code）...")
        run_deep_repro(cfg, project_root, attempts=args.attempts)
        return
    if args.mode == "ultra":
        logger.info("进入 ULTRA 模式（最接近完整流程）...")
        run_ultra_repro(cfg, project_root)
        return
    
    # 3. 初始化模块
    logger.info("正在初始化 TreeCoTModule...")
    try:
        module = TreeCoTModule(cfg, project_root)
    except Exception as e:
        logger.error(f"初始化失败: {e}")
        raise

    # 4. 运行模块
    logger.info("开始运行 run()...")
    try:
        # 这里你可以选择传入 imply_doc，或者让它自己去读文件
        # output_path = module.run(imply_doc="直接传入的测试文档内容...") 
        output_path = module.run() # 让它去读上面 setup 创建的文件
        
        logger.info("="*50)
        logger.info(f"测试完成！")
        logger.info(f"输出目录: {output_path}")
        logger.info("请检查该目录下是否生成了: tree.json, project_tree.txt, main.py 等文件")
        logger.info("="*50)
        
    except Exception as e:
        logger.error(f"运行过程中发生错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    # 确保你的 API KEY 环境变量已经设置，因为 OpenAILLM 会读取它
    # os.environ["AGICTO_API_KEY"] = "sk-..." 
    # os.environ["AGICTO_BASE_URL"] = "..."
    
    main()
