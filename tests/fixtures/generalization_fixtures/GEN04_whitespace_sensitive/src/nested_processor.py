class NestedProcessor:
    def process_record(self, record: dict) -> dict:
        """Process record with nested status and tier checks."""
        if record.get("active"):
            tier = record.get("tier", "standard")
            if tier == "vip":
                # Defect: 16 spaces of indentation on statement
                record["rate"] = 0.50  # VIP rate should be 0.20
        return record
