"""
config.py
─────────
Dynaconf 全局配置入口。

读取优先级（从低到高）:
  1. settings.toml      — 各环境默认值
  2. .secrets.toml      — 敏感信息（API Key 等），gitignore
  3. 环境变量            — OPENAGENTS_<KEY>=value 格式

切换环境:
  export OPENAGENTS_ENV=production   # 或 development / vllm
"""

from dynaconf import Dynaconf

settings = Dynaconf(
    envvar_prefix="OPENAGENTS",          # 环境变量前缀
    settings_files=["settings.toml", ".secrets.toml"],
    environments=True,                   # 启用 [default] / [production] 等分区
    env_switcher="OPENAGENTS_ENV",       # 用该环境变量切换当前环境
    load_dotenv=True,                    # 自动加载 .env 文件
    default_env="development",
)
