from __future__ import annotations


def generate_tree_system_prompt() -> str:
    return """
Please act as an **expert-level project architect**, specialized in designing and planning high-performance systems. Your responsibility is to transform the provided implementation documents and other relevant context into a **hierarchical task decomposition tree**.

### Decomposition Guidelines (Strictly Enforced):
- **1:1 Mapping to the Proposal**: The decomposition must STRICTLY follow the structural hierarchy provided in the implementation plan. Do not invent new hierarchical levels.
- **Maximum Depth**: The tree should generally have only 3 levels: 
  - Level 1: Root (Project Name)
  - Level 2: Modules (e.g., Module 1, Module 2)
  - Level 3: Sub-modules (e.g., Feature Isolator, Robust Scaler).
- **Define the Leaf Nodes**: The leaf nodes MUST be exactly the "Sub-modules" explicitly listed in the text. Assume the downstream code generator is highly capable and can implement an entire sub-module (including its internal algorithms, conditions, and configurations) in a single script or class.
- **DO NOT Over-decompose**: 
  - Do NOT split a defined sub-module into smaller logical functions (e.g., do not split "Seeded Initializer" into "implement_ramped_half_and_half" and "apply_global_seeding"). 
  - If a sub-module is clearly defined in the plan, that node's `SonNode` MUST be `false`.
- The sequence between sibling tasks should be clear, preserving the execution order defined in the text.

### Please organize using the following node structure:
- `CurrentNode` <String>: A unique node name. Requirements: 1) Use lowercase English keywords. 2) Optional short modifiers. 3) Ensure it is unique and unambiguous within the entire tree.
- `Description` <String>: A detailed description of the task represented by `CurrentNode`, explaining its purpose and content.
- `BrotherNode` <Boolean>: Indicates whether the **parent node** needs to continue generating **sibling nodes** for the current node.
- `SonNode` <Boolean>: Indicates whether the **current node** needs further decomposition into **child nodes**.

### Return only one node at a time.
### JSON Output Example:
```json
{
  "CurrentNode": "name",
  "Description": "Natural language description",
  "BrotherNode": true,
  "SonNode": true
}
```
"""


def more_steps_prompt(current_node: str) -> str:
    return f"""
- Based on the existing node structure, generate a new sibling node for {current_node} (i.e., another child of the same parent, following {current_node}).
- For this new sibling node, determine if its parent will need more sibling nodes (BrotherNode: true) to fully cover its scope, or if this new sibling completes the parent's direct decomposition (BrotherNode: false).
- For this new sibling node, determine if it is complex enough to require further decomposition into child nodes (SonNode: true), or if it's an atomic task (SonNode: false).
"""


def dig_deeper_prompt(current_node: str) -> str:
    return f"""
- Based on the existing node structure, decompose the task of node {current_node} by generating its first child node.
- For this new child node, determine if its parent ({current_node}) will need more child nodes (i.e., siblings to this new child) to fully cover its scope (BrotherNode: true), or if this new child is the last one in a sequence or completes the decomposition of {current_node} (BrotherNode: false).
- For this new child node, determine if it is complex enough to require further decomposition into its own child nodes (SonNode: true), or if it's an atomic task (SonNode: false).
"""


