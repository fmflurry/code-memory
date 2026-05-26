import sqlite3
import time
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

@dataclass
class RetrieveTiming:
    query: str
    embed_ms: float
    code_search_ms: float
    eps_search_ms: float
    claims_ms: float
    total_ms: float
    code_hit_count: int = 0
    eps_hit_count: int = 0
    claims_hit_count: int = 0

class MetricsStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
    
    def _init_db(self):
        with sqlite3.connect(str(self.path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS retrieves (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT,
                    embed_ms REAL,
                    code_search_ms REAL,
                    eps_search_ms REAL,
                    claims_ms REAL,
                    total_ms REAL,
                    code_hit_count INTEGER DEFAULT 0,
                    eps_hit_count INTEGER DEFAULT 0,
                    claims_hit_count INTEGER DEFAULT 0,
                    ts REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS backend_health (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    backend TEXT NOT NULL,
                    status TEXT NOT NULL,
                    latency_ms REAL,
                    ts REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cache_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    hits INTEGER DEFAULT 0,
                    misses INTEGER DEFAULT 0,
                    ts REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ingest_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    files INTEGER DEFAULT 0,
                    symbols INTEGER DEFAULT 0,
                    duration_s REAL,
                    ts REAL NOT NULL
                )
            """)
            conn.commit()
    
    def record_retrieve(self, m: RetrieveTiming):
        with sqlite3.connect(str(self.path)) as conn:
            conn.execute(
                "INSERT INTO retrieves (query, embed_ms, code_search_ms, eps_search_ms, claims_ms, total_ms, code_hit_count, eps_hit_count, claims_hit_count, ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (m.query, m.embed_ms, m.code_search_ms, m.eps_search_ms, m.claims_ms, m.total_ms, m.code_hit_count, m.eps_hit_count, m.claims_hit_count, time.time())
            )
            conn.commit()
    
    def record_backend_health(self, backend: str, status: str, latency_ms: float):
        with sqlite3.connect(str(self.path)) as conn:
            conn.execute(
                "INSERT INTO backend_health (backend, status, latency_ms, ts) VALUES (?,?,?,?)",
                (backend, status, latency_ms, time.time())
            )
            conn.commit()
    
    def record_cache_stats(self, hits: int, misses: int):
        with sqlite3.connect(str(self.path)) as conn:
            conn.execute(
                "INSERT INTO cache_stats (hits, misses, ts) VALUES (?,?,?)",
                (hits, misses, time.time())
            )
            conn.commit()
    
    def record_ingest(self, files: int, symbols: int, duration_s: float):
        with sqlite3.connect(str(self.path)) as conn:
            conn.execute(
                "INSERT INTO ingest_stats (files, symbols, duration_s, ts) VALUES (?,?,?,?)",
                (files, symbols, duration_s, time.time())
            )
            conn.commit()
    
    def summary(self) -> dict[str, Any]:
        with sqlite3.connect(str(self.path)) as conn:
            conn.row_factory = sqlite3.Row
            # Retrieve stats
            r = conn.execute(
                "SELECT COUNT(*) as count, AVG(total_ms) as avg_total_ms, AVG(embed_ms) as avg_embed_ms, AVG(code_search_ms) as avg_code_search_ms FROM retrieves"
            ).fetchone()
            
            # Cache stats
            c = conn.execute(
                "SELECT COALESCE(SUM(hits),0) as total_hits, COALESCE(SUM(misses),0) as total_misses FROM cache_stats"
            ).fetchone()
            total_cache = c["total_hits"] + c["total_misses"]
            hit_ratio = c["total_hits"] / total_cache if total_cache > 0 else 0.0
            
            # Ingest stats
            i = conn.execute(
                "SELECT COUNT(*) as count, COALESCE(SUM(symbols),0) as total_symbols, MAX(ts) as last_ingest_ts FROM ingest_stats"
            ).fetchone()
            
            # Backend health - latest per backend
            b = conn.execute(
                "SELECT backend, status, MAX(ts) as last_ts FROM backend_health GROUP BY backend"
            ).fetchall()
            
            return {
                "retrieves": {
                    "count": r["count"],
                    "avg_total_ms": round(r["avg_total_ms"] or 0, 1),
                    "avg_embed_ms": round(r["avg_embed_ms"] or 0, 1),
                    "avg_code_search_ms": round(r["avg_code_search_ms"] or 0, 1),
                },
                "cache": {
                    "total_hits": c["total_hits"],
                    "total_misses": c["total_misses"],
                    "hit_ratio": round(hit_ratio, 3),
                },
                "ingest": {
                    "count": i["count"],
                    "total_symbols": i["total_symbols"],
                    "last_ingest_ts": i["last_ingest_ts"],
                },
                "backends": [{"backend": row["backend"], "status": row["status"]} for row in b],
            }
    
    def recent_retrieves(self, limit: int = 10) -> list[dict]:
        with sqlite3.connect(str(self.path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM retrieves ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
    
    def close(self):
        pass
