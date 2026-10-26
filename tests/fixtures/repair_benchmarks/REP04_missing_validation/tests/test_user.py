import pytest
from src.user_input import allocate_slots

def test_allocate_slots():
    assert allocate_slots(3) == [0, 0, 0]
    with pytest.raises(ValueError, match="cannot be negative"):
        allocate_slots(-1)
