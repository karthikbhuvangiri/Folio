"""
llm_client.py
Local LLM client for Ollama-backed categorization, Mira, and receipt parsing.
"""

import base64
import contextvars
import json as _json
import os
import time
import httpx
from dotenv import load_dotenv
from log_config import get_logger
import local_llm

load_dotenv()

logger = get_logger(__name__)

_LLM_CALL_TRACE = contextvars.ContextVar("llm_call_trace", default=None)


def start_trace():
    """Start request-scoped LLM telemetry collection.

    The trace deliberately stores timings and token counts only, never prompt
    text or model output text.
    """

    return _LLM_CALL_TRACE.set([])


def finish_trace(token) -> list[dict]:
    calls = current_trace_calls()
    try:
        _LLM_CALL_TRACE.reset(token)
    except ValueError:
        # Starlette may advance/close sync streaming generators from different
        # worker contexts. Keep telemetry best-effort and never surface context
        # bookkeeping errors to the user after an answer has already streamed.
        _LLM_CALL_TRACE.set(None)
    return calls


def current_trace_calls() -> list[dict]:
    calls = _LLM_CALL_TRACE.get()
    return list(calls) if isinstance(calls, list) else []


def _record_llm_call(record: dict) -> None:
    calls = _LLM_CALL_TRACE.get()
    if isinstance(calls, list):
        calls.append(record)


def _ns_to_ms(value) -> float | None:
    try:
        return round(float(value) / 1_000_000, 2)
    except (TypeError, ValueError):
        return None


def _messages_char_count(messages: list[dict] | None, system: str | None = None) -> int:
    total = len(system or "")
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        total += len(str(msg.get("content") or ""))
    return total


def _ollama_timing_record(
    *,
    purpose: str,
    model: str,
    stream: bool,
    messages: list[dict] | None,
    system: str | None = None,
    max_tokens: int | None = None,
    started: float,
    first_token_ms: float | None = None,
    output_chars: int = 0,
    final_event: dict | None = None,
    error: str | None = None,
    think: bool | None = None,
) -> dict:
    event = final_event or {}
    return {
        "provider": "ollama",
        "purpose": purpose,
        "model": model,
        "stream": bool(stream),
        "max_tokens": max_tokens,
        "prompt_chars": _messages_char_count(messages, system),
        "output_chars": int(output_chars or 0),
        "first_token_ms": round(first_token_ms, 2) if first_token_ms is not None else None,
        "wall_ms": round((time.perf_counter() - started) * 1000, 2),
        "load_duration_ms": _ns_to_ms(event.get("load_duration")),
        "prompt_eval_duration_ms": _ns_to_ms(event.get("prompt_eval_duration")),
        "eval_duration_ms": _ns_to_ms(event.get("eval_duration")),
        "total_duration_ms": _ns_to_ms(event.get("total_duration")),
        "prompt_eval_count": event.get("prompt_eval_count"),
        "eval_count": event.get("eval_count"),
        "done_reason": event.get("done_reason"),
        "think": think,
        "error": error or None,
    }

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").strip().lower() or "ollama"
_TRUE_ENV_VALUES = {"1", "true", "yes", "on"}

