from __future__ import annotations

from open_webui_gpt_researcher.openwebui import OpenWebUIClient


def test_retrieval_response_is_normalized() -> None:
    passages = OpenWebUIClient._parse_retrieval_response(
        [
            {
                "documents": [["first", "second"]],
                "metadatas": [[{"source": "a"}, {"source": "b"}]],
            }
        ]
    )
    assert [passage.text for passage in passages] == ["first", "second"]
    assert passages[1].metadata == {"source": "b"}
    assert OpenWebUIClient._parse_retrieval_response({"documents": []}) == []
