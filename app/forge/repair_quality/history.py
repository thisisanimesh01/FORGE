"""Phase 12 M6: Bounded SQLite Historical Reliability Storage.

Provides bounded, thread-safe persistence and querying of historical repair
quality records, strategy success rates, and repeated failure patterns.
Strictly bounded by configurable record counts and time windows.
"""

from datetime import datetime, timezone, timedelta
import json
import logging
from pathlib import Path
import sqlite3
import threading
from typing import Any, Dict, List, Optional

from forge.repair_quality.schemas import MAX_HISTORY_RECORDS

logger = logging.getLogger(__name__)


class HistoricalReliabilityStore:
    """Thread-safe SQLite store for bounded historical repair reliability."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or Path(".forge") / "repair_quality_history.db"
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database schema if not already present."""
        if self.db_path != Path(":memory:"):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            with self._lock, sqlite3.connect(str(self.db_path), timeout=10.0) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS repair_quality_records (
                        request_id TEXT PRIMARY KEY,
                        repair_plan_id TEXT,
                        execution_id TEXT,
                        strategy_id TEXT,
                        target_files TEXT,
                        quality_status TEXT,
                        overall_score REAL,
                        gate_decision TEXT,
                        risk_level TEXT,
                        regressions_count INTEGER DEFAULT 0,
                        timestamp TEXT,
                        metadata_json TEXT
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_rq_strategy ON repair_quality_records(strategy_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_rq_timestamp ON repair_quality_records(timestamp)")
                conn.commit()
        except sqlite3.DatabaseError as e:
            logger.warning(f"Failed to initialize HistoricalReliabilityStore: {e}")

    def record_quality_result(
        self,
        request_id: str,
        repair_plan_id: str,
        execution_id: str,
        strategy_id: str,
        target_files: List[str],
        quality_status: str,
        overall_score: Optional[float],
        gate_decision: str,
        risk_level: str,
        regressions_count: int = 0,
        timestamp: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Persist a quality evaluation record with bounded table size."""
        ts = timestamp or datetime.now(timezone.utc).isoformat()
        meta_str = json.dumps(metadata or {}, sort_keys=True, default=str)
        files_str = json.dumps(target_files)

        try:
            with self._lock, sqlite3.connect(str(self.db_path), timeout=10.0) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO repair_quality_records (
                        request_id, repair_plan_id, execution_id, strategy_id,
                        target_files, quality_status, overall_score, gate_decision,
                        risk_level, regressions_count, timestamp, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_id,
                        repair_plan_id,
                        execution_id,
                        strategy_id,
                        files_str,
                        quality_status,
                        overall_score,
                        gate_decision,
                        risk_level,
                        regressions_count,
                        ts,
                        meta_str,
                    ),
                )
                # Bound table size: keep newest MAX_HISTORY_RECORDS
                conn.execute(
                    f"""
                    DELETE FROM repair_quality_records
                    WHERE request_id NOT IN (
                        SELECT request_id FROM repair_quality_records
                        ORDER BY timestamp DESC
                        LIMIT {MAX_HISTORY_RECORDS}
                    )
                    """
                )
                conn.commit()
                return True
        except sqlite3.DatabaseError as e:
            logger.warning(f"Failed to record quality result: {e}")
            return False

    def get_history(
        self,
        strategy_id: Optional[str] = None,
        target_file: Optional[str] = None,
        limit: int = 100,
        max_age_days: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Query historical quality evaluation records."""
        clamped_limit = min(max(1, limit), MAX_HISTORY_RECORDS)
        query = "SELECT * FROM repair_quality_records WHERE 1=1"
        params: List[Any] = []

        if strategy_id:
            query += " AND strategy_id = ?"
            params.append(strategy_id)

        if target_file:
            query += " AND target_files LIKE ?"
            params.append(f"%{target_file}%")

        if max_age_days:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
            query += " AND timestamp >= ?"
            params.append(cutoff)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(clamped_limit)

        results: List[Dict[str, Any]] = []
        try:
            with self._lock, sqlite3.connect(str(self.db_path), timeout=10.0) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(query, params)
                for row in cursor.fetchall():
                    row_dict = dict(row)
                    try:
                        row_dict["target_files"] = json.loads(row_dict["target_files"])
                    except Exception:
                        row_dict["target_files"] = []
                    try:
                        row_dict["metadata"] = json.loads(row_dict["metadata_json"])
                    except Exception:
                        row_dict["metadata"] = {}

                    # Exact membership check in case substring matched another path
                    if target_file and target_file not in row_dict["target_files"]:
                        continue

                    results.append(row_dict)
        except sqlite3.DatabaseError as e:
            logger.warning(f"Failed to query history store: {e}")

        return results

    def get_strategy_stats(
        self,
        strategy_id: str,
        max_age_days: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Aggregate statistical performance metrics for a repair strategy."""
        query = """
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN quality_status = 'passed' THEN 1 ELSE 0 END) as passed_count,
                SUM(CASE WHEN quality_status = 'failed' THEN 1 ELSE 0 END) as failed_count,
                SUM(CASE WHEN regressions_count > 0 THEN 1 ELSE 0 END) as regressions_count,
                AVG(overall_score) as avg_score
            FROM repair_quality_records
            WHERE strategy_id = ?
        """
        params: List[Any] = [strategy_id]
        if max_age_days:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
            query += " AND timestamp >= ?"
            params.append(cutoff)

        try:
            with self._lock, sqlite3.connect(str(self.db_path), timeout=10.0) as conn:
                cursor = conn.execute(query, params)
                row = cursor.fetchone()
                total = row[0] or 0
                passed = row[1] or 0
                failed = row[2] or 0
                regressions = row[3] or 0
                avg_score = row[4] if row[4] is not None else 0.0

                return {
                    "strategy_id": strategy_id,
                    "total_attempts": total,
                    "passed_count": passed,
                    "failed_count": failed,
                    "regressions_count": regressions,
                    "success_rate": (passed / total) if total > 0 else 0.0,
                    "avg_score": round(avg_score, 4),
                }
        except sqlite3.DatabaseError as e:
            logger.warning(f"Failed to calculate strategy stats: {e}")
            return {
                "strategy_id": strategy_id,
                "total_attempts": 0,
                "passed_count": 0,
                "failed_count": 0,
                "regressions_count": 0,
                "success_rate": 0.0,
                "avg_score": 0.0,
            }