# Ollama settings
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
OLLAMA_MODEL_CATEGORIZE = os.getenv("OLLAMA_MODEL_CATEGORIZE", "gemma4:e4b")
OLLAMA_MODEL_CONTROLLER = os.getenv("OLLAMA_MODEL_CONTROLLER", OLLAMA_MODEL_CATEGORIZE)
OLLAMA_MODEL_COPILOT = os.getenv("OLLAMA_MODEL_COPILOT", "gemma4:e4b")
OLLAMA_MODEL_RECEIPT = os.getenv("OLLAMA_MODEL_RECEIPT", "gemma4:e4b")
OLLAMA_MODEL_ADVISOR = os.getenv("MIRA_ADVISOR_LENS_MODEL", os.getenv("OLLAMA_MODEL_ADVISOR", "gemma4:e4b"))
LLAMACPP_BASE_URL = os.getenv("LLAMACPP_BASE_URL", "http://host.docker.internal:8081")
LLAMACPP_MODEL = os.getenv("LLAMACPP_MODEL", "local")
LLAMACPP_TIMEOUT = float(os.getenv("LLAMACPP_TIMEOUT", os.getenv("OLLAMA_TIMEOUT_COPILOT", "240")))
LLAMACPP_TEMPERATURE = float(os.getenv("LLAMACPP_TEMPERATURE", "1.0"))
LLAMACPP_TOP_P = float(os.getenv("LLAMACPP_TOP_P", "0.95"))
LLAMACPP_TOP_K = int(os.getenv("LLAMACPP_TOP_K", "64"))
LLAMACPP_THINK = os.getenv("LLAMACPP_THINK", "false").strip().lower() in {"1", "true", "yes", "on"}
# Timeouts are generous by default — local inference on a laptop is slow,
# especially categorization batches of 50 transactions.
# Increase further via env if your hardware is particularly slow.
_OLLAMA_TIMEOUT_CATEGORIZE = float(os.getenv("OLLAMA_TIMEOUT_CATEGORIZE", "600"))  # 10 min
_OLLAMA_TIMEOUT_CONTROLLER = float(os.getenv("OLLAMA_TIMEOUT_CONTROLLER", "90"))    # 1.5 min
_OLLAMA_TIMEOUT_COPILOT = float(os.getenv("OLLAMA_TIMEOUT_COPILOT", "240"))        # 4 min
_OLLAMA_TIMEOUT_ADVISOR = float(os.getenv("MIRA_ADVISOR_LENS_TIMEOUT", os.getenv("OLLAMA_TIMEOUT_ADVISOR", "600")))
_OLLAMA_CONTROLLER_KEEP_ALIVE = os.getenv(
    "OLLAMA_CONTROLLER_KEEP_ALIVE",
    os.getenv("OLLAMA_PREWARM_KEEP_ALIVE", "15m"),
).strip() or "15m"
_OLLAMA_COPILOT_KEEP_ALIVE = os.getenv(
    "OLLAMA_COPILOT_KEEP_ALIVE",
    os.getenv("OLLAMA_PREWARM_KEEP_ALIVE", "15m"),
).strip() or "15m"
_OLLAMA_ADVISOR_KEEP_ALIVE = os.getenv("MIRA_ADVISOR_LENS_KEEP_ALIVE", os.getenv("OLLAMA_ADVISOR_KEEP_ALIVE", "2m")).strip() or "2m"
_OLLAMA_ADVISOR_THINK = os.getenv("MIRA_ADVISOR_LENS_THINK", "0").strip().lower() in _TRUE_ENV_VALUES


def is_available() -> bool:
    """Return True when a local LLM base URL is configured."""
    provider = get_provider()
    if provider == "llamacpp":
        return bool(get_llamacpp_config()["base_url"])
    return bool(get_ollama_config()["base_url"])


def get_provider() -> str:
    try:
        return local_llm.get_provider()
    except Exception:
        return "llamacpp" if LLM_PROVIDER == "llamacpp" else "ollama"


def get_ollama_config() -> dict:
    try:
        return local_llm.get_ollama_config()
    except Exception:
        return {
            "base_url": OLLAMA_BASE_URL,
            "categorize_model": OLLAMA_MODEL_CATEGORIZE,
            "controller_model": OLLAMA_MODEL_CONTROLLER,
            "copilot_model": OLLAMA_MODEL_COPILOT,
            "advisor_model": OLLAMA_MODEL_ADVISOR,
        }


def get_llamacpp_config() -> dict:
    try:
        return local_llm.get_llamacpp_config()
    except Exception:
        return {
            "base_url": LLAMACPP_BASE_URL,
            "model": LLAMACPP_MODEL,
        }