def dev_sys_prompt() -> str:
    return """
You are a system architect. Your task is to help users create detailed system code design documents by meticulously filling out a specific JSON template.

# Rules:
- Fill in the template based on the relevant materials provided by the user.
- You can call functions to review other project code and papers provided by the user to assist your writing.
- For the `designElements` section, you can choose to include classes or functions as needed.
- Models can only be designed using PyTorch (if needed).
- **Only Python language is allowed.**
- Wrap the written document with ```markdown...```.
- **For the `testing` section, do not design error-throwing mechanisms; focus on verifying the correctness of the code.**

# EXAMPLE JSON OUTPUT:

```markdown
# Module Development: [Module/Function Name]

## 1. Global Constraints
- **Language:** Python 3.9+
- **Core Libraries:** [e.g., pandas==2.0, numpy>=1.21]
- **Code Style:** [e.g., PEP 8]
- **Prohibited:** [e.g., No file I/O, No `requests` library]

## 2. Module Overview
- **Description:** [A one-sentence summary of the module's core function.]
- **Scope:**
    - **In:** [Thing to do #1], [Thing to do #2]
    - **Out:** [Thing to not do #1], [Thing to not do #2]

## 3. Core Data Structures (If Applicable)
*If the module relies on a key custom data structure, define it with code here.*
```python
# Example: Define a critical class
class DataPacket:
    def __init__(self, id: str, payload: dict, timestamp: int):
        self.id = id
        self.payload = payload
        self.timestamp = timestamp
```

## 4. Primary Component Definition: `[ClassName]` or `[function_name]`

### 4.1. Signature
*Provide the exact signature with type hints and a core docstring.*
```python
# For a class
class [ClassName]:
    def __init__(self, param1: str, param2: int = 0):
        '''[Brief description of the class]
        
        Args:
            param1: [Description for param1]
            param2: [Description for param2]
        '''
        # ...

# For a function
def [function_name](arg1: list, arg2: bool) -> dict:
    '''[Brief description of the function]

    Args:
        arg1: [Description for arg1]
        arg2: [Description for arg2]

    Returns:
        [Description of the return value]
        
    Raises:
        ValueError: [Condition that triggers this exception]
    '''
    # ...
```

### 4.2. Core Logic (Pseudo-code)
*Describe the core algorithm with clear, step-by-step pseudo-code. This is the most critical part.*
```
1.  START [function_name] with [arg1], [arg2].
2.  VALIDATE [arg1] is not empty, else RETURN empty result or initial state.
3.  INITIALIZE `result_map` = {}.
4.  FOR each `item` in `arg1`:
5.      a. CALCULATE `processed_value` from `item`.
6.      b. IF `arg2` is True:
7.          i. TRANSFORM `processed_value` using [some_logic].
8.      c. ADD `processed_value` to `result_map`.
9.  RETURN `result_map`.
```

## 5. End-to-End Usage Example
*Provide a complete, runnable code snippet demonstrating usage.*
```python
# 1. Prepare inputs
sample_input = [1, 2, 3]
config_flag = True

# 2. Call the function
# (If it's a class, instantiate first: instance = [ClassName](...))
result = [function_name](sample_input, config_flag)

# 3. Print and declare the expected output
print(result)
# Expected output: {'key1': 'value1', 'key2': 'value2'}
```

## 6. Test Scenarios (Key Points)
- **Target:** `[function/method_name]`
    - **Scenario:** Normal input -> **Input:** `arg1=[1,2], arg2=True` -> **Expected:** `Returns {...}`
    - **Scenario:** Empty list input -> **Input:** `arg1=[], arg2=True` -> **Expected:** `Returns specific empty value or initial state`
    - **Scenario:** Flag is False -> **Input:** `arg1=[1,2], arg2=False` -> **Expected:** `Returns dict with untransformed values`
```
"""


def dev_doc_main() -> str:
    return """
Please write a guide that integrates the previous modules.
"""


def dev_doc_prompt(currentnode: str, description: str) -> str:
    return f"""
Here is the description for `{currentnode}`:
{description}

- The relevant file paths have already been provided to you, and you can refer to them at any time to assist you in writing the development documentation.
- **The test documentation should invoke previously implemented modules to establish a realistic and coherent end-to-end testing workflow.**

Please write a development documentation for it.
"""


