import copy
import os
import subprocess
import logging
from pathlib import Path
from typing import Any, Optional
from itertools import chain

# === 保持原有的 imports ===
from LLM_MODULE.llm import OpenAILLM
from LLM_MODULE.tools import tool_spec, make_tool_handler
from .structure_tree import TreeNode, CircularQueue
from .prompts.prompt_template_EN import (
    dev_doc_first_prompt,
    first_step_prompt,
    generate_first_code_prompt,
)
from .prompts.work_flow_prompt_EN import (
    combine_code,
    dev_doc_main,
    dev_doc_prompt,
    dev_sys_prompt,
    dig_deeper_prompt,
    generate_code_prompt,
    generate_code_system_prompt,
    generate_tree_system_prompt,
    more_steps_prompt,
)
from .utils import (
    collect_leaves_recursive,
    convert_to_multiway,
    copy_code_to_file,
    create_folder_with_timestamp,
    extract_json,
    generate_project_tree_text,
    remove_main_block,
    save_tree,
    set_code_to_file,
    visualize_tree,
)


def chat_and_append(llm: OpenAILLM, messages: list[dict[str, Any]], *, temperature: float) -> str:
    """辅助函数：进行对话并将结果追加到消息列表"""
    content = llm.chat(messages, temperature=temperature)
    messages.append({"role": "assistant", "content": content})
    return content