def _model_for_purpose(ollama_config: dict, purpose: str) -> str:
    if purpose == "categorize":
        return ollama_config.get("categorize_model") or OLLAMA_MODEL_CATEGORIZE
    if purpose == "controller":
        return (
            ollama_config.get("controller_model")
            or ollama_config.get("categorize_model")
            or OLLAMA_MODEL_CONTROLLER
        )
    if purpose == "advisor":
        return ollama_config.get("advisor_model") or OLLAMA_MODEL_ADVISOR
    return ollama_config.get("copilot_model") or OLLAMA_MODEL_COPILOT


def _timeout_for_purpose(purpose: str) -> float:
    if purpose == "categorize":
        return _OLLAMA_TIMEOUT_CATEGORIZE
    if purpose == "controller":
        return _OLLAMA_TIMEOUT_CONTROLLER
    if purpose == "advisor":
        return _OLLAMA_TIMEOUT_ADVISOR
    return _OLLAMA_TIMEOUT_COPILOT


def _keep_alive_for_purpose(purpose: str) -> str | None:
    if purpose == "controller":
        return _OLLAMA_CONTROLLER_KEEP_ALIVE
    if purpose == "advisor":
        return _OLLAMA_ADVISOR_KEEP_ALIVE
    if purpose == "copilot":
        return _OLLAMA_COPILOT_KEEP_ALIVE
    return None


def complete(
    prompt: str,
    max_tokens: int = 1024,
    purpose: str = "copilot",
    response_format=None,
) -> str:
    """
    Send a prompt to the configured LLM and return the response text.

    Args:
        prompt:     The user message content.
        max_tokens: Maximum tokens to generate.
        purpose:    "categorize", "controller", "copilot", or "advisor" selects the local Ollama model.

    Returns:
        Stripped response text from the model.

    Raises:
        Exception on API or network errors.
    """
    if get_provider() == "llamacpp" and purpose in {"controller", "copilot"}:
        return _complete_llamacpp(prompt, max_tokens)
    return _complete_ollama(prompt, max_tokens, purpose, response_format=response_format)


def complete_stream(prompt: str, max_tokens: int = 1024, purpose: str = "copilot"):
    """
    Stream plain chat completion text chunks from the configured local LLM.
    """
    if get_provider() == "llamacpp" and purpose in {"controller", "copilot"}:
        yield from _complete_stream_llamacpp(prompt, max_tokens)
        return
    yield from _complete_stream_ollama(prompt, max_tokens, purpose)


def complete_vision(
    prompt: str,
    image_bytes: bytes,
    max_tokens: int = 2048,
    purpose: str = "copilot",
    mime_type: str | None = None,
) -> tuple[str, str]:
    """
    Send one image plus text to the configured local vision model.
    Returns (response_text, model_name). Receipt parsing is intentionally local-only.
    """
    return _complete_ollama_vision(prompt, image_bytes, max_tokens, purpose, mime_type)


def chat_with_tools(
    messages: list[dict],
    tools: list[dict],
    system: str | None = None,
    max_tokens: int = 2048,
    purpose: str = "copilot",
) -> dict:
    """
    Tool-capable chat through local Ollama.

    Args:
        messages: list of {"role": "user"|"assistant"|"tool", "content": str,
                   "tool_calls": [{"id","name","args"}]?, "tool_call_id": str?}
        tools:    registry-agnostic tool schemas (see copilot_tools module)
        system:   optional system prompt

    Returns:
        {"content": str, "tool_calls": [{"id","name","args"}], "stop_reason": str}
        If tool_calls is non-empty, the caller should execute each and append a
        tool-role message before calling again.
    """
    if get_provider() == "llamacpp" and purpose == "copilot":
        return _chat_with_tools_llamacpp(messages, tools, system, max_tokens)
    return _chat_with_tools_ollama(messages, tools, system, max_tokens, purpose)


