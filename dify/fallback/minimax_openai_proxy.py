#!/usr/bin/env python3
"""
备选方案：把 MiniMax 包成一个「标准 OpenAI 服务」，让 Dify 用官方 OpenAI 供应商插件直连。

什么时候需要它？
  - 插件市场装不上 openai_api_compatible（本环境实测可以装，故默认不走这条路）；
  - 或者你只想用 Dify 内置的 OpenAI 供应商、不想装第三方插件。

它做什么？
  - GET  /v1/models            → 返回 MiniMax 模型列表
  - POST /v1/chat/completions  → 转发到 MiniMax，并把响应**规整成标准 OpenAI 结构**
     · MiniMax 推理模型会返回 reasoning_content：这里把它丢掉，只保留 content
     · MiniMax 要求 max_tokens 足够大，否则 content 为空：这里给一个下限兜底
     · 支持 stream=true → 转成标准 SSE（data: {...}\\n\\n，以 [DONE] 收尾）

依赖：fastapi, uvicorn, httpx
运行：
  export MINIMAX_API_KEY=<your-key>
  python3 -m uvicorn minimax_openai_proxy:app --host 0.0.0.0 --port 8100
在 Dify 里：供应商=OpenAI，Base URL=http://<host>:8100/v1，模型=MiniMax-M3.1-Flash
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

UPSTREAM = os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.chat/v1").rstrip("/")
API_KEY = os.environ.get("MINIMAX_API_KEY", "")
DEFAULT_MODEL = os.environ.get("MINIMAX_MODEL", "MiniMax-M3.1-Flash")
# 推理模型先烧 reasoning tokens；给太小会 content 为空、finish_reason=length
MIN_MAX_TOKENS = int(os.environ.get("MINIMAX_MIN_MAX_TOKENS", "2048"))

app = FastAPI(title="MiniMax → OpenAI compatible proxy")


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}


def _prepare(body: dict[str, Any]) -> dict[str, Any]:
    """把 OpenAI 请求体规整成 MiniMax 能吃的形状。"""
    body = dict(body)
    body.setdefault("model", DEFAULT_MODEL)
    # 兜底：推理模型必须给足 max_tokens
    mt = body.get("max_tokens") or body.get("max_completion_tokens") or 0
    if not isinstance(mt, int) or mt < MIN_MAX_TOKENS:
        body["max_tokens"] = MIN_MAX_TOKENS
    body.pop("max_completion_tokens", None)
    # 去掉一些 MiniMax 不认的字段，避免 400
    for k in ("response_format", "seed", "logit_bias", "user", "parallel_tool_calls"):
        body.pop(k, None)
    return body


def _normalize(resp: dict[str, Any]) -> dict[str, Any]:
    """把 MiniMax 响应规整成标准 OpenAI ChatCompletion（丢弃 reasoning_content）。"""
    for ch in resp.get("choices") or []:
        msg = ch.get("message") or {}
        msg.pop("reasoning_content", None)          # 关键：不要把思考过程暴露给 Dify
        if "content" not in msg:
            msg["content"] = ""
        ch["message"] = msg
        ch.setdefault("finish_reason", "stop")
    return resp


@app.get("/v1/models")
async def models() -> JSONResponse:
    async with httpx.AsyncClient(timeout=30) as c:
        try:
            r = await c.get(f"{UPSTREAM}/models", headers=_headers())
            data = r.json().get("data")
            if isinstance(data, list) and data:
                return JSONResponse({"object": "list", "data": data})
        except Exception:
            pass
    # 上游列不出时，至少给出配置的模型，保证 Dify 能选到
    return JSONResponse({"object": "list",
                         "data": [{"id": DEFAULT_MODEL, "object": "model", "owned_by": "minimax"}]})


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = _prepare(await request.json())
    stream = bool(body.get("stream"))
    body.pop("stream", None)  # 流式时上游也走非流式，代理自己切片，兼容性最好

    client = httpx.AsyncClient(timeout=None)
    r = await client.post(f"{UPSTREAM}/chat/completions", headers=_headers(), json=body)
    if r.status_code != 200:
        await client.aclose()
        return JSONResponse({"error": {"message": r.text, "type": "upstream_error"}},
                            status_code=r.status_code)
    resp = _normalize(r.json())

    if not stream:
        await client.aclose()
        return JSONResponse(resp)

    async def gen():
        # 一次性拿到整段，再按 OpenAI SSE 格式切片下发
        text = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
        head = {"id": resp.get("id"), "object": "chat.completion.chunk",
                "model": resp.get("model", DEFAULT_MODEL),
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
        yield f"data: {json.dumps(head, ensure_ascii=False)}\n\n"
        step = 60
        for i in range(0, len(text), step):
            chunk = {"id": resp.get("id"), "object": "chat.completion.chunk",
                     "model": resp.get("model", DEFAULT_MODEL),
                     "choices": [{"index": 0, "delta": {"content": text[i:i + step]}, "finish_reason": None}]}
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        tail = {"id": resp.get("id"), "object": "chat.completion.chunk",
                "model": resp.get("model", DEFAULT_MODEL),
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": resp.get("usage") or {}}
        yield f"data: {json.dumps(tail, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
        await client.aclose()

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/health")
async def health():
    return {"ok": True, "upstream": UPSTREAM,
            "model": DEFAULT_MODEL, "key_configured": bool(API_KEY)}
