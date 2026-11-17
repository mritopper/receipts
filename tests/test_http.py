"""HTTP-level tests for the Token Factory and Tavily clients, against in-process fakes of both APIs."""
import asyncio
import json

import httpx

from receipts.config import Settings
from receipts.llm import Ledger, LLMError, TokenFactory
from receipts.search import SearchError, Tavily

MODELS = {"data": [
    {"id": "Qwen/Qwen3-30B-A3B-Instruct-2507", "pricing": {"prompt": "0.0000001", "completion": "0.0000003"}},
    {"id": "Qwen/Qwen3-235B-A22B-Instruct-2507", "pricing": {"prompt": "0.0000002", "completion": "0.0000006"}},
    {"id": "meta-llama/Llama-3.3-70B-Instruct", "pricing": {"prompt": "0.00000013", "completion": "0.0000004"}},
    {"id": "openai/gpt-oss-120b"},
]}


def settings(**kw) -> Settings:
    s = Settings(nebius_api_key="k", tavily_api_key="t", use_cache=False,
                 base_urls=("https://a.example/v1", "https://b.example/v1"))
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def tf_client(handler, **kw) -> TokenFactory:
    return TokenFactory(settings(**kw), http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def ok(content="{}", usage=None, **msg):
    usage = usage or {"prompt_tokens": 1000, "completion_tokens": 200}
    return httpx.Response(200, json={"choices": [{"message": {"content": content, **msg}}], "usage": usage})


def run(coro):
    return asyncio.run(coro)


def test_discovery_assigns_roles_and_keeps_checker_independent():
    def h(req):
        assert req.headers["authorization"] == "Bearer k"
        return httpx.Response(200, json=MODELS) if req.url.path.endswith("/models") else ok()

    tf = tf_client(h)
    m = run(tf.discover())
    assert m["fast"] == "Qwen/Qwen3-30B-A3B-Instruct-2507"
    assert m["strong"] == "Qwen/Qwen3-235B-A22B-Instruct-2507"
    assert m["check"] == "meta-llama/Llama-3.3-70B-Instruct" and m["check"] != m["strong"]
    assert tf.price("strong") == (2e-07, 6e-07)


def test_overrides_and_fallbacks():
    tf = tf_client(lambda req: httpx.Response(500), model_overrides={"strong": "my/model"})
    m = run(tf.discover())
    assert m["strong"] == "my/model" and m["fast"] == "meta-llama/Llama-3.3-70B-Instruct"
    assert tf.price("fast")[0] > 0


def test_cost_accounting_and_reasoning_stripped():
    def h(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json=MODELS)
        body = json.loads(req.content)
        assert body["chat_template_kwargs"] == {"enable_thinking": False}  # Qwen3: thinking off
        return ok("<think>hidden</think>```json\n{\"x\": 1}\n```",
                  usage={"prompt_tokens": 1_000_000, "completion_tokens": 100_000})

    tf, led = tf_client(h), Ledger()
    data, rec = run(tf.chat_json("strong", [{"role": "user", "content": "hi"}], role="t", ledger=led, stage="write"))
    assert data == {"x": 1}
    assert abs(rec.cost_usd - (0.20 + 0.06)) < 1e-9 and rec.stage == "write"
    assert led.summary()["llm_calls"] == 1


def test_drops_reasoning_switch_if_rejected_and_remembers():
    seen = []

    def h(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json=MODELS)
        body = json.loads(req.content)
        seen.append("chat_template_kwargs" in body)
        if "chat_template_kwargs" in body:
            return httpx.Response(400, json={"error": "unknown field"})
        return ok('{"a": 1}')

    tf, led = tf_client(h), Ledger()
    run(tf.chat("fast", [{"role": "user", "content": "x"}], role="t", ledger=led))
    run(tf.chat("fast", [{"role": "user", "content": "x"}], role="t", ledger=led))
    assert seen == [True, False, False]


def test_retries_429_and_falls_over_to_second_endpoint(monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    calls = {"n": 0}

    def h(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json=MODELS)
        calls["n"] += 1
        return httpx.Response(429) if calls["n"] < 3 else ok("fine")

    text, _ = run(tf_client(h).chat("fast", [{"role": "user", "content": "x"}], role="t", ledger=Ledger()))
    assert text == "fine" and calls["n"] == 3

    hosts = []

    def h2(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json=MODELS)
        hosts.append(req.url.host)
        return httpx.Response(404) if req.url.host == "a.example" else ok("hello")

    text, _ = run(tf_client(h2).chat("check", [{"role": "user", "content": "x"}], role="t", ledger=Ledger()))
    assert text == "hello" and "b.example" in hosts


def test_chat_json_retries_once_then_raises():
    replies = iter(["I think yes", '{"ok": true}'])

    def h(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json=MODELS)
        return ok(next(replies))

    data, rec = run(tf_client(h).chat_json("strong", [{"role": "user", "content": "x"}], role="t", ledger=Ledger()))
    assert data == {"ok": True} and rec.note == "json-retry"
    tf2 = tf_client(lambda req: httpx.Response(200, json=MODELS) if req.url.path.endswith("/models") else ok("never json"))
    try:
        run(tf2.chat_json("strong", [{"role": "user", "content": "x"}], role="t", ledger=Ledger()))
        raise AssertionError("expected LLMError")
    except LLMError:
        pass


def test_tavily_request_shape_and_errors(monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    seen = {}

    def h(req):
        seen.update(json.loads(req.content))
        assert req.headers["authorization"] == "Bearer t"
        return httpx.Response(200, json={"results": [{"url": "https://x.org", "raw_content": "abc"}], "usage": {"credits": 2}})

    tv = Tavily(settings(), http=httpx.AsyncClient(transport=httpx.MockTransport(h)))
    res, credits, cached = run(tv.search("q", depth="advanced", time_range="month"))
    assert res[0]["url"] == "https://x.org" and credits == 2 and not cached
    assert seen["include_raw_content"] == "text" and seen["include_answer"] is False and seen["time_range"] == "month"

    tv_down = Tavily(settings(), http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))))
    try:
        run(tv_down.search("q"))
        raise AssertionError("expected SearchError")
    except SearchError:
        pass
