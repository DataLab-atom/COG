import json
import ast
import re
import os
from datetime import datetime
import shutil
from anytree import Node, RenderTree
from anytree.exporter import DotExporter
import numpy as np
from collections import deque
from typing import List

def remove_main_block(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    # 移除 from init_generated_code import * 这一行
    lines = [line for line in lines if not line.strip().startswith('from init_generated_code import')]
    
    # 寻找包含 if __name__ == "__main__": 的行号
    cutoff = None
    for i, line in enumerate(lines):
        if line.strip().startswith('if __name__ == "__main__":'):
            cutoff = i
            break
    
    if cutoff is None:
        return ''.join(lines)  # 如果没有找到 __main__ 块，返回整个文件内容
    
    new_lines = lines[:cutoff]  # 截断到该行之前的内容
    modified_content = ''.join(new_lines)
    
    with open(file_path, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)
    
    return modified_content

def extract_json(target):
    try:
        if isinstance(target, dict):  # 如果已经是字典，直接返回
            return target
        elif isinstance(target, str):  # 如果是字符串，尝试提取 JSON
            # 匹配 Markdown 格式的 JSON（忽略大小写）
            markdown_match = re.search(r'```json\s*([\s\S]*?)\s*```', target, re.IGNORECASE)
            if markdown_match:
                json_part = markdown_match.group(1).strip()  # 提取 JSON 部分
                return json.loads(json_part)  # 将 JSON 字符串解析为字典
            
            # 匹配纯 JSON 字符串
            json_match = re.search(r'^\s*\{.*\}\s*$', target, re.DOTALL)
            if json_match:
                return json.loads(target.strip())  # 直接解析整个字符串为 JSON
            
            raise ValueError("Input does not match any supported JSON format.")
        else:
            raise TypeError(f"Unsupported type for target: {type(target)}")
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to decode JSON: {e}")
    except re.error as e:
        raise ValueError(f"Regular expression error: {e}")

def extract_markdown_content(text: str) -> str:
    """
    从输入字符串中提取第一个被 ```markdown ... ``` 包裹的内容。

    Args:
        text (str): 包含 markdown 代码块的原始字符串。

    Returns:
        str: 第一个被提取出来的内容字符串。如果没有找到匹配项，则返回空字符串。
    """
    # 正则表达式模式保持不变
    pattern = r"```markdown\s*(.*?)\s*```"
    
    # 使用 re.search() 来查找第一个匹配项
    # 它会返回一个匹配对象（match object），或者在找不到时返回 None
    match = re.search(pattern, text, flags=re.DOTALL | re.IGNORECASE)
    
    # 如果找到了匹配项 (match is not None)
    if match:
        # match.group(1) 用于获取第一个捕react组（也就是括号里）匹配到的内容
        # .strip() 用于移除内容前后可能的多余空白或换行
        return match.group(1).strip()
    else:
        # 如果没有找到匹配项，返回一个空字符串
        return ""

def save_tree(root_node, save_path=None):
    """
    将树对象序列化为JSON字符串，并可选地写入文件。

    Args:
        root_node (TreeNode): 要保存的树的根节点。
        save_path (str, optional): 若提供，则将JSON写入该路径；否则仅返回字符串。

    Returns:
        str: 树的JSON字符串表示。
    """

    def _tree_to_dict(node):
        if node is None:
            return None
        node_dict = {
            "ID": node.ID,
            "CurrentNode": node.CurrentNode,
            "Description": node.Description,
            "depth": node.depth,
            "children": []
        }
        child = node.SonNode
        while child:
            node_dict["children"].append(_tree_to_dict(child))
            child = child.BrotherNode
        return node_dict

    tree_as_dict = _tree_to_dict(root_node)
    json_output = json.dumps(tree_as_dict, indent=4, ensure_ascii=False)

    if save_path:
        with open(save_path, 'w', encoding='utf-8') as f:
            f.write(json_output)

    return json_output


def extract_code_with_structures(file_path, keep_last=False):
    """
    提取并处理代码结构，可选保留最后出现的同名定义
    
    :param file_path: Python文件路径
    :param keep_last: 是否保留最后出现的同名定义（默认False）
    :return: 结构列表，包含类型、名称和源码
    """
    with open(file_path, "r", encoding="utf-8") as f:
        source = f.read()
    
    tree = ast.parse(source)
    lines = source.split('\n')
    raw_results = []

    class Collector(ast.NodeVisitor):
        def __init__(self):
            self.current_class = None

        def visit_ClassDef(self, node):
            start = self._get_start_line(node)
            code = '\n'.join(lines[start-1:node.end_lineno])
            
            raw_results.append({
                'type': 'class',
                'code_name': node.name,
                'code_source': f'```python\n{code}\n```',
                'lineno': node.lineno
            })
            
            self.current_class = node.name
            self.generic_visit(node)
            self.current_class = None

        def visit_FunctionDef(self, node):
            if self.current_class is None:
                start = self._get_start_line(node)
                code = '\n'.join(lines[start-1:node.end_lineno])
                
                raw_results.append({
                    'type': 'function',
                    'code_name': node.name,
                    'code_source': f'```python\n{code}\n```',
                    'lineno': node.lineno
                })
            
            self.generic_visit(node)

        def _get_start_line(self, node):
            return min(d.lineno for d in node.decorator_list) if node.decorator_list else node.lineno

    # 遍历AST节点
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)):
            Collector().visit(node)

    # 按行号排序
    sorted_results = sorted(raw_results, key=lambda x: x['lineno'])
    
    # 去重处理
    if keep_last:
        seen = {}
        for idx, item in enumerate(sorted_results):
            key = (item['type'], item['code_name'])
            seen[key] = idx
        
        # 筛选保留最后出现的条目
        final_results = []
        for idx, item in enumerate(sorted_results):
            if seen[(item['type'], item['code_name'])] == idx:
                final_results.append(item)
        return final_results
    
    return sorted_results

