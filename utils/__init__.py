# 加载 open-agents 框架（作为 _oa_utils 命名空间），并 re-export 所有公开符号。
from utils._oa_loader import oa_utils as _oa

text_utils = _oa.text_utils
input_utils = _oa.input_utils

ToolResult = _oa.ToolResult

validate_long_running_propagation = _oa.validate_long_running_propagation
validate_all_configs = _oa.validate_all_configs

call_tool = _oa.call_tool
call_from_file = _oa.call_from_file
call_from_dict = _oa.call_from_dict
list_tools = _oa.list_tools
to_openai_schema = _oa.to_openai_schema
call_input = _oa.call_input
call_input_from_file = _oa.call_input_from_file
list_inputs = _oa.list_inputs

import sys as _sys
_ch = _sys.modules["_oa_utils._channel"]
create_channel = _ch.create_channel
send_to_channel = _ch.send_to_channel
close_channel = _ch.close_channel
receive_from_channel = _ch.receive_from_channel
channel_exists = _ch.channel_exists
list_channels = _ch.list_channels
ChannelClosedError = _ch.ChannelClosedError

run_pipeline = _oa.run_pipeline
run_pipeline_from_file = _oa.run_pipeline_from_file
run_pipeline_stream = _oa.run_pipeline_stream
run_pipeline_stream_from_file = _oa.run_pipeline_stream_from_file
PipelineResult = _oa.PipelineResult

run_graph = _oa.run_graph
run_graph_from_file = _oa.run_graph_from_file
run_graph_stream = _oa.run_graph_stream
run_graph_stream_from_file = _oa.run_graph_stream_from_file
run_graph_events = _oa.run_graph_events
GraphResult = _oa.GraphResult
