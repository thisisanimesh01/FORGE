import unittest
from src.backoff import BackoffPolicy


class TestBackoffPolicy(unittest.TestCase):
    def test_exponential_progression(self):
        """Verify exponential backoff progression 1.0, 2.0, 4.0, 8.0."""
        policy = BackoffPolicy(base_delay=1.0, max_delay=60.0)
        self.assertAlmostEqual(policy.compute_delay(0), 1.0)
        self.assertAlmostEqual(policy.compute_delay(1), 2.0)
        self.assertAlmostEqual(policy.compute_delay(2), 4.0)
        self.assertAlmostEqual(policy.compute_delay(3), 8.0)

    def test_max_delay_cap(self):
        """Verify calculated delay never exceeds max_delay."""
        policy = BackoffPolicy(base_delay=1.0, max_delay=10.0)
        self.assertAlmostEqual(policy.compute_delay(10), 10.0)

    def test_negative_attempt_rejected(self):
        """Verify negative attempts raise ValueError."""
        policy = BackoffPolicy(base_delay=1.0, max_delay=60.0)
        with self.assertRaises(ValueError):
            policy.compute_delay(-1)


if __name__ == "__main__":
    unittest.main()