def set_code_to_file(content, file_path):
    """
    从字符串中提取一个完整的Python代码块，并将其追加到文件中。

    这个函数会查找以 ```python 开始并以 ``` 结束的代码块。
    它使用贪婪匹配来确保即使代码内容本身包含 ``` 字符串，也能正确提取完整的块。

    Args:
        content (str): 包含代码块的字符串。
        file_path (str): 要追加代码的目标文件的路径。

    Raises:
        ValueError: 如果在内容中没有找到有效的代码块。
    """
    try:
        # 使用贪婪匹配 (.*) 来捕获从第一个 ```python 到最后一个 ``` 之间的所有内容。
        # re.DOTALL 标志允许 `.` 匹配包括换行符在内的任何字符。
        match = re.search(r'```python\s*\n(.*)\n```', content, re.DOTALL)
        
        if match:
            # match.group(1) 包含被捕获的代码内容。
            code_content = match.group(1).strip()
            
            # 使用 'w' 模式写入文件，确保测试前文件是空的
            with open(file_path, 'a', encoding='utf-8') as code_file:
                code_file.write(code_content)
            return
        
        # 如果没有找到匹配项，则抛出异常
        raise ValueError("在响应中未找到有效的Python代码块")
    
    except:
        # 如果提取代码失败，则写入一个会直接报错的代码
        error_code = "The content you returned is empty, please return it again.\n"
        with open(file_path, 'w', encoding='utf-8') as code_file:
            code_file.write(error_code)
        return


