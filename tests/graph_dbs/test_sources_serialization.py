"""sources 的序列化必须幂等，且读回来要能解到底。

历史 bug：写入路径同一份 metadata 被 json.dumps 两次，读取路径只认
`{...}` 形态且遇错就 break，于是每次重写都给 sources 再套一层转义。
实测某条 310 字符的会话被套到 18 层、落库 36MB。
"""

import json

from memos.graph_dbs.neo4j import Neo4jGraphDB, _prepare_node_metadata
from memos.utils import decode_json_string, serialize_sources


SAMPLE = {"type": "chat", "role": "user", "content": "低风险忽略"}


def _nested(value, layers):
    for _ in range(layers):
        value = json.dumps({"type": "doc", "content": value}, ensure_ascii=True)
    return value


def test_prepare_metadata_is_idempotent():
    metadata = {"sources": [dict(SAMPLE)]}

    once = _prepare_node_metadata(dict(metadata))
    twice = _prepare_node_metadata(dict(once))

    assert once["sources"] == twice["sources"]
    assert len(once["sources"][0]) == len(twice["sources"][0])


def test_serialize_sources_keeps_existing_strings():
    payload = json.dumps(SAMPLE, ensure_ascii=False)

    assert serialize_sources([payload]) == [payload]
    assert serialize_sources([dict(SAMPLE)]) == [payload]
    assert serialize_sources([]) is None
    assert serialize_sources(None) is None


def test_decode_json_string_unwraps_all_layers():
    nested = _nested(SAMPLE, layers=18)

    assert len(nested) > 1_000_000
    assert decode_json_string(nested) == SAMPLE


def test_decode_json_string_leaves_plain_text_alone():
    assert decode_json_string("低风险忽略") == "低风险忽略"
    assert decode_json_string("") == ""


def test_parse_node_decodes_sources_not_only_first_item():
    """第一项不是 {...} 形态时，后面的元素也必须继续解包。"""
    good = json.dumps(SAMPLE, ensure_ascii=False)
    nested = _nested(SAMPLE, layers=4)
    node = {"id": "m1", "memory": "x", "sources": [nested, good]}

    parsed = Neo4jGraphDB._parse_node(None, node)

    assert parsed["metadata"]["sources"] == [SAMPLE, SAMPLE]


def test_round_trip_does_not_grow():
    metadata = {"sources": [dict(SAMPLE)]}

    for _ in range(5):
        metadata = _prepare_node_metadata(metadata)
        parsed = Neo4jGraphDB._parse_node(
            None, {"id": "m1", "memory": "x", "sources": metadata["sources"]}
        )
        metadata = {"sources": parsed["metadata"]["sources"]}

    assert metadata["sources"] == [SAMPLE]
    assert len(json.dumps(metadata["sources"], ensure_ascii=False)) < 200