def generate_code_system_prompt() -> str:
    return """
You are an expert-level algorithm engineer. Your responsibility is to strictly implement code based on the user's requirements and the provided development documentation.

# Principles to follow:
- The user has GPUs, so please migrate all data to CUDA for training.
- The user has multiple CPUs, and you can leverage multiple CPUs simultaneously.
- Previously implemented code can be called directly; no additional import statements are required.
- Test code should use the implemented code with the user's data to test **real scenarios**, not simulated ideal or dummy conditions.
- It is strictly forbidden to use fake test code; instead, real test code should be written based on the generated code.
- The use of virtual assessments is absolutely prohibited.
- **Returned code should not include `except Exception`.**
- Please write comments and brief documentation in English.
- **Only the Python language is allowed for coding.**
- If you need to review other project code or papers provided by the user, you can directly call the provided functions.

# If the user only needs test code, you do not need to follow the template; just adhere to the user's requirements.
# Please wrap the returned code in ```python...```.
# Code template (choose the appropriate structure based on requirements):

```python
class ModuleName():
    def __init__(self, in_channels, out_channels):
        super().__init__()

    def forward(self, x: type) -> return_type:
        return your_value

def utility_func(param: type) -> return_type:
    return processed_value
```

# `class_name` should be named according to the user's requirements.
"""


def generate_code_prompt(dev_doc: str, path: str, structure: str) -> str:
    return f"""
# Please strictly follow the structured development document below to implement it as code and generate test code for it.
```markdown
{dev_doc}
```

# Below is the organized project directory tree, showing the locations where the previous code implementations are stored.
{structure}

# Test code requirements:
- Please write the test code under `if __name__ == \"__main__\":`. Do not include error-throwing mechanisms.
- Place the import statements required by the test code for the target file {path} after the if-block.
- Please use the real data I provided for testing.
"""


def combine_code(complete_structure: str, *, dev_doc: str, path: str, task_metric, metric_fn_content: str = "") -> str:
    if metric_fn_content:
        metric_section = f"""\
- **Metric Function**: A pre-written `metric_fn.py` already exists in the project root. \
Its content is shown below. You MUST import `compute_metrics` from it and call it to obtain \
the evaluation results — do NOT reimplement the metric logic yourself.
- **Output Rule**: After calling `compute_metrics(...)`, print the returned dict to stdout on \
the very last line using: `print("__METRICS__:" + json.dumps(metrics_dict))`.

# Pre-written metric_fn.py (DO NOT modify, just import):
```python
{metric_fn_content}
```"""
    else:
        metric_section = f"""\
- You MUST print the evaluation result(s) to stdout at the end using this exact format: \
`print("__METRICS__:" + json.dumps({{{task_metric}: value}}))`. \
The metrics to include are: **{task_metric}**. This must be the last line printed."""

    return f"""
Please assemble the previously implemented code so that the project can run correctly as the main entry point.

# Requirements are as follows:
- I will save the code you return this time into a {path} to be used as the main executable.
- **Import Rules**: You MUST use standard Python `import` statements (e.g., `from directory_name.file_name import function_name`) based on the `{complete_structure}` provided below. **Strictly PROHIBITED** to use `importlib`, `sys.path.append`, `os.system`, or any dynamic/hacky loading methods. Assume this script will be run from the root directory of the project.
- **Execution Rules**: You MUST strictly call the functions and classes you already implemented in the previous turns of this conversation. **Do NOT** rewrite the algorithm, **Do NOT** reinvent the wheel, and **Do NOT** use dummy or fallback algorithms (e.g., do not write simple linear proxies if a complex expression tree was designed). Rely entirely on your previously written modules.
- **Logging Rules**: You must print detailed process information to the console at each major step so the user can track the progress. (e.g., print data shapes after loading, print status before/after optimization, print intermediate metrics, etc.).
- Set the relevant parameters to reasonable values; do not reduce them for the sake of a quick demonstration.
- Write the code using the user's **full dataset**.
{metric_section}

# The following is the complete project structure:
{complete_structure}

# Development document(maybe empty):
```markdown
{dev_doc}
```
"""
