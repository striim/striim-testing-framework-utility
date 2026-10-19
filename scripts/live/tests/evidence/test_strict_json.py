"""Strict payload comparison preserves JSON token types and refuses unsafe coercions."""
from decimal import Decimal

import pytest

from livetest import canon, exactdata


def decl(columns):
    return canon.declaration({"strict_json": True, "columns": columns}, db_route=False)


@pytest.mark.parametrize("actual", [1, True, None, "1"])
def test_json_token_type(actual):
    d = decl({"x": "json"})
    expected = [{"x": "1"}]
    assert canon.compare(expected, [{"x": actual}], d)["equal"] is (actual == 1 and not isinstance(actual, bool) and actual is not None)


def test_strict_text_and_number():
    d = decl({"n": "number", "s": "text", "b": "boolean"})
    assert canon.compare([{"n": "1.25", "s": "1", "b": "true"}],
                         [{"n": Decimal("1.25"), "s": "1", "b": True}], d)["equal"]
    with pytest.raises(canon.CanonError, match="invalid-value:actual:s"):
        canon.compare([{"s": "1"}], [{"s": 1}], d)


@pytest.mark.parametrize("value", ["text", 1, None, b"abc"])
def test_undeclared_is_refused(value):
    with pytest.raises(canon.CanonError, match="undeclared-column"):
        canon.compare([{"x": "text"}], [{"x": value}], decl({}))


@pytest.mark.parametrize("text", ['{"data":{"x":NaN}}', '{"data":{"x":Infinity}}',
                                  '{"data":{"x":-Infinity}}', '{"data":{"x":1},"data":{}}',
                                  '{"data":{"x":1}} garbage', '[{"data":{"x":1}}', '123'])
def test_strict_stream_refuses_invalid_json(text):
    with pytest.raises(canon.CanonError, match="invalid-json"):
        exactdata.parse_events(text, "rows", strict=True)


def test_strict_stream_keeps_types_and_all_events():
    events = exactdata.parse_events('[{"data":{"n":1.25,"s":"1","b":true,"nil":null}}]\n'
                                   '{"data":{"n":2}}', "rows", strict=True)
    assert events == [{"data": {"n": Decimal("1.25"), "s": "1", "b": True, "nil": None}}, {"data": {"n": 2}}]


def test_bytes_need_encoding_and_refuse_object_identity():
    d = decl({"x": "binary:base64"})
    assert canon.compare([{"x": "YWJj"}], [{"x": b"abc"}], d)["equal"]
    with pytest.raises(canon.CanonError, match="invalid-value:actual:x"):
        canon.compare([{"x": "YWJj"}], [{"x": "[B@1a2b"}], d)


@pytest.mark.parametrize("value", ["true", 1, None])
def test_strict_flag_is_boolean(value):
    with pytest.raises(canon.CanonError, match="invalid-declaration"):
        canon.declaration({"strict_json": value}, db_route=False)


def test_strict_flag_refused_on_database():
    with pytest.raises(canon.CanonError, match="invalid-declaration"):
        canon.declaration({"strict_json": True}, db_route=True)


def test_kafka_json_payload_keeps_key_and_value(monkeypatch):
    from livetest.kafkaadmin import KafkaAdmin
    admin = KafkaAdmin({})
    message = type("Message", (), {"key": lambda self: b'{"id":1}',
                                   "value": lambda self: b'{"n":1.25,"s":"1","b":true,"nil":null}'})()
    monkeypatch.setattr(admin, "_select", lambda topic, budget, decode, **kw: decode(message))
    assert admin.select_records("rows", strict_json=True, payload="json") == [
        {"key": {"id": 1}, "value": {"n": Decimal("1.25"), "s": "1", "b": True, "nil": None}}]
    message.value = lambda: b'{"n":NaN}'
    with pytest.raises(canon.CanonError, match="invalid-json"):
        admin.select_records("rows", strict_json=True, payload="json")


def test_number_does_not_round_long_tokens():
    value = Decimal("123456789012345678901234567890.123456789")
    assert canon.compare([{"n": str(value)}], [{"n": value}], decl({"n": "number"}))["equal"]
    assert not canon.compare([{"n": str(value)}], [{"n": value + Decimal("1")}], decl({"n": "number"}))["equal"]