def copy_file_with_timestamp(src_path, dst_dir=None):
    """
    复制文件并在目标文件名中添加当前时间戳。
    
    :param src_path: 源文件的路径
    :param dst_dir: 目标目录，默认为None表示与源文件同一目录
    """
    # 检查源文件是否存在
    if not os.path.isfile(src_path):
        print(f"源文件 {src_path} 不存在")
        return
    
    # 获取源文件的文件名和扩展名
    file_name, file_extension = os.path.splitext(os.path.basename(src_path))
    
    # 获取当前时间的时间戳字符串
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # 构建新的文件名
    new_file_name = f"{file_name}_{timestamp}{file_extension}"
    
    # 确定目标路径
    if dst_dir is None:
        dst_path = os.path.join(os.path.dirname(src_path), new_file_name)
    else:
        if not os.path.exists(dst_dir):
            os.makedirs(dst_dir)  # 如果目标目录不存在，则创建它
        dst_path = os.path.join(dst_dir, new_file_name)
    
    # 复制文件
    try:
        shutil.copy(src_path, dst_path)
        print(f"文件已成功复制到 {dst_path}")
    except Exception as e:
        print(f"复制文件时出错: {e}")

def copy_code_to_file(content,fila_path):
    with open(fila_path,'a',encoding='utf-8') as code_file:
            code_file.write(content)
            code_file.write('\n')

def collect_leaves_recursive(root, root_dir: str):
    """
    根据最终规则生成路径：深度>=2的叶子节点，其路径由其在深度2的祖先决定。
    """
    def _get_full_path(node) -> str:
        path_determining_node = node

        # --- 这是最终的、唯一的逻辑 ---
        # 如果叶子节点的深度大于等于2
        if node.depth >= 2:
            curr = node
            # 向上回溯，直到找到深度正好为2的那个祖先节点
            while curr and curr.depth > 2:
                curr = curr.parent
            
            if curr and curr.depth == 2:
                path_determining_node = curr
        
        # --- 后续路径构建逻辑 ---
        path_components = deque()
        curr_builder = path_determining_node
        while curr_builder is not None:
            path_components.appendleft(curr_builder.CurrentNode)
            curr_builder = curr_builder.parent
        
        if path_components: path_components.popleft()
        if not path_components: return root_dir
            
        filename = f"{path_components.pop()}"
        #full_path = os.path.join(root_dir, *list(path_components), filename)
        full_path = os.path.join(root_dir,*list(path_components)[1:],filename)

        return full_path
    
    def traverse(node, leaves, classes, desc, paths):
        if not node: return
        if not node.has_son_node and node.parent is not None:
            leaves.append(node.CurrentNode)
            classes.append(node.parent.CurrentNode)
            desc.append(node.Description)
            paths.append(_get_full_path(node))
        traverse(node.SonNode, leaves, classes, desc, paths)
        traverse(node.BrotherNode, leaves, classes, desc, paths)

    leaves, classes, descriptions, paths = [], [], [], []
    traverse(root.SonNode, leaves, classes, descriptions, paths)
    return leaves, classes, descriptions, paths


class MultiwayTreeNode:
    """多叉树节点（转换用）"""
    def __init__(self, name):
        self.name = name       # 节点标识
        self.children = []     # 子节点列表


def convert_to_multiway(root):
    """兄弟节点树 -> 多叉树转换"""
    def process_node(original_node):
        if not original_node:
            return None
        
        # 创建新节点（只保留名称）
        new_node = MultiwayTreeNode(original_node.CurrentNode)
        
        # 处理子节点链（兄弟节点展开为同级子节点）
        child = original_node.SonNode
        while child:
            converted_child = process_node(child)
            if converted_child:
                new_node.children.append(converted_child)
            child = child.BrotherNode
        
        return new_node
    
    return process_node(root)

import os
import subprocess
from anytree import Node, RenderTree
# 假设你有这些导入，如果没有则需要添加
# from anytree.exporter import DotExporter 

