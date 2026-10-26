from src.search import find_element

def test_find_element():
    assert find_element([10, 20, 30], 20) == 1
    assert find_element([10, 20, 30], 99) == -1
    assert find_element([], 1) == -1
