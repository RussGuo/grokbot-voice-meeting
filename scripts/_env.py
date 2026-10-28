"""脚本公共：加载 .env（不打印任何值），只报告变量是否存在。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_env() -> None:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)
    from orchestrator.config import report
    print("环境变量是否存在：", {k: ("有" if v else "无") for k, v in report().items()})
