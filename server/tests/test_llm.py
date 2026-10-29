import asyncio
import json

import httpx

from hidagent.llm import LLM


def fake_server(llm: LLM, wants: str) -> list[dict]:
    """Point the client at an in-memory endpoint that only accepts the `wants` token parameter."""
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if wants not in body:
            return httpx.Response(400, json={"error": {"message": f"Unsupported parameter. Use '{wants}' instead."}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "DO: wait"}}]})

    llm._http = httpx.AsyncClient(base_url="http://test/v1", transport=httpx.MockTransport(handle))
    return seen


def test_classic_servers_get_max_tokens_once():
    llm = LLM("http://test/v1", "qwen")
    seen = fake_server(llm, "max_tokens")
    assert asyncio.run(llm.chat("sys", "hi")) == "DO: wait"
    assert len(seen) == 1 and seen[0]["max_tokens"] == 600


def test_auto_switches_to_max_completion_tokens_and_remembers():
    llm = LLM("http://test/v1", "gpt")
    seen = fake_server(llm, "max_completion_tokens")
    asyncio.run(llm.chat("sys", "hi"))
    asyncio.run(llm.chat("sys", "again"))
    assert [("max_tokens" in b, "max_completion_tokens" in b) for b in seen] == [(True, False), (False, True),
                                                                                 (False, True)]


def test_explicit_token_param_is_used_directly():
    llm = LLM("http://test/v1", "gpt", token_param="max_completion_tokens")
    seen = fake_server(llm, "max_completion_tokens")
    asyncio.run(llm.chat("sys", "hi"))
    assert len(seen) == 1


def test_extra_body_and_image_are_sent():
    llm = LLM("http://test/v1", "gpt", extra={"reasoning_effort": "none"})
    seen = fake_server(llm, "max_tokens")
    asyncio.run(llm.chat("sys", "hi", image=b"\xff\xd8jpeg"))
    body = seen[0]
    assert body["reasoning_effort"] == "none"
    assert body["messages"][1]["content"][0]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_temperature_is_dropped_for_models_that_only_take_the_default():
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if "max_completion_tokens" not in body:
            return httpx.Response(400, json={"error": {"message": "Use 'max_completion_tokens' instead."}})
        if "temperature" in body:
            return httpx.Response(400, json={"error": {"message": "Unsupported value: 'temperature' does not "
                                                                  "support 0 with this model."}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "DO: wait"}}]})

    llm = LLM("http://test/v1", "gpt-6")
    llm._http = httpx.AsyncClient(base_url="http://test/v1", transport=httpx.MockTransport(handle))
    assert asyncio.run(llm.chat("sys", "hi")) == "DO: wait"
    assert asyncio.run(llm.chat("sys", "again", temperature=0.4)) == "DO: wait"
    assert len(seen) == 4 and "temperature" not in seen[-1]  # learned once, then sent right the first time


def test_an_answer_lost_to_reasoning_is_asked_again_with_more_room():
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body["max_tokens"])
        if body["max_tokens"] < 1000:
            return httpx.Response(200, json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "DO: home"}, "finish_reason": "stop"}]})

    llm = LLM("http://test/v1", "r1")
    llm._http = httpx.AsyncClient(base_url="http://test/v1", transport=httpx.MockTransport(handle))
    assert asyncio.run(llm.chat("sys", "hi")) == "DO: home"
    assert seen == [600, 2400]
