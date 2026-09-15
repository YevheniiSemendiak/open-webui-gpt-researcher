from __future__ import annotations

from openwebui_functions.deep_research_pipe import Pipe


def test_pipe_extracts_question_sources_and_user_budget() -> None:
    pipe = Pipe()
    pipe.valves.default_searches = 20
    query = pipe._last_user_message({"messages": [{"role": "user", "content": "Investigate this"}]})
    assert query == "Investigate this"
    sources = pipe._sources(
        [
            {"type": "knowledge", "id": "kb-1", "name": "Knowledge"},
            {"type": "file", "file": {"id": "file-1", "filename": "data.pdf"}},
            {"type": "file", "file": {"id": "file-1", "filename": "data.pdf"}},
        ]
    )
    assert sources == [
        {"kind": "collection", "id": "kb-1", "name": "Knowledge"},
        {"kind": "file", "id": "file-1", "name": "data.pdf"},
    ]
    budget = pipe._budget({"max_searches": 7})
    assert budget["max_searches"] == 7
    assert budget["max_output_tokens"] == pipe.valves.default_output_tokens


async def test_pipe_requires_a_private_source_when_public_search_is_disabled() -> None:
    pipe = Pipe()
    pipe.valves.service_token = "configured"
    pipe.valves.public_search_enabled = False
    result = await pipe.pipe(
        {"messages": [{"role": "user", "content": "Investigate this"}]},
        {"id": "user-1"},
        __chat_id__="chat-1",
        __message_id__="message-1",
    )
    assert "Attach a file" in result