def _chat_with_tools_ollama(
    messages: list[dict],
    tools: list[dict],
    system: str | None,
    max_tokens: int,
    purpose: str,
) -> dict:
    import copilot_tools
    if tools and isinstance(tools[0], str):
        ollama_tools = copilot_tools.tools_for_ollama(tools)
    else:
        ollama_tools = tools or []
    ollama_config = get_ollama_config()
    model = _model_for_purpose(ollama_config, purpose)
    timeout = _timeout_for_purpose(purpose)

    ollama_msgs = []
    if system:
        ollama_msgs.append({"role": "system", "content": system})
    for msg in messages:
        role = msg.get("role")
        if role == "tool":
            ollama_msgs.append({
                "role": "tool",
                "content": msg.get("content") or "",
                "tool_call_id": msg.get("tool_call_id"),
            })
        elif role == "assistant" and msg.get("tool_calls"):
            ollama_msgs.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {
                            "name": c["name"],
                            "arguments": c.get("args") or {},
                        },
                    }
                    for c in msg["tool_calls"]
                ],
            })
        else:
            ollama_msgs.append({"role": role, "content": msg.get("content") or ""})

    payload = {
        "model": model,
        "messages": ollama_msgs,
        "stream": False,
        "think": False,
        "options": {"num_predict": max_tokens, "temperature": 0},
    }
    keep_alive = _keep_alive_for_purpose(purpose)
    if keep_alive:
        payload["keep_alive"] = keep_alive
    if ollama_tools:
        payload["tools"] = ollama_tools

    url = f"{ollama_config['base_url'].rstrip('/')}/api/chat"
    started = time.perf_counter()
    result = {}
    try:
        resp = httpx.post(url, json=payload, timeout=timeout)
        result = resp.json()
        if "message" not in result:
            raise Exception(f"Ollama tool API error: {result}")
        message = result["message"]
    except Exception as exc:
        _record_llm_call(_ollama_timing_record(
            purpose=purpose,
            model=model,
            stream=False,
            messages=messages,
            system=system,
            max_tokens=max_tokens,
            started=started,
            final_event=result if isinstance(result, dict) else {},
            error=str(exc),
        ))
        raise

    tool_calls = []
    for idx, call in enumerate(message.get("tool_calls") or []):
        fn = call.get("function") or {}
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = _json.loads(args)
            except Exception:
                args = {}
        tool_calls.append({
            "id": call.get("id") or f"call_{idx}",
            "name": fn.get("name") or "",
            "args": args,
        })

    content = (message.get("content") or "").strip()
    _record_llm_call(_ollama_timing_record(
        purpose=purpose,
        model=model,
        stream=False,
        messages=messages,
        system=system,
        max_tokens=max_tokens,
        started=started,
        output_chars=len(content),
        final_event=result,
    ))
    return {
        "content": content,
        "tool_calls": tool_calls,
        "stop_reason": result.get("done_reason") or "stop",
    }


def chat_with_tools_stream(
    messages: list[dict],
    tools: list[dict],
    system: str | None = None,
    max_tokens: int = 2048,
    purpose: str = "copilot",
):
    """
    Generator yielding ("text", delta_text) | ("tool_call", dict) | ("stop", reason)
    for a single streaming local Ollama turn.
    """
    if get_provider() == "llamacpp" and purpose == "copilot":
        yield from _chat_with_tools_stream_llamacpp(messages, tools, system, max_tokens)
        return
    yield from _chat_with_tools_stream_ollama(messages, tools, system, max_tokens, purpose)


