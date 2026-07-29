import sqlite3
import json
import time
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "sentinelai.db")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            timestamp REAL,
            reading TEXT,
            primary_hazard TEXT,
            risk_score REAL,
            risk_band TEXT,
            actions TEXT
        )
    """)
    conn.commit()
    conn.close()


def log_event(result: dict):
    conn = sqlite3.connect(DB_PATH)
    d = result["decision"]
    conn.execute(
        "INSERT INTO events (device_id, timestamp, reading, primary_hazard, risk_score, risk_band, actions) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            result["device_id"],
            result["timestamp"],
            json.dumps(result["reading"]),
            d["primary_hazard"],
            d["risk_score"],
            d["risk_band"],
            json.dumps(d["actions"]),
        ),
    )
    conn.commit()
    conn.close()


def recent_events(limit=50):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
