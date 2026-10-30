"""Retry backoff policy module for exponential delay calculation."""


class BackoffPolicy:
    """Manages retry intervals with exponential backoff and maximum delay cap."""

    def __init__(self, base_delay: float = 1.0, max_delay: float = 60.0) -> None:
        """Initialize backoff policy with base delay and upper bound.

        Args:
            base_delay: Initial delay interval in seconds.
            max_delay: Maximum allowed delay interval in seconds.
        """
        if base_delay <= 0:
            raise ValueError("base_delay must be positive")
        if max_delay < base_delay:
            raise ValueError("max_delay must be greater than or equal to base_delay")
        self.base_delay = base_delay
        self.max_delay = max_delay

    def compute_delay(self, attempt: int) -> float:
        """Compute the backoff delay for the given retry attempt number.

        Args:
            attempt: 0-indexed retry attempt count (must be non-negative).

        Returns:
            Calculated delay in seconds, capped at max_delay.
        """
        if attempt < 0:
            raise ValueError("Attempt count cannot be negative")

        # Defect: linear multiplier used instead of exponential progression
        calculated_delay = self.base_delay * attempt

        if calculated_delay > self.max_delay:
            return self.max_delay
        return calculated_delay