def _chat_with_tools_stream_ollama(messages, tools, system, max_tokens, purpose):
    import copilot_tools
    if tools and isinstance(tools[0], str):
        ollama_tools = copilot_tools.tools_for_ollama(tools)
    else:
        ollama_tools = tools or []
    ollama_config = get_ollama_config()
    model = _model_for_purpose(ollama_config, purpose)
    timeout = _timeout_for_purpose(purpose)

    ollama_msgs = []
    if system:
        ollama_msgs.append({"role": "system", "content": system})
    for msg in messages:
        role = msg.get("role")
        if role == "tool":
            ollama_msgs.append({
                "role": "tool",
                "content": msg.get("content") or "",
                "tool_call_id": msg.get("tool_call_id"),
            })
        elif role == "assistant" and msg.get("tool_calls"):
            ollama_msgs.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {"name": c["name"], "arguments": c.get("args") or {}},
                    }
                    for c in msg["tool_calls"]
                ],
            })
        else:
            ollama_msgs.append({"role": role, "content": msg.get("content") or ""})

    payload = {
        "model": model,
        "messages": ollama_msgs,
        "stream": True,
        "think": False,
        "options": {"num_predict": max_tokens, "temperature": 0},
    }
    keep_alive = _keep_alive_for_purpose(purpose)
    if keep_alive:
        payload["keep_alive"] = keep_alive
    if ollama_tools:
        payload["tools"] = ollama_tools

    url = f"{ollama_config['base_url'].rstrip('/')}/api/chat"
    started = time.perf_counter()
    first_token_ms = None
    output_chars = 0
    final_event = {}
    recorded = False
    try:
        with httpx.stream("POST", url, json=payload, timeout=timeout) as resp:
            call_idx = 0
            for raw_line in resp.iter_lines():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    event = _json.loads(line)
                except Exception:
                    continue
                message = event.get("message") or {}
                content = message.get("content")
                if content:
                    if first_token_ms is None:
                        first_token_ms = (time.perf_counter() - started) * 1000
                    output_chars += len(content)
                    yield ("text", content)
                for call in message.get("tool_calls") or []:
                    if first_token_ms is None:
                        first_token_ms = (time.perf_counter() - started) * 1000
                    fn = call.get("function") or {}
                    args = fn.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = _json.loads(args)
                        except Exception:
                            args = {}
                    yield ("tool_call", {
                        "id": call.get("id") or f"call_{call_idx}",
                        "name": fn.get("name") or "",
                        "args": args,
                    })
                    call_idx += 1
                if event.get("done"):
                    final_event = event
                    _record_llm_call(_ollama_timing_record(
                        purpose=purpose,
                        model=model,
                        stream=True,
                        messages=messages,
                        system=system,
                        max_tokens=max_tokens,
                        started=started,
                        first_token_ms=first_token_ms,
                        output_chars=output_chars,
                        final_event=final_event,
                    ))
                    recorded = True
                    yield ("stop", event.get("done_reason") or "stop")
    except Exception as exc:
        _record_llm_call(_ollama_timing_record(
            purpose=purpose,
            model=model,
            stream=True,
            messages=messages,
            system=system,
            max_tokens=max_tokens,
            started=started,
            first_token_ms=first_token_ms,
            output_chars=output_chars,
            final_event=final_event,
            error=str(exc),
        ))
        recorded = True
        raise
    finally:
        if not recorded and _LLM_CALL_TRACE.get() is not None:
            _record_llm_call(_ollama_timing_record(
                purpose=purpose,
                model=model,
                stream=True,
                messages=messages,
                system=system,
                max_tokens=max_tokens,
                started=started,
                first_token_ms=first_token_ms,
                output_chars=output_chars,
                final_event=final_event,
                error="stream_closed_before_done",
            ))


