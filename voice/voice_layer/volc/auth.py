"""火山（豆包语音）鉴权头。只读取环境变量，绝不打印或记录值。

新版控制台（优先）：X-Api-Key = VOLC_API_KEY
旧版控制台：
  ASR  X-Api-App-Key = VOLC_APP_ID, X-Api-Access-Key = VOLC_ACCESS_TOKEN
       文档 https://docs.volcengine.com/docs/6561/1354869
  TTS  X-Api-App-Id  = VOLC_APP_ID, X-Api-Access-Key = VOLC_ACCESS_TOKEN
       文档「旧版控制台鉴权参考示例」https://docs.volcengine.com/docs/doubao-voice/old-version-console-authentication-reference-example
       （TTS V3 旧版头名与 ASR 不同：App-Id vs App-Key，按文档实现，未用旧版凭证联调）
"""
from __future__ import annotations

import os
import uuid


class VolcAuthMissing(RuntimeError):
    pass


def auth_headers(kind: str) -> dict[str, str]:
    api_key = os.environ.get("VOLC_API_KEY")
    if api_key:
        return {"X-Api-Key": api_key}
    app_id, token = os.environ.get("VOLC_APP_ID"), os.environ.get("VOLC_ACCESS_TOKEN")
    if app_id and token:
        if kind == "asr":
            return {"X-Api-App-Key": app_id, "X-Api-Access-Key": token}
        return {"X-Api-App-Id": app_id, "X-Api-Access-Key": token}
    raise VolcAuthMissing("未设置 VOLC_API_KEY，也未同时设置 VOLC_APP_ID 与 VOLC_ACCESS_TOKEN")


def asr_headers(resource_id: str) -> dict[str, str]:
    h = auth_headers("asr")
    h.update({"X-Api-Resource-Id": resource_id, "X-Api-Request-Id": str(uuid.uuid4()),
              "X-Api-Connect-Id": str(uuid.uuid4())})
    return h


def tts_headers(resource_id: str) -> dict[str, str]:
    h = auth_headers("tts")
    h.update({"X-Api-Resource-Id": resource_id, "X-Api-Connect-Id": str(uuid.uuid4())})
    return h


def redact(headers: dict[str, str]) -> dict[str, str]:
    secret = {"X-Api-Key", "X-Api-Access-Key", "X-Api-App-Key", "X-Api-App-Id"}
    return {k: ("***" if k in secret else v) for k, v in headers.items()}
