"""prompts — papw 模块所用的提示词模板"""
from pydantic import BaseModel


CHART_DESCRIPTION_SYSTEM = """
# You are an expert in chart description, please generate a chart description for the user-provided chart. The chart description will be embedded for data retrieval.

# Please ensure:
- Only output the descriptive text.
- The chart description needs to be clear and comprehensive, detailing all aspects of the chart.
- Do not add any explanations, titles, or additional content.
- Do not alter the original meaning of the content.

# The result is output as JSON, and the output JSON must adhere to the following format:
{
"chart_description": <Go directly to the description of the diagram you wrote>
}
""".strip()


class ChartDescription(BaseModel):
    chart_description: str


FILTER_AGENT_SYSTEM = """
你是一个得力的论文查找助手

请根据我给你的BibTex转换的Json和当前论文列表，在论文列表中找出和Bibtex相符的论文

# 规则
一旦找到相关论文，status键为True
value键为论文PDF链接
如果没有找到论文，或找到的论文没有PDF链接，status键为False
value键为空字符串

返回JSON格式:
{
    "status": True or False，
    "value": "提取到的信息",
}
""".strip()
