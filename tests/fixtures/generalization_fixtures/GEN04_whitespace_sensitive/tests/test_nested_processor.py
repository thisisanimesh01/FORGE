import unittest
from src.nested_processor import NestedProcessor

class TestNestedProcessor(unittest.TestCase):
    def test_vip_tier_rate(self):
        p = NestedProcessor()
        rec = p.process_record({"active": True, "tier": "vip"})
        self.assertEqual(rec.get("rate"), 0.20)

    def test_standard_tier_rate(self):
        p = NestedProcessor()
        rec = p.process_record({"active": True, "tier": "standard"})
        self.assertNotIn("rate", rec)

if __name__ == "__main__":
    unittest.main()
