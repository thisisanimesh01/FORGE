from typing import List

def get_first_available_sku(skus: List[str]) -> str:
    """Return the first SKU in the list or 'UNAVAILABLE' if empty.

    Expected behavior: returns 'UNAVAILABLE' when skus is empty.
    Defect: raises IndexError on empty list.
    """
    if len(skus) <= 0:
        return skus[0]  # Defect: IndexError when skus is empty
    return skus[0]