def _messages_for_openai(messages: list[dict], system: str | None) -> list[dict]:
    openai_msgs = []
    if system:
        openai_msgs.append({"role": "system", "content": system})
    for msg in messages:
        role = msg.get("role")
        if role == "tool":
            openai_msgs.append({
                "role": "tool",
                "content": msg.get("content") or "",
                "tool_call_id": msg.get("tool_call_id"),
            })
        elif role == "assistant" and msg.get("tool_calls"):
            openai_msgs.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {
                            "name": c["name"],
                            "arguments": _json.dumps(c.get("args") or {}),
                        },
                    }
                    for c in msg["tool_calls"]
                ],
            })
        else:
            openai_msgs.append({"role": role, "content": msg.get("content") or ""})
    return openai_msgs


def _llamacpp_payload(
    messages: list[dict],
    max_tokens: int,
    stream: bool,
    tools: list[dict] | None = None,
) -> dict:
    config = get_llamacpp_config()
    payload = {
        "messages": messages,
        "stream": stream,
        "max_tokens": max_tokens,
        "temperature": LLAMACPP_TEMPERATURE,
        "top_p": LLAMACPP_TOP_P,
        "top_k": LLAMACPP_TOP_K,
        "think": LLAMACPP_THINK,
    }
    model = (config.get("model") or "").strip()
    if model and model != "local":
        payload["model"] = model
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    return payload


def _parse_openai_tool_calls(raw_calls: list[dict] | None) -> list[dict]:
    parsed = []
    for idx, call in enumerate(raw_calls or []):
        fn = call.get("function") or {}
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = _json.loads(args)
            except Exception:
                args = {}
        parsed.append({
            "id": call.get("id") or f"call_{idx}",
            "name": fn.get("name") or "",
            "args": args,
        })
    return parsed


def _chat_with_tools_llamacpp(
    messages: list[dict],
    tools: list[dict],
    system: str | None,
    max_tokens: int,
) -> dict:
    import copilot_tools
    openai_tools = copilot_tools.tools_for_ollama(tools) if tools and isinstance(tools[0], str) else tools or []
    config = get_llamacpp_config()
    payload = _llamacpp_payload(
        _messages_for_openai(messages, system),
        max_tokens=max_tokens,
        stream=False,
        tools=openai_tools,
    )
    url = f"{config['base_url'].rstrip('/')}/v1/chat/completions"
    resp = httpx.post(url, json=payload, timeout=LLAMACPP_TIMEOUT)
    result = resp.json()
    choices = result.get("choices") or []
    if not choices:
        raise Exception(f"llama.cpp API error: {result}")
    message = choices[0].get("message") or {}
    return {
        "content": (message.get("content") or "").strip(),
        "tool_calls": _parse_openai_tool_calls(message.get("tool_calls")),
        "stop_reason": choices[0].get("finish_reason") or "stop",
    }


def _chat_with_tools_stream_llamacpp(messages, tools, system, max_tokens):
    import copilot_tools
    openai_tools = copilot_tools.tools_for_ollama(tools) if tools and isinstance(tools[0], str) else tools or []
    config = get_llamacpp_config()
    payload = _llamacpp_payload(
        _messages_for_openai(messages, system),
        max_tokens=max_tokens,
        stream=True,
        tools=openai_tools,
    )
    url = f"{config['base_url'].rstrip('/')}/v1/chat/completions"
    buffered_tool_calls: dict[int, dict] = {}
    with httpx.stream("POST", url, json=payload, timeout=LLAMACPP_TIMEOUT) as resp:
        for raw_line in resp.iter_lines():
            line = raw_line.strip()
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                event = _json.loads(data)
            except Exception:
                continue
            choices = event.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            content = delta.get("content")
            if content:
                yield ("text", content)
            for call in delta.get("tool_calls") or []:
                idx = int(call.get("index") or 0)
                current = buffered_tool_calls.setdefault(idx, {
                    "id": call.get("id") or f"call_{idx}",
                    "name": "",
                    "arguments": "",
                })
                if call.get("id"):
                    current["id"] = call["id"]
                fn = call.get("function") or {}
                if fn.get("name"):
                    current["name"] += fn["name"]
                if fn.get("arguments"):
                    current["arguments"] += fn["arguments"]
            if choices[0].get("finish_reason"):
                break

    for idx, call in sorted(buffered_tool_calls.items()):
        try:
            args = _json.loads(call.get("arguments") or "{}")
        except Exception:
            args = {}
        yield ("tool_call", {
            "id": call.get("id") or f"call_{idx}",
            "name": call.get("name") or "",
            "args": args,
        })
    yield ("stop", "stop")


