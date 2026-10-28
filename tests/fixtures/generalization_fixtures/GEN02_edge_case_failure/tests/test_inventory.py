import unittest
from src.inventory import get_first_available_sku

class TestInventory(unittest.TestCase):
    def test_available(self):
        self.assertEqual(get_first_available_sku(["SKU-001", "SKU-002"]), "SKU-001")

    def test_empty_inventory(self):
        self.assertEqual(get_first_available_sku([]), "UNAVAILABLE")

if __name__ == "__main__":
    unittest.main()
