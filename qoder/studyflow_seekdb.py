"""StudyFlow 的 seekdb 存储层。

StudyFlow 的论文卡片默认存在浏览器 localStorage 里；这个模块把同一份数据
落到 seekdb（MySQL 协议兼容）上，使卡片可以跨设备、跨会话复用。

连接参数全部走环境变量，默认对齐本地 seekdb 实例：

    STUDYFLOW_DB_HOST=127.0.0.1
    STUDYFLOW_DB_PORT=2881
    STUDYFLOW_DB_USER=root
    STUDYFLOW_DB_PASSWORD=
    STUDYFLOW_DB_NAME=studyflow
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import pymysql

DB_HOST = os.environ.get("STUDYFLOW_DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("STUDYFLOW_DB_PORT", "2881"))
DB_USER = os.environ.get("STUDYFLOW_DB_USER", "root")
DB_PASSWORD = os.environ.get("STUDYFLOW_DB_PASSWORD", "")
DB_NAME = os.environ.get("STUDYFLOW_DB_NAME", "studyflow")

DDL_CARDS = """
CREATE TABLE IF NOT EXISTS study_cards (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  title VARCHAR(255) NOT NULL,
  background TEXT,
  method TEXT,
  conclusion TEXT,
  limitation TEXT,
  created_at BIGINT NOT NULL,
  KEY idx_created_at (created_at)
)
"""

DDL_SESSIONS = """
CREATE TABLE IF NOT EXISTS focus_sessions (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  started_at BIGINT NOT NULL,
  minutes INT NOT NULL,
  label VARCHAR(255)
)
"""


def connect(create_database: bool = True) -> pymysql.connections.Connection:
    """Open one seekdb connection, creating the database when asked."""

    if create_database:
        bootstrap = pymysql.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            charset="utf8mb4",
            autocommit=True,
        )
        try:
            with bootstrap.cursor() as cursor:
                cursor.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{DB_NAME}` "
                    "DEFAULT CHARACTER SET utf8mb4"
                )
        finally:
            bootstrap.close()
    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        charset="utf8mb4",
        autocommit=True,
    )


def init_schema() -> None:
    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(DDL_CARDS)
            cursor.execute(DDL_SESSIONS)
    finally:
        connection.close()


def health() -> dict[str, Any]:
    """Report whether seekdb is reachable and the schema is present."""

    try:
        connection = connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
                cursor.execute("SELECT COUNT(*) FROM study_cards")
                cards = cursor.fetchone()[0]
        finally:
            connection.close()
        return {"ok": True, "cards": int(cards), "host": DB_HOST, "port": DB_PORT}
    except Exception as error:  # noqa: BLE001 - surfaced to the UI as a badge
        return {"ok": False, "error": f"{type(error).__name__}: {error}"}


def add_card(card: dict[str, Any]) -> int:
    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO study_cards "
                "(title, background, method, conclusion, limitation, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    str(card.get("title", ""))[:255],
                    card.get("background", ""),
                    card.get("method", ""),
                    card.get("conclusion", ""),
                    card.get("limitation", ""),
                    int(card.get("createdAt") or time.time() * 1000),
                ),
            )
            return int(cursor.lastrowid)
    finally:
        connection.close()


def list_cards(limit: int = 100) -> list[dict[str, Any]]:
    connection = connect()
    try:
        with connection.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                "SELECT id, title, background, method, conclusion, limitation, created_at "
                "FROM study_cards ORDER BY id DESC LIMIT %s",
                (int(limit),),
            )
            rows = cursor.fetchall()
    finally:
        connection.close()
    return [
        {
            "id": row["id"],
            "title": row["title"],
            "background": row["background"],
            "method": row["method"],
            "conclusion": row["conclusion"],
            "limitation": row["limitation"],
            "createdAt": row["created_at"],
        }
        for row in rows
    ]


def add_focus_session(minutes: int, label: str = "") -> int:
    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO focus_sessions (started_at, minutes, label) VALUES (%s, %s, %s)",
                (int(time.time() * 1000), int(minutes), label[:255]),
            )
            return int(cursor.lastrowid)
    finally:
        connection.close()


def focus_total_minutes() -> int:
    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT COALESCE(SUM(minutes), 0) FROM focus_sessions")
            return int(cursor.fetchone()[0])
    finally:
        connection.close()


if __name__ == "__main__":
    init_schema()
    print(json.dumps(health(), ensure_ascii=False))
