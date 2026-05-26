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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tool_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tool TEXT NOT NULL,
                    project TEXT NOT NULL,
                    query_text TEXT,
                    output_chars INTEGER DEFAULT 0,
                    result_count INTEGER DEFAULT 0,
                    session_id TEXT,
                    ts REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS fs_reads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tool TEXT NOT NULL,
                    path TEXT,
                    output_chars INTEGER DEFAULT 0,
                    session_id TEXT,
                    project TEXT NOT NULL,
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

    def record_tool_call(self, tool: str, project: str, *, query_text: str = "", output_chars: int = 0, result_count: int = 0, session_id: str = ""):
        with sqlite3.connect(str(self.path)) as conn:
            conn.execute(
                "INSERT INTO tool_calls (tool, project, query_text, output_chars, result_count, session_id, ts) VALUES (?,?,?,?,?,?,?)",
                (tool, project, query_text or None, output_chars, result_count, session_id or None, time.time())
            )
            conn.commit()

    def record_fs_read(self, tool: str, path: str, project: str, *, output_chars: int = 0, session_id: str = ""):
        with sqlite3.connect(str(self.path)) as conn:
            conn.execute(
                "INSERT INTO fs_reads (tool, path, output_chars, session_id, project, ts) VALUES (?,?,?,?,?,?)",
                (tool, path or None, output_chars, session_id or None, project, time.time())
            )
            conn.commit()

    def tool_usage_summary(self, project: str | None = None) -> dict:
        with sqlite3.connect(str(self.path)) as conn:
            conn.row_factory = sqlite3.Row
            where = " WHERE project=?" if project else ""
            params = (project,) if project else ()
            rows = conn.execute(
                f"SELECT tool, COUNT(*) as calls, COALESCE(SUM(output_chars),0) as total_chars, COALESCE(AVG(output_chars),0) as avg_chars FROM tool_calls{where} GROUP BY tool ORDER BY calls DESC",
                params
            ).fetchall()
            return {
                "tools": [dict(r) for r in rows],
                "total_calls": sum(r["calls"] for r in rows),
            }

    def efficiency_summary(self, project: str | None = None) -> dict:
        with sqlite3.connect(str(self.path)) as conn:
            conn.row_factory = sqlite3.Row
            # Aggregate tool_calls
            tc_where = " WHERE project=?" if project else ""
            tc_params = (project,) if project else ()
            tc = conn.execute(
                f"SELECT COUNT(*) as calls, COALESCE(SUM(output_chars),0) as total_chars FROM tool_calls{tc_where}",
                tc_params
            ).fetchone()

            # Aggregate fs_reads
            fs_where = " WHERE project=?" if project else ""
            fs_params = (project,) if project else ()
            fs = conn.execute(
                f"SELECT COUNT(*) as reads, COALESCE(SUM(output_chars),0) as total_chars FROM fs_reads{fs_where}",
                fs_params
            ).fetchone()

            # Per-session breakdown
            session_where = " WHERE project=?" if project else ""
            session_params = (project,) if project else ()
            tc_sessions = conn.execute(
                f"SELECT COALESCE(session_id,'') as session_id, SUM(output_chars) as mcp_chars FROM tool_calls{session_where} GROUP BY session_id",
                session_params
            ).fetchall()
            fs_sessions = conn.execute(
                f"SELECT COALESCE(session_id,'') as session_id, SUM(output_chars) as fs_chars FROM fs_reads{session_where} GROUP BY session_id",
                session_params
            ).fetchall()

            # Merge per-session
            session_map: dict[str, dict] = {}
            for r in tc_sessions:
                sid = r["session_id"]
                session_map.setdefault(sid, {"session_id": sid, "mcp_chars": 0, "fs_chars": 0})
                session_map[sid]["mcp_chars"] = r["mcp_chars"]
            for r in fs_sessions:
                sid = r["session_id"]
                session_map.setdefault(sid, {"session_id": sid, "mcp_chars": 0, "fs_chars": 0})
                session_map[sid]["fs_chars"] = r["fs_chars"]

            return {
                "total_mcp_chars": tc["total_chars"],
                "total_fs_chars": fs["total_chars"],
                "mcp_calls": tc["calls"],
                "fs_reads": fs["reads"],
                "sessions": list(session_map.values()),
            }

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
