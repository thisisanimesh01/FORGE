from src.parser import parse_tokens

def test_parse_tokens():
    res = parse_tokens("hello world")
    assert res == ["hello", "world"]