def _complete_llamacpp(prompt: str, max_tokens: int) -> str:
    config = get_llamacpp_config()
    payload = _llamacpp_payload(
        [{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        stream=False,
    )
    url = f"{config['base_url'].rstrip('/')}/v1/chat/completions"
    resp = httpx.post(url, json=payload, timeout=LLAMACPP_TIMEOUT)
    result = resp.json()
    choices = result.get("choices") or []
    if not choices:
        raise Exception(f"llama.cpp API error: {result}")
    message = choices[0].get("message") or {}
    return (message.get("content") or "").strip()


def _complete_stream_llamacpp(prompt: str, max_tokens: int):
    config = get_llamacpp_config()
    payload = _llamacpp_payload(
        [{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        stream=True,
    )
    url = f"{config['base_url'].rstrip('/')}/v1/chat/completions"
    with httpx.stream("POST", url, json=payload, timeout=LLAMACPP_TIMEOUT) as resp:
        for raw_line in resp.iter_lines():
            line = raw_line.strip()
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                event = _json.loads(data)
            except Exception:
                continue
            choices = event.get("choices") or []
            if not choices:
                continue
            content = (choices[0].get("delta") or {}).get("content")
            if content:
                yield content
            if choices[0].get("finish_reason"):
                break


def _complete_ollama(prompt: str, max_tokens: int, purpose: str, response_format=None) -> str:
    ollama_config = get_ollama_config()
    model = _model_for_purpose(ollama_config, purpose)
    timeout = _timeout_for_purpose(purpose)
    url = f"{ollama_config['base_url'].rstrip('/')}/api/chat"
    messages = [{"role": "user", "content": prompt}]
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"num_predict": max_tokens},
    }
    keep_alive = _keep_alive_for_purpose(purpose)
    if keep_alive:
        payload["keep_alive"] = keep_alive
    if response_format is not None:
        payload["format"] = response_format

    if purpose in {"categorize", "controller", "copilot"}:
        # Merchant enrichment, categorization, routing, and extraction are
        # deterministic extraction/translation tasks, so disable thinking
        # and randomness for faster, steadier output.
        payload["think"] = False
        payload["options"]["temperature"] = 0
    elif purpose == "advisor":
        payload["think"] = _OLLAMA_ADVISOR_THINK
        payload["options"]["temperature"] = 0

    started = time.perf_counter()
    result = {}
    try:
        resp = httpx.post(
            url,
            json=payload,
            timeout=timeout,
        )
        result = resp.json()
        if "message" not in result:
            raise Exception(f"Ollama API error: {result}")
        content = result["message"]["content"].strip()
        _record_llm_call(_ollama_timing_record(
            purpose=purpose,
            model=model,
            stream=False,
            messages=messages,
            max_tokens=max_tokens,
            started=started,
            output_chars=len(content),
            final_event=result,
            think=payload.get("think"),
        ))
        return content
    except Exception as exc:
        _record_llm_call(_ollama_timing_record(
            purpose=purpose,
            model=model,
            stream=False,
            messages=messages,
            max_tokens=max_tokens,
            started=started,
            final_event=result if isinstance(result, dict) else {},
            error=str(exc),
            think=payload.get("think"),
        ))
        raise


def _complete_stream_ollama(prompt: str, max_tokens: int, purpose: str):
    ollama_config = get_ollama_config()
    model = _model_for_purpose(ollama_config, purpose)
    timeout = _timeout_for_purpose(purpose)
    url = f"{ollama_config['base_url'].rstrip('/')}/api/chat"
    messages = [{"role": "user", "content": prompt}]
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "options": {"num_predict": max_tokens},
    }
    keep_alive = _keep_alive_for_purpose(purpose)
    if keep_alive:
        payload["keep_alive"] = keep_alive

    if purpose in {"categorize", "controller", "copilot"}:
        payload["think"] = False
        payload["options"]["temperature"] = 0
    elif purpose == "advisor":
        payload["think"] = _OLLAMA_ADVISOR_THINK
        payload["options"]["temperature"] = 0

    started = time.perf_counter()
    first_token_ms = None
    output_chars = 0
    final_event = {}
    recorded = False
    try:
        with httpx.stream("POST", url, json=payload, timeout=timeout) as resp:
            for raw_line in resp.iter_lines():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    event = _json.loads(line)
                except Exception:
                    continue
                content = (event.get("message") or {}).get("content")
                if content:
                    if first_token_ms is None:
                        first_token_ms = (time.perf_counter() - started) * 1000
                    output_chars += len(content)
                    yield content
                if event.get("done"):
                    final_event = event
                    _record_llm_call(_ollama_timing_record(
                        purpose=purpose,
                        model=model,
                        stream=True,
                        messages=messages,
                        max_tokens=max_tokens,
                        started=started,
                        first_token_ms=first_token_ms,
                        output_chars=output_chars,
                        final_event=final_event,
                        think=payload.get("think"),
                    ))
                    recorded = True
                    break
    except Exception as exc:
        _record_llm_call(_ollama_timing_record(
            purpose=purpose,
            model=model,
            stream=True,
            messages=messages,
            max_tokens=max_tokens,
            started=started,
            first_token_ms=first_token_ms,
            output_chars=output_chars,
            final_event=final_event,
            error=str(exc),
            think=payload.get("think"),
        ))
        recorded = True
        raise
    finally:
        if not recorded and _LLM_CALL_TRACE.get() is not None:
            _record_llm_call(_ollama_timing_record(
                purpose=purpose,
                model=model,
                stream=True,
                messages=messages,
                max_tokens=max_tokens,
                started=started,
                first_token_ms=first_token_ms,
                output_chars=output_chars,
                final_event=final_event,
                error="stream_closed_before_done",
                think=payload.get("think"),
            ))


def _complete_ollama_vision(
    prompt: str,
    image_bytes: bytes,
    max_tokens: int,
    purpose: str,
    mime_type: str | None = None,
) -> tuple[str, str]:
    ollama_config = get_ollama_config()
    preferred_model = _model_for_purpose(ollama_config, purpose)
    model = _select_ollama_vision_model(preferred_model, ollama_config)
    timeout = _timeout_for_purpose(purpose)
    url = f"{ollama_config['base_url'].rstrip('/')}/api/chat"
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "images": [image_b64],
            "content": prompt,
        }],
        "stream": False,
        "think": False,
        "options": {
            "num_predict": max_tokens,
            "temperature": 0,
        },
    }
    keep_alive = _keep_alive_for_purpose(purpose)
    if keep_alive:
        payload["keep_alive"] = keep_alive

    resp = httpx.post(url, json=payload, timeout=timeout)
    result = resp.json()
    if "message" not in result:
        raise Exception(f"Ollama vision API error: {result}")
    return result["message"]["content"].strip(), model


def _select_ollama_vision_model(preferred_model: str, ollama_config: dict) -> str:
    candidates = [
        preferred_model,
        ollama_config.get("copilot_model"),
        ollama_config.get("categorize_model"),
        OLLAMA_MODEL_RECEIPT,
    ]
    for candidate in candidates:
        model = (candidate or "").strip()
        family = model.lower()
        if model and ("gemma4" in family or "gemma3" in family):
            return model
    return OLLAMA_MODEL_RECEIPT
