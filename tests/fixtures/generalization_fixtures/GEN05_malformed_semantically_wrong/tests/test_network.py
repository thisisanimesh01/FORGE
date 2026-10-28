import unittest
from src.network import validate_port_number

class TestNetwork(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(validate_port_number(8080))

    def test_invalid_port_raises(self):
        """Demonstrates defect: out-of-range port must raise ValueError."""
        with self.assertRaises(ValueError):
            validate_port_number(99999)

if __name__ == "__main__":
    unittest.main()
