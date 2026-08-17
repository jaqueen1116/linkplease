import sqlite3
import uuid
import json
from contextlib import contextmanager
from typing import List, Dict, Any, Optional
from app.config import DATABASE_PATH

@contextmanager
def get_db(db_path: Optional[str] = None):
    """
    Context manager for SQLite database connection.
    Enables WAL mode and foreign key constraints for concurrency and safety.
    """
    path = db_path or DATABASE_PATH
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db(db_path: Optional[str] = None):
    """
    Initialize SQLite database tables.
    Persistent across application restarts.
    """
    with get_db(db_path) as conn:
        cursor = conn.cursor()
        
        # 1. Rules table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS rules (
                id TEXT PRIMARY KEY,
                keyword TEXT NOT NULL,
                dm_message TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        # 2. Events table (event_id is UNIQUE to deduplicate webhooks)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL
            );
        """)
        
        # 3. Deliveries table (CRITICAL: UNIQUE(rule_id, user_id) constraint)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS deliveries (
                id TEXT PRIMARY KEY,
                rule_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                comment_id TEXT NOT NULL,
                dm_id TEXT,
                status TEXT NOT NULL, -- 'queued', 'sent', 'failed', 'cancelled'
                attempts INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT unique_rule_user UNIQUE(rule_id, user_id)
            );
        """)
        
        # 4. Blocked duplicates tracking table for accurate persistent /stats
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS blocked_duplicates (
                id TEXT PRIMARY KEY,
                event_id TEXT,
                rule_id TEXT,
                user_id TEXT,
                reason TEXT NOT NULL, -- 'duplicate_event' or 'duplicate_user_rule'
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

def create_rule(keyword: str, dm_message: str, db_path: Optional[str] = None) -> Dict[str, Any]:
    rule_id = str(uuid.uuid4())
    with get_db(db_path) as conn:
        conn.execute(
            "INSERT INTO rules (id, keyword, dm_message) VALUES (?, ?, ?)",
            (rule_id, keyword, dm_message)
        )
    return {"rule_id": rule_id, "keyword": keyword, "dm_message": dm_message}

def get_all_rules(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    with get_db(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, keyword, dm_message FROM rules")
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

def save_event(event_id: str, event_type: str, payload_data: Dict[str, Any], db_path: Optional[str] = None) -> bool:
    """
    Persist incoming webhook event.
    Returns True if new event, or False if event_id is a duplicate.
    """
    payload_str = json.dumps(payload_data)
    with get_db(db_path) as conn:
        try:
            conn.execute(
                "INSERT INTO events (event_id, event_type, payload) VALUES (?, ?, ?)",
                (event_id, event_type, payload_str)
            )
            return True
        except sqlite3.IntegrityError:
            # Duplicate event_id received! Record in blocked_duplicates
            conn.execute(
                "INSERT INTO blocked_duplicates (id, event_id, reason) VALUES (?, ?, ?)",
                (str(uuid.uuid4()), event_id, "duplicate_event")
            )
            return False

def create_delivery(rule_id: str, user_id: str, comment_id: str, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Attempts to insert a new delivery record for (rule_id, user_id).
    Enforces UNIQUE(rule_id, user_id). If a delivery already exists for this rule and user,
    returns None and logs to blocked_duplicates.
    """
    delivery_id = str(uuid.uuid4())
    with get_db(db_path) as conn:
        try:
            conn.execute(
                """
                INSERT INTO deliveries (id, rule_id, user_id, comment_id, status, attempts)
                VALUES (?, ?, ?, ?, 'queued', 0)
                """,
                (delivery_id, rule_id, user_id, comment_id)
            )
            return {
                "id": delivery_id,
                "rule_id": rule_id,
                "user_id": user_id,
                "comment_id": comment_id,
                "status": "queued",
                "attempts": 0
            }
        except sqlite3.IntegrityError:
            # Duplicate rule_id + user_id prevented by database UNIQUE constraint!
            conn.execute(
                """
                INSERT INTO blocked_duplicates (id, rule_id, user_id, reason)
                VALUES (?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), rule_id, user_id, "duplicate_user_rule")
            )
            return None

def update_delivery_status(
    delivery_id: str,
    status: str,
    dm_id: Optional[str] = None,
    increment_attempts: bool = False,
    db_path: Optional[str] = None
):
    with get_db(db_path) as conn:
        if increment_attempts and dm_id:
            conn.execute(
                """
                UPDATE deliveries
                SET status = ?, dm_id = ?, attempts = attempts + 1, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, dm_id, delivery_id)
            )
        elif increment_attempts:
            conn.execute(
                """
                UPDATE deliveries
                SET status = ?, attempts = attempts + 1, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, delivery_id)
            )
        elif dm_id:
            conn.execute(
                """
                UPDATE deliveries
                SET status = ?, dm_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, dm_id, delivery_id)
            )
        else:
            conn.execute(
                """
                UPDATE deliveries
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, delivery_id)
            )

def cancel_pending_delivery_by_comment(comment_id: str, db_path: Optional[str] = None) -> bool:
    """
    If a delivery for comment_id is currently queued, mark it as 'cancelled'.
    If already sent/delivered, do not undo it.
    """
    with get_db(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM deliveries WHERE comment_id = ? AND status = 'queued'",
            (comment_id,)
        )
        row = cursor.fetchone()
        if row:
            conn.execute(
                "UPDATE deliveries SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (row["id"],)
            )
            return True
        return False

def get_queued_deliveries(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Fetch all queued deliveries for worker startup recovery.
    """
    with get_db(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM deliveries WHERE status = 'queued'")
        return [dict(row) for row in cursor.fetchall()]

def get_stats(db_path: Optional[str] = None) -> Dict[str, int]:
    """
    Returns exact stats based on persistent database state.
    """
    with get_db(db_path) as conn:
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*) FROM deliveries WHERE status IN ('sent', 'delivered')")
        sent = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM deliveries WHERE status = 'failed'")
        failed = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM deliveries WHERE status = 'queued'")
        queued = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM blocked_duplicates")
        duplicates_blocked = cursor.fetchone()[0]
        
        return {
            "sent": sent,
            "failed": failed,
            "queued": queued,
            "duplicates_blocked": duplicates_blocked
        }
