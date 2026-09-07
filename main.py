"""
番頭 LINE Webhook サーバー
LINEのメッセージを受信してSQLiteに保存し、Claudeが読み込めるAPIを提供する

起動方法:
  uvicorn main:app --reload --port 8001

エンドポイント:
  POST /webhook          - LINE Webhookイベント受信
  GET  /api/messages     - 保存済みメッセージ一覧（Claude読み込み用）
  POST /api/messages/mark-saved - Obsidian保存済みマーク
  GET  /api/health       - 死活確認
"""

import os
import hmac
import hashlib
import base64
import sqlite3
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, List

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
API_KEY = os.getenv("API_KEY", "banto-secret")  # Claude読み込み用の簡易認証

DB_PATH = os.getenv("DB_PATH", "messages.db")

app = FastAPI(
    title="番頭 LINE Webhook API",
    description="LINEメッセージをObsidianに自動保存するためのブリッジサーバー",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── DB 初期化 ───────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT NOT NULL,
            source_type TEXT,          -- user / group / room
            source_id   TEXT,          -- group_id or user_id
            user_id     TEXT,
            display_name TEXT,
            message_type TEXT,         -- text / image / sticker / etc.
            content     TEXT,
            raw_event   TEXT,          -- 生JSONを保存
            saved_to_obsidian INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()
    logger.info("DB初期化完了")

@app.on_event("startup")
def startup():
    init_db()

# ── LINE署名検証 ────────────────────────────────────────────
def verify_signature(body: bytes, signature: str) -> bool:
    if not LINE_CHANNEL_SECRET:
        logger.warning("LINE_CHANNEL_SECRET未設定 - 署名検証スキップ")
        return True
    hash_ = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256
    ).digest()
    expected = base64.b64encode(hash_).decode("utf-8")
    return hmac.compare_digest(expected, signature or "")

# ── Webhook受信 ─────────────────────────────────────────────
@app.post("/webhook")
async def webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    if not verify_signature(body, signature):
        raise HTTPException(status_code=400, detail="Invalid signature")

    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    conn = get_db()
    for event in payload.get("events", []):
        event_type = event.get("type")
        source = event.get("source", {})
        source_type = source.get("type", "unknown")
        user_id = source.get("userId", "")
        group_id = source.get("groupId") or source.get("roomId") or user_id
        msg = event.get("message", {})
        msg_type = msg.get("type", event_type)

        # テキスト・スタンプ・画像など内容を取得
        if msg_type == "text":
            content = msg.get("text", "")
        elif msg_type == "sticker":
            content = f"[スタンプ] packageId={msg.get('packageId')} stickerId={msg.get('stickerId')}"
        elif msg_type == "image":
            content = "[画像]"
        elif msg_type == "file":
            content = f"[ファイル] {msg.get('fileName', '')}"
        elif msg_type == "location":
            content = f"[位置情報] {msg.get('title','')} {msg.get('address','')}"
        elif msg_type == "video":
            content = "[動画]"
        elif msg_type == "audio":
            content = "[音声]"
        else:
            content = f"[{msg_type}]"

        conn.execute("""
            INSERT INTO messages
                (received_at, source_type, source_id, user_id, message_type, content, raw_event)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.utcnow().isoformat(),
            source_type,
            group_id,
            user_id,
            msg_type,
            content,
            json.dumps(event, ensure_ascii=False),
        ))
        logger.info(f"受信: [{source_type}] {user_id} / {msg_type}: {content[:50]}")

    conn.commit()
    conn.close()
    return {"status": "ok"}

# ── メッセージ取得API (Claude読み込み用) ────────────────────
class MessageOut(BaseModel):
    id: int
    received_at: str
    source_type: Optional[str]
    source_id: Optional[str]
    user_id: Optional[str]
    display_name: Optional[str]
    message_type: Optional[str]
    content: Optional[str]
    saved_to_obsidian: int

@app.get("/api/messages", response_model=List[MessageOut])
def get_messages(
    since: Optional[str] = Query(None, description="ISO8601 datetime。これ以降のメッセージを返す"),
    unsaved_only: bool = Query(False, description="Obsidian未保存のみ返す"),
    limit: int = Query(200, le=500),
    key: str = Query(..., description="APIキー"),
):
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    conn = get_db()
    query = "SELECT * FROM messages WHERE 1=1"
    params: list = []

    if since:
        query += " AND received_at > ?"
        params.append(since)
    if unsaved_only:
        query += " AND saved_to_obsidian = 0"

    query += " ORDER BY received_at ASC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/messages/mark-saved")
def mark_saved(
    ids: List[int],
    key: str = Query(..., description="APIキー"),
):
    """ObsidianへのVault保存が完了したIDをマークする"""
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    conn = get_db()
    conn.executemany(
        "UPDATE messages SET saved_to_obsidian = 1 WHERE id = ?",
        [(i,) for i in ids]
    )
    conn.commit()
    conn.close()
    return {"marked": len(ids)}

@app.get("/api/stats")
def stats(key: str = Query(...)):
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    unsaved = conn.execute("SELECT COUNT(*) FROM messages WHERE saved_to_obsidian=0").fetchone()[0]
    conn.close()
    return {"total": total, "unsaved": unsaved}

@app.get("/api/health")
def health():
    return {"status": "ok", "version": "1.0.0"}