class TreeCoTModule:
    def __init__(self, cfg, project_root: Path):
        """
        cfg: Hydra 配置对象
        project_root: 项目根目录路径 (用于定位 problems 文件夹)
        """
        self.cfg = cfg
        self.project_root = project_root
        self.logger = logging.getLogger("TreeCoT")

        # 1. 从 cfg 中加载模型配置
        # 对应 yaml: models.tree_cot.structure_model / doc_model / code_model
        self.llm_structure = OpenAILLM(
            model=cfg.models.tree_cot.structure_model, 
            module_name="tree_cot", 
            agent_name="architecture_agent"
        )
        self.llm_doc = OpenAILLM(
            model=cfg.models.tree_cot.doc_model, 
            module_name="tree_cot", 
            agent_name="initial_doc_agent"
        )
        self.llm_code = OpenAILLM(
            model=cfg.models.tree_cot.code_model, 
            module_name="tree_cot", 
            agent_name="initial_coder_agent"
        )

        # 2. 从 cfg 中加载参数配置
        # 对应 yaml: parameters.tree_cot.temperature.structure / code / doc
        self.temp_structure = float(cfg.models.tree_cot.temperature.structure)
        self.temp_code = float(cfg.models.tree_cot.temperature.code)
        self.temp_doc = float(cfg.models.tree_cot.temperature.doc)
        
        # 初始化状态变量
        problem_cfg = getattr(cfg, "problems", None) or getattr(cfg, "problem", None)
        self.task_name = getattr(problem_cfg, "task_name", None) or getattr(problem_cfg, "name", "")
        self.task_metric = getattr(problem_cfg, "metric", None) or getattr(problem_cfg, "task_metric", None) or ""
        if not self.task_name:
            raise AttributeError("配置缺少 `problems.task_name`（或兼容字段），无法初始化 TreeCoTModule。")
        self.ID = 0
        self.node_tree = CircularQueue(2000)
        self.dialogue_queue = CircularQueue(2000)
        self.retry_times = cfg.parameters.tree_cot.retry_times
    def run(self, imply_doc: Optional[str] = None, metric_fn_content: Optional[str] = None) -> str:
        """
        执行 Tree CoT 流程的主入口
        :param imply_doc: 可选，Implementation Plan 的内容。如果不传，则尝试从文件读取。
        :param metric_fn_content: 可选，metric_fn.py 的完整代码字符串。若提供，会写入 dst_dir
                                   并在 combine_code prompt 中指示 LLM 直接 import 使用。
        :return: 生成的代码输出目录路径
        """
        # 每次运行前重置状态
        self.ID = 0 
        self.node_tree = CircularQueue(2000)
        self.dialogue_queue = CircularQueue(2000)

        # 初始化根节点
        root = TreeNode(
            ID=self.ID,
            CurrentNode="root",
            Description="",
            has_brother_node=False,
            has_son_node=True,
            parent=None,
            depth=-1,
        )

        # === 修改处：判断 imply_doc 是否传入，未传入则使用原有逻辑读取 ===
        if imply_doc is None:
            reports_dir = self.project_root / "problems" / self.task_name / "reports"
            imply_doc_path = reports_dir / "implementation_plan.md"
            imply_doc = imply_doc_path.read_text(encoding="utf-8", errors="replace") if imply_doc_path.is_file() else ""
        # ==========================================================

        code_structure = ""
        demand_text = self.cfg.problems.demand
        # 第一步：生成初始结构
        messages: list[dict[str, Any]] = [{"role": "system", "content": generate_tree_system_prompt()}]
        messages.append({"role": "user", "content": first_step_prompt(imply_doc, code_structure, demand=demand_text)})
        chat_and_append(self.llm_structure, messages, temperature=self.temp_structure)

        # 递归生成节点
        self._generate_brother_node(messages, parent_node=root, prev_brother=None)
        root = root.SonNode or root

        # 创建输出目录
        output_root = self.project_root / "problems" / self.task_name / "output_programs"
        output_root.mkdir(parents=True, exist_ok=True)
        dst_dir = create_folder_with_timestamp(self.task_name, str(output_root))
        if not dst_dir:
            raise RuntimeError("Failed to create output folder.")
        dst_path = Path(dst_dir)

        # 写入 metric_fn.py（若由 metric_agent 提供）
        if metric_fn_content:
            (dst_path / "metric_fn.py").write_text(metric_fn_content, encoding="utf-8")
            self.logger.info("metric_fn.py written to %s", dst_path)

        # 开始树形对话，细化节点
        self._chat_tree(root, dst_path)

        # 收集叶子节点（全程内存）
        leaves, classes, desc, paths = collect_leaves_recursive(root, dst_dir)
        leaf_nodes_data = {'leaves': leaves, 'desc': desc, 'classes': classes, 'paths': paths}

        root_str = save_tree(root)  # 不写文件，直接返回 JSON 字符串
        multiway_root = convert_to_multiway(root)
        visualize_tree(multiway_root, str(dst_path / self.task_name))

        # 生成文档（内存传递）
        leaf_nodes_data = self._create_doc(
            leaf_nodes_data=leaf_nodes_data,
            tree_str=root_str
        )

        # 生成代码（内存传递）
        generate_project_tree_text(project_path=str(dst_path), output_filepath=str(dst_path / "project_tree.txt"))
        self._generate_code_logic(
            dst_dir=str(dst_path),
            leaf_nodes_data=leaf_nodes_data,
            retry_times=self.retry_times,
            tree_str=root_str,
            metric_fn_content=metric_fn_content or "",
        )

        return str(dst_path)

    def _generate_brother_node(
        self,
        messages: list[dict[str, Any]],
        *,
        parent_node: TreeNode,
        prev_brother: Optional[TreeNode] = None,
    ) -> None:
        """递归生成兄弟节点 (修复：Queue Enqueue 时机)"""
        json_dict = extract_json(messages[-1]["content"])

        current = json_dict["CurrentNode"]
        description = json_dict["Description"]
        has_brother = json_dict["BrotherNode"]
        has_son = json_dict["SonNode"]

        self.ID += 1
        node = TreeNode(
            ID=self.ID,
            CurrentNode=current,
            Description=description,
            has_brother_node=has_brother,
            has_son_node=has_son,
            parent=parent_node,
            depth=parent_node.depth + 1,
        )

        if prev_brother is None:
            parent_node.SonNode = node
        else:
            prev_brother.BrotherNode = node


        self.node_tree.enqueue(node) 
        
        # 递归处理兄弟节点
        if has_brother:
            messages.append({"role": "user", "content": more_steps_prompt(current)})
            chat_and_append(self.llm_structure, messages, temperature=self.temp_structure)
            # 注意：这里递归调用会继续修改 messages
            self._generate_brother_node(messages, parent_node=parent_node, prev_brother=node)

        self.dialogue_queue.enqueue(copy.deepcopy(messages))

    def _chat_tree(self, root: TreeNode, output_dir: Path) -> None:
        """处理树形结构对话，深入生成子节点"""
        while not self.node_tree.isEmpty():
            node_info = self.node_tree.dequeue()
            node_dialogue = self.dialogue_queue.dequeue()
            
            if node_info is None or node_dialogue is None:
                continue
            
            if node_info.has_son_node:
                node_dialogue.append({"role": "user", "content": dig_deeper_prompt(node_info.CurrentNode)})
                chat_and_append(self.llm_structure, node_dialogue, temperature=self.temp_structure)
                self._generate_brother_node(node_dialogue, parent_node=node_info)

    def _create_doc(self, *, leaf_nodes_data: dict, tree_str: str) -> dict:
        """生成文档，全程在内存中操作，返回更新后的 leaf_nodes_data。"""
        paths = list(leaf_nodes_data.get("paths", []))
        root_dir = os.path.dirname(paths[0]) if paths else ""
        modified = False

        for i in range(len(paths)):
            path = paths[i]
            if not path.endswith('.py'):
                paths[i] = path + '.py'
                modified = True

        main_path = os.path.join(root_dir, 'main.py')
        main_exists = any(path == main_path for path in paths)

        if modified and not main_exists:
            paths.append(main_path)

        leaf_nodes_data = dict(leaf_nodes_data)
        leaf_nodes_data["paths"] = paths

        leaves = leaf_nodes_data["leaves"]
        desc = leaf_nodes_data["desc"]

        doc_messages: list[dict[str, Any]] = [{"role": "system", "content": dev_sys_prompt()}]
        documents: list[str] = []
        combined = list(chain(zip(leaves, desc), [(None, None)]))

        for i, (leaf, description) in enumerate(combined):
            if i == 0:
                doc_messages.append({
                    "role": "user",
                    "content": dev_doc_first_prompt(
                        leaf, description, structure=tree_str
                    ),
                })
            elif i != len(combined) - 1:
                doc_messages.append({"role": "user", "content": dev_doc_prompt(leaf, description)})
            else:
                doc_messages.append({"role": "user", "content": dev_doc_main()})

            content = self.llm_doc.chat_with_tools(
                doc_messages,
                tools=tool_spec("memory_tools"),
                tool_handler=make_tool_handler(toolset="memory_tools", enforce_policy=True),
                temperature=self.temp_doc,
                need_append_msg=True,
            )
            documents.append(content)

        leaf_nodes_data["documents"] = documents
        return leaf_nodes_data
    def _test_and_fix_code(
        self,
        retry_times: int,
        messages: list[dict[str, Any]],
        folder_path: str,
    ) -> tuple[list[dict[str, Any]], bool]:
        """尝试生成代码并进行测试修复"""
        code_messages = copy.deepcopy(messages)
        times_left = retry_times
        test_path = str(Path(folder_path).parent / "test.py")
        
        # 预设的 import 路径
        ipt = (
            "import sys\n"
            "import os\n"
            "current_dir = os.path.dirname(os.path.abspath(__file__))\n"
            "project_root = os.path.abspath(os.path.join(current_dir, \"..\"))\n"
            "sys.path.append(project_root)\n"
        )

        for _ in range(retry_times):
            os.makedirs(os.path.dirname(test_path), exist_ok=True)
            with open(test_path, "w", encoding="utf-8") as f:
                f.write(ipt)
                f.write("\n")

            # 使用 self.llm_code 和 self.temp_code
            content = self.llm_code.chat_with_tools(
                code_messages,
                tools=tool_spec("memory_tools"),
                tool_handler=make_tool_handler(toolset="memory_tools", enforce_policy=True),
                temperature=self.temp_code,
                need_append_msg=True,
            )
            code_messages.append({"role": "assistant", "content": content})
            set_code_to_file(content, test_path)

            try:
                result = subprocess.run(
                    [os.environ.get("PYTHON", "python"), test_path],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                copy_code_to_file(remove_main_block(test_path), folder_path)
                if Path(test_path).is_file():
                    Path(test_path).unlink()
                print(result.stdout)
                break
            except subprocess.CalledProcessError as e:
                error_context = (
                    "An error occurred while executing the code:\n\n"
                    f"{e.stderr}\n\n"
                    "Please modify the code to fix these errors. Make sure the code can run correctly."
                )
                code_messages.append({"role": "user", "content": error_context})
                times_left -= 1

        if times_left == 0:
            return code_messages, True
        return code_messages, False

    def _generate_code_logic(
        self,
        dst_dir: str,
        leaf_nodes_data: dict,
        retry_times: int,
        *,
        tree_str: str,
        metric_fn_content: str = "",
    ) -> list[dict[str, Any]]:
        """根据文档和结构生成代码（内存传递，无 .npy 读写）"""
        messages: list[dict[str, Any]] = [{"role": "system", "content": generate_code_system_prompt()}]
        documents = leaf_nodes_data.get("documents", [])
        folder_path_list = leaf_nodes_data.get("paths", [])
        ds_path = self.cfg.problems.dataset_path
        for i, (doc, folder_path) in enumerate(zip(documents, folder_path_list)):
            project_tree_file = Path(dst_dir) / "project_tree.txt"
            if project_tree_file.exists():
                file_structure_str = project_tree_file.read_text(encoding="utf-8")
            else:
                file_structure_str = ""
            if i == 0:
                messages.append({
                    "role": "user",
                    "content": generate_first_code_prompt(
                        dev_doc=doc,
                        structure=tree_str,
                        dataset_path=ds_path
                    ),
                })
            elif i < len(documents) - 1:
                messages.append({"role": "user", "content": generate_code_prompt(doc, folder_path, structure=file_structure_str)})
            else:
                messages.append({"role": "user", "content": combine_code(complete_structure=file_structure_str, dev_doc="", path=folder_path, task_metric=self.task_metric, metric_fn_content=metric_fn_content)})
                combined_code, stop = self._test_and_fix_code(retry_times, messages, folder_path)
                if stop:
                    raise RuntimeError("Failed to generate code after maximum retries.")
                messages.append({"role": "assistant", "content": combined_code[-1]["content"]})
                set_code_to_file(combined_code[-1]["content"], folder_path)
                break

            code_messages, stop = self._test_and_fix_code(retry_times, messages, folder_path)
            generate_project_tree_text(project_path=dst_dir, output_filepath=str(project_tree_file))
            if stop:
                raise RuntimeError("Failed to generate code after maximum retries.")
            
            # 更新项目树结构文本
            generate_project_tree_text(project_path=dst_dir, output_filepath=str(Path(dst_dir) / "project_tree.txt"))
            messages.append({"role": "assistant", "content": code_messages[-1]["content"]})

        return messages