def visualize_tree(multiway_root, filename="tree_structure"):
    """生成纯节点结构图"""
    # 转换到anytree节点
    def build_anytree(node, parent=None):
        if not node:
            return
        any_node = Node(node.name, parent=parent)
        for child in node.children:
            build_anytree(child, any_node)
        return any_node
    
    # 生成结构
    visual_root = build_anytree(multiway_root)
    
    # 文本预览
    print("树结构文本预览:")
    for pre, _, node in RenderTree(visual_root):
        print(f"{pre}{node.name}")
    
    # --- 修改部分：手动创建 DOT 文件并调用 dot 命令 ---
    # 生成 DOT 代码字符串
    def _generate_dot_code(root_node):
        lines = ["digraph tree {", 'node [shape=box, style=filled, fillcolor=lightgrey];']
        for pre, _, node in RenderTree(root_node):
            # 处理节点名称中的引号，避免 DOT 语法错误
            safe_label = node.name.replace('"', r'\"')
            lines.append(f'"{node.name}" [label="{safe_label}"];')
            if node.parent:
                # 处理父节点名称中的引号
                safe_parent_label = node.parent.name.replace('"', r'\"')
                lines.append(f'"{node.parent.name}" -> "{node.name}";')
        lines.append("}")
        return "\n".join(lines)

    dot_code = _generate_dot_code(visual_root)
    
    # 创建临时 DOT 文件（在当前目录）
    temp_dot_file = f"{filename}.dot"
    try:
        with open(temp_dot_file, 'w', encoding='utf-8') as f:
            f.write(dot_code)
        
        # 调用 dot 命令生成 PNG（若未安装则仅保留 .dot 文件）
        dot_exe = os.environ.get("GRAPHVIZ_DOT") or shutil.which("dot")
        if not dot_exe:
            print("警告: 未找到 Graphviz 的 dot 命令，已保留 .dot 文件，请安装 Graphviz 或设置 GRAPHVIZ_DOT。")
            print(f"DOT 文件路径: {os.path.abspath(temp_dot_file)}")
            return

        cmd = [dot_exe, temp_dot_file, "-T", "png", "-o", f"{filename}.png"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            print(f"错误: dot 命令执行失败。")
            print(f"错误输出: {result.stderr}")
            print(f"命令: {' '.join(cmd)}")
        else:
            print(f"\n结构图已保存到 {filename}.png")
    
    except Exception as e:
        print(f"生成 DOT 文件或调用 dot 命令时发生错误: {e}")
    finally:
        # 清理临时 DOT 文件
        if os.path.exists(temp_dot_file):
            try:
                os.remove(temp_dot_file)
            except OSError as e:
                print(f"警告: 无法删除临时文件 {temp_dot_file}: {e}")
    # --- 修改部分结束 ---
def save_list_to_npy(leaves, classes, desc, paths, file_path):
    """
    将叶子节点标识和描述信息保存到 .npy 文件中
    参数:
        leaves (list): 叶子节点标识列表
        desc (list): 描述信息列表
        file_path (str): 保存的 .npy 文件路径
    """
    # 组合数据为字典
    data = {'leaves': leaves, 'desc': desc, 'classes': classes,'paths':paths}
    
    # 保存到 .npy 文件
    np.save(file_path, data)
    print(f"数据已保存到 {file_path}")


def create_folder_with_timestamp(folder_name, base_directory=None):
    """
    在指定目录下生成带有时间戳的文件夹。

    参数:
        folder_name (str): 要创建的文件夹名称。
        base_directory (str): 基础目录路径（可选）。如果为 None，则默认使用当前工作目录。

    返回:
        str: 创建的文件夹的绝对路径。
    """
    # 如果未提供基础目录，则使用当前工作目录
    if base_directory is None:
        base_directory = os.getcwd()
    
    # 获取当前时间并格式化为字符串（例如：2023-10-05_14-30-00）
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    
    # 将时间戳附加到文件夹名称
    folder_name_with_timestamp = f"{folder_name}_{timestamp}"
    
    # 拼接完整路径
    full_path = os.path.join(base_directory, folder_name_with_timestamp)
    
    try:
        # 检查文件夹是否存在
        if not os.path.exists(full_path):
            # 创建文件夹
            os.makedirs(full_path)
            print(f"文件夹已成功创建: {full_path}")
        else:
            print(f"文件夹已存在: {full_path}")
        
        # 返回文件夹的绝对路径
        return os.path.abspath(full_path)
    except Exception as e:
        print(f"文件夹创建失败: {e}")
        return None

"""
def save_module_design(module_data, root_dir=""):
    '''保存模块设计文档到结构化目录'''
    
    # 1. 生成唯一文件名或使用模块名
    module_name = module_data["moduleName"].strip().replace(" ", "_")

    # 2. 创建模块目录
    doc_dir = os.path.join(root_dir, "json_doc")  # 例如: "project_docs/doc"
    os.makedirs(doc_dir, exist_ok=True)  # 自动创建目录（如果不存在）
    # 3. 保存主设计文件
    design_path = os.path.join(doc_dir, f"{module_name}.json")
    with open(design_path, "w", encoding="utf-8") as f:
        json.dump(module_data, f, ensure_ascii=False, indent=2)
    
    print(f"模块文档已保存至: {design_path}")
    return design_path
"""

def save_module_design(module_data, cls, root_dir=""):
    """保存模块设计文档到结构化目录"""
    
    # 1. 生成唯一文件名或使用模块名
    module_name = module_data["moduleName"].strip().replace(" ", "_")

    # 2. 创建模块目录
    folder_dir = os.path.join(root_dir, cls)
    doc_dir = os.path.join(folder_dir, "json_doc")  # 例如: "project_docs/doc"
    os.makedirs(doc_dir, exist_ok=True)  # 自动创建目录（如果不存在）
    # 3. 保存主设计文件
    design_path = os.path.join(doc_dir, f"{module_name}.json")
    with open(design_path, "w", encoding="utf-8") as f:
        json.dump(module_data, f, ensure_ascii=False, indent=2)
    
    print(f"模块文档已保存至: {design_path}")
    return design_path

def code_path_name(module_data):
    module_name = module_data["moduleName"].strip().replace(" ", "_")
    return module_name

def analyze_file(filepath):
    """分析单个 Python 文件，提取类和函数"""
    with open(filepath, "r", encoding="utf-8") as source:
        try:
            tree = ast.parse(source.read(), filename=filepath)
        except SyntaxError:
            return {"classes": [], "functions": [], "error": "SyntaxError"}
        except Exception as e:
            return {"classes": [], "functions": [], "error": f"Parsing Error: {type(e).__name__}"}

    classes = []
    functions = []
    
    # 遍历模块的直接子节点以查找顶层类和函数
    for node in tree.body: # tree 是 Module 节点
        if isinstance(node, ast.ClassDef):
            class_methods = []
            # 遍历类定义体内的节点以查找方法
            for body_item in node.body:
                if isinstance(body_item, ast.FunctionDef) or isinstance(body_item, ast.AsyncFunctionDef):
                    class_methods.append(body_item.name)
            classes.append({"name": node.name, "methods": class_methods})
        # --- 错误修正：将 body_item 改为 node ---
        elif isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
            functions.append(node.name)

    return {"classes": classes, "functions": functions}

# --- 定义要忽略的项 (保持不变) ---
IGNORED_DIR_NAMES = {'.git', '__pycache__', '.vscode', '.idea', 'venv', '.env', '.mypy_cache', '.pytest_cache', 'json_doc'}
IGNORED_FILE_EXTENSIONS = {'.json', '.txt', '.log', '.md', '.DS_Store', '.yml', '.yaml', '.xml', '.csv', '.lock', '.swp', '.pyc', '.bak','.npy','.png'}
IGNORED_FILE_NAMES = {'README.md', 'LICENSE', 'requirements.txt'}

def generate_project_tree_text(project_path, indent_char="    ", prefix="", output_filepath=None):
    """
    生成项目结构的文本树。
    
    Args:
        project_path (str): 要分析的项目路径。
        indent_char (str): 缩进字符，默认为四个空格。
        prefix (str): 当前层级的树形前缀。
        output_filepath (str, optional): 如果提供，将结果保存到指定文件路径。
                                         默认为 None，表示不保存到文件。
    
    Returns:
        list: 包含项目树每一行的字符串列表。如果 output_filepath 被提供，则同时会执行文件写入操作。
    """
    output_lines = []
    try:
        items = sorted(os.listdir(project_path))
    except FileNotFoundError:
        output_lines.append(f"{prefix}└── [Error: Directory not found - {project_path}]")
        if prefix == "" and output_filepath:
            _write_to_file(output_filepath, output_lines, project_path, clear_file=True)
        return output_lines
    except PermissionError:
        output_lines.append(f"{prefix}└── [Error: Permission denied - {project_path}]")
        if prefix == "" and output_filepath:
            _write_to_file(output_filepath, output_lines, project_path, clear_file=True)
        return output_lines
    except Exception as e:
        output_lines.append(f"{prefix}└── [Error accessing {project_path}: {type(e).__name__}]")
        if prefix == "" and output_filepath:
            _write_to_file(output_filepath, output_lines, project_path, clear_file=True)
        return output_lines

    filtered_items = []
    for item_name in items:
        item_path = os.path.join(project_path, item_name)
        if os.path.isdir(item_path):
            if item_name in IGNORED_DIR_NAMES:
                continue
        elif os.path.isfile(item_path):
            if item_name in IGNORED_FILE_NAMES or any(item_name.endswith(ext) for ext in IGNORED_FILE_EXTENSIONS):
                continue
        filtered_items.append(item_name)
    
    for i, item_name in enumerate(filtered_items):
        item_path = os.path.join(project_path, item_name)
        is_last = (i == len(filtered_items) - 1) 
        connector = "└── " if is_last else "├── "
        
        if os.path.isdir(item_path):
            output_lines.append(f"{prefix}{connector}{item_name}/")
            new_prefix = prefix + (indent_char if is_last else "│   ")
            output_lines.extend(generate_project_tree_text(item_path, indent_char, new_prefix))
        elif os.path.isfile(item_path):
            output_lines.append(f"{prefix}{connector}{item_name}")

            if item_name.endswith(".py"):
                details = analyze_file(item_path)
                
                file_content_indent_prefix = prefix + (indent_char if is_last else "│   ")
                
                if "error" in details:
                    output_lines.append(f"{file_content_indent_prefix}└── (Error: {details['error']})")
                    continue

                num_classes = len(details.get("classes", []))
                num_functions = len(details.get("functions", []))

                for cls_index, cls_info in enumerate(details.get("classes", [])):
                    cls_is_last_major_item = (cls_index == num_classes - 1) and (num_functions == 0)
                    cls_connector = "└── " if cls_is_last_major_item else "├── "
                    output_lines.append(f"{file_content_indent_prefix}{cls_connector}[Class] {cls_info['name']}")
                    
                    method_line_prefix = file_content_indent_prefix + ("    " if cls_is_last_major_item else "│   ")
                    
                    for method_index, method_name in enumerate(cls_info.get("methods", [])):
                        method_is_last = (method_index == len(cls_info.get("methods", [])) - 1)
                        method_connector = "└── " if method_is_last else "├── "
                        output_lines.append(f"{method_line_prefix}{method_connector}{method_name}()")

                for func_index, func_name in enumerate(details.get("functions", [])):
                    func_is_last_major_item = (func_index == num_functions - 1)
                    func_connector = "└── " if func_is_last_major_item else "├── "
                    
                    output_lines.append(f"{file_content_indent_prefix}{func_connector}[Func] {func_name}()")
    
    if prefix == "" and output_filepath:
        _write_to_file(output_filepath, output_lines, project_path, clear_file = True)

    return output_lines

def _write_to_file(filepath, lines_to_write, project_path_for_header, clear_file=False):
    """私有辅助函数，用于将行写入文件。"""
    mode = "w" if clear_file else "a"
    try:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, mode, encoding="utf-8") as f:
            if clear_file:
                f.write(f"program: {os.path.abspath(project_path_for_header)}\n")
                f.write("-" * 30 + "\n")
            for line in lines_to_write:
                f.write(line + "\n")
        print(f"\n项目结构已成功更新到 '{os.path.abspath(filepath)}'")
    except IOError as e:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        print(f"\n错误：无法写入文件 '{os.path.abspath(filepath)}': {e}")
