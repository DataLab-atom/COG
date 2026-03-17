"""papw — 论文写作子系统（从 Eureka 迁移到 Science-OS 本地架构）

基础设施层：
    llm_adapter.py          同步 LLM 客户端（读 Science-OS dynaconf 配置）
    embeddings.py           嵌入模型适配（API / 本地 HuggingFace）
    rerank.py               重排模型适配（API / 本地 FlagReranker）
    common.py               通用工具（文件、图片、日志、写作辅助）
    prompts.py              通用提示词（图表描述、论文过滤）

数据库构建层：
    text_split.py           Markdown 文本分块（tiktoken）
    scholar.py              Semantic Scholar 去重
    paper_crawler.py        文献爬取（Arxiv / Unpaywall / LLM 过滤）
    pdf_extract.py          PDF → Markdown 解析（MineRU API / 本地）
    vector_db.py            Chroma 向量库构建
    database_building.py    向量库构建主流程

实验层：
    experimenting.py        实验流程（分析角度规划 + 代码生成 + 绘图 + 写作）
    judge.py                评估代理（TextJudge / TextEval / Multimodal / TextModifier）

写作层：
    method_writing.py       方法论章节写作
    related_work_writing.py 相关工作章节写作
    experiment_writing.py   实验章节写作
    introduction_writing.py 引言章节写作
    merge_writing.py        章节合并 + 摘要/结论/标题生成
    latex_evolution.py      LaTeX 质量进化（评估 + 迭代改进）
"""
