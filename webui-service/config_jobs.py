"""配置保存异步任务存储与状态管理模块 (Config Job Store).

支持配置保存持久化、进度追踪、幂等提交、CAS 版本防冲突以及断线/刷新恢复。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("webui_service.config_jobs")


@dataclass
class ConfigJob:
    job_id: str
    idempotency_key: str | None
    created_at: float
    updated_at: float
    status: str  # pending, running, success, partial, failed, conflict
    phase: str   # queued, validating, gate_checking, writing, applying_processes, reconciling_sources, done
    progress: int  # 0..100
    message: str
    base_revision: str | None
    new_revision: str | None
    payload: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ok"] = self.status in ("success", "partial")
        return d


class ConfigJobStore:
    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            # 优先使用持久数据卷 /data/webui_jobs.db
            data_dir = os.environ.get("XDG_DATA_HOME", "/data")
            p = Path(data_dir)
            if p.is_dir() and os.access(str(p), os.W_OK):
                self.db_path = p / "webui_jobs.db"
            else:
                repo_dir = os.environ.get("WEBUI_REPO_DIR", ".")
                self.db_path = Path(repo_dir) / ".webui_jobs.db"
        else:
            self.db_path = Path(db_path)

        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        try:
            with self._get_connection() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS config_jobs (
                        job_id TEXT PRIMARY KEY,
                        idempotency_key TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        status TEXT NOT NULL,
                        phase TEXT NOT NULL,
                        progress INTEGER NOT NULL DEFAULT 0,
                        message TEXT NOT NULL DEFAULT '',
                        base_revision TEXT,
                        new_revision TEXT,
                        payload_json TEXT NOT NULL,
                        result_json TEXT,
                        error TEXT
                    );
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_jobs_idempotency ON config_jobs(idempotency_key);
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_jobs_status ON config_jobs(status);
                """)
                conn.commit()
        except Exception as exc:
            logger.warning("初始化 config_jobs 数据库失败: %s，将回退内存库", exc)
            self.db_path = Path(":memory:")
            with self._get_connection() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS config_jobs (
                        job_id TEXT PRIMARY KEY,
                        idempotency_key TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        status TEXT NOT NULL,
                        phase TEXT NOT NULL,
                        progress INTEGER NOT NULL DEFAULT 0,
                        message TEXT NOT NULL DEFAULT '',
                        base_revision TEXT,
                        new_revision TEXT,
                        payload_json TEXT NOT NULL,
                        result_json TEXT,
                        error TEXT
                    );
                """)
                conn.commit()

    def _row_to_job(self, row: sqlite3.Row) -> ConfigJob:
        return ConfigJob(
            job_id=row["job_id"],
            idempotency_key=row["idempotency_key"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            status=row["status"],
            phase=row["phase"],
            progress=row["progress"],
            message=row["message"],
            base_revision=row["base_revision"],
            new_revision=row["new_revision"],
            payload=json.loads(row["payload_json"] or "{}"),
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error=row["error"],
        )

    def create_job(
        self,
        payload: dict[str, Any],
        base_revision: str | None = None,
        idempotency_key: str | None = None,
    ) -> ConfigJob:
        job_id = f"cfg_job_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        now = time.time()
        job = ConfigJob(
            job_id=job_id,
            idempotency_key=idempotency_key,
            created_at=now,
            updated_at=now,
            status="pending",
            phase="queued",
            progress=0,
            message="保存任务已提交，等待执行...",
            base_revision=base_revision,
            new_revision=None,
            payload=payload,
            result=None,
            error=None,
        )
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO config_jobs (
                    job_id, idempotency_key, created_at, updated_at, status, phase,
                    progress, message, base_revision, new_revision, payload_json, result_json, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    job.idempotency_key,
                    job.created_at,
                    job.updated_at,
                    job.status,
                    job.phase,
                    job.progress,
                    job.message,
                    job.base_revision,
                    job.new_revision,
                    json.dumps(job.payload, ensure_ascii=False),
                    None,
                    None,
                ),
            )
            conn.commit()
        return job

    def get_job(self, job_id: str) -> ConfigJob | None:
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM config_jobs WHERE job_id = ?", (job_id,))
            row = cur.fetchone()
            return self._row_to_job(row) if row else None

    def find_by_idempotency_key(self, key: str, max_age_seconds: float = 3600.0) -> ConfigJob | None:
        if not key:
            return None
        min_created = time.time() - max_age_seconds
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT * FROM config_jobs WHERE idempotency_key = ? AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
                (key, min_created),
            )
            row = cur.fetchone()
            return self._row_to_job(row) if row else None

    def get_active_job(self) -> ConfigJob | None:
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT * FROM config_jobs WHERE status IN ('pending', 'running') ORDER BY created_at DESC LIMIT 1"
            )
            row = cur.fetchone()
            return self._row_to_job(row) if row else None

    def update_job(
        self,
        job_id: str,
        status: str | None = None,
        phase: str | None = None,
        progress: int | None = None,
        message: str | None = None,
        new_revision: str | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> ConfigJob | None:
        now = time.time()
        updates = ["updated_at = ?"]
        params: list[Any] = [now]

        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if phase is not None:
            updates.append("phase = ?")
            params.append(phase)
        if progress is not None:
            updates.append("progress = ?")
            params.append(max(0, min(100, progress)))
        if message is not None:
            updates.append("message = ?")
            params.append(message)
        if new_revision is not None:
            updates.append("new_revision = ?")
            params.append(new_revision)
        if result is not None:
            updates.append("result_json = ?")
            params.append(json.dumps(result, ensure_ascii=False))
        if error is not None:
            updates.append("error = ?")
            params.append(error)

        params.append(job_id)
        with self._get_connection() as conn:
            conn.execute(f"UPDATE config_jobs SET {', '.join(updates)} WHERE job_id = ?", params)
            conn.commit()
        return self.get_job(job_id)

    def cleanup_old_jobs(self, keep_limit: int = 50) -> int:
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                DELETE FROM config_jobs WHERE job_id NOT IN (
                    SELECT job_id FROM config_jobs ORDER BY created_at DESC LIMIT ?
                ) AND status NOT IN ('pending', 'running')
                """,
                (keep_limit,),
            )
            deleted = cur.rowcount
            conn.commit()
            return deleted
