# shim: load open-agents/llm_config.py to eliminate duplicate code.
# config.py + settings.toml remain in COG so dynaconf resolves from cwd correctly.
import importlib.util
import sys
from pathlib import Path

_path = Path(__file__).parent.parent / "open-agents" / "llm_config.py"
_spec = importlib.util.spec_from_file_location("_oa_llm_config", str(_path))
_mod = importlib.util.module_from_spec(_spec)
sys.modules["_oa_llm_config"] = _mod
_spec.loader.exec_module(_mod)

LLMConfig = _mod.LLMConfig
load_config = _mod.load_config
load_config_from_file = _mod.load_config_from_file
load_prompt = _mod.load_prompt
validate_variables = _mod.validate_variables
build_messages = _mod.build_messages
schema_to_pydantic = _mod.schema_to_pydantic
make_client = _mod.make_client
run_text = _mod.run_text
run_text_stream = _mod.run_text_stream
run_json_prompt = _mod.run_json_prompt
run_json_instructor = _mod.run_json_instructor
run = _mod.run
run_from_file = _mod.run_from_file
run_from_dict = _mod.run_from_dict
