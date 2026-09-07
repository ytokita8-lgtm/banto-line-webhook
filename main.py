"""
番頭 LINE Webhook サーバー v2.0
- グループ名を自動取得・保存
- 現場名マッピング対応
- Obsidianへの現場別分類保存に対応
"""

import os
import hmac
import hashlib
import base64
import sqlite3
import json
import logging
import httpx
from datetime import datetime
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
API_KEY = os.getenv("API_KEY", "banto-secret")
DB_PATH = os.getenv("DB_PATH", "messages.db")

app = FastAPI(
    title="番頭 LINE Webhook API v2",
    description="LINEグループメッセージを現場別にObsidianへ自動保存",
    version="2.0.0",
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
    # グループ名キャッシュテーブル
    conn.execute("""
        CREATE TABLE IF NOT EXISTS groups (
            group_id    TEXT PRIMARY KEY,
            group_name  TEXT,
            site_name   TEXT,
            updated_at  TEXT
        )
    """)
    # メッセージテーブル（group_name追加）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at     TEXT NOT NULL,
            source_type     TEXT,
            source_id       TEXT,
            group_name      TEXT,
            site_name       TEXT,
            user_id         TEXT,
            display_name    TEXT,
            message_type    TEXT,
            content         TEXT,
            raw_event       TEXT,
            saved_to_obsidian INTEGER DEFAULT 0
        )
    """)
    # 既存DBにカラムが無い場合は追加
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN group_name TEXT")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN site_name TEXT")
    except Exception:
        pass
    conn.commit()
    conn.close()
    logger.info("DB初期化完了 v2")

@app.on_event("startup")
def startup():
    init_db()

# ── LINE グループ名取得 ──────────────────────────────────────
async def fetch_group_name(group_id: str) -> str:
    """LINE APIからグループ名を取得してキャッシュ"""
    conn = get_db()
    row = conn.execute("SELECT group_name FROM groups WHERE group_id=?", (group_id,)).fetchone()
    if row and row["group_name"]:
        conn.close()
        return row["group_name"]

    # LINE API呼び出し
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.line.me/v2/bot/group/{group_id}/summary",
                headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
                timeout=5.0
            )
            if resp.status_code == 200:
                data = resp.json()
                group_name = data.get("groupName", group_id)
                site_name = extract_site_name(group_name)
                conn.execute("""
                    INSERT OR REPLACE INTO groups (group_id, group_name, site_name, updated_at)
                    VALUES (?, ?, ?, ?)
                """, (group_id, group_name, site_name, datetime.utcnow().isoformat()))
                conn.commit()
                conn.close()
                return group_name
    except Exception as e:
        logger.warning(f"グループ名取得失敗: {e}")

    conn.close()
    return group_id

def extract_site_name(group_name: str) -> str:
    """グループ名から現場名を抽出
    例:【7月着工】長町5丁目AP → 長町5丁目AP
        【9月着工】中田5 → 中田5
        創世建設　設計施工勉強会 → 設計施工勉強会
    """
    import re
    # 【...】を除去
    name = re.sub(r'【[^】]*】', '', group_name).strip()
    # 全角スペースも除去
    name = name.replace('　', ' ').strip()
    return name if name else group_name

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

        # グループ名を取得（キャッシュ優先）
        group_name = ""
        site_name = ""
        if group_id and source_type == "group":
            row = conn.execute("SELECT group_name, site_name FROM groups WHERE group_id=?", (group_id,)).fetchone()
            if row and row["group_name"]:
                group_name = row["group_name"]
                site_name = row["site_name"] or ""
            else:
                # 非同期でバックグラウンド取得（次回以降はキャッシュ）
                import asyncio
                asyncio.create_task(fetch_group_name(group_id))

        # メッセージ内容
        if msg_type == "text":
            content = msg.get("text", "")
        elif msg_type == "sticker":
            content = f"[スタンプ]"
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
        elif event_type in ("join", "memberJoined"):
            content = "[グループ参加]"
            # 参加時にグループ名を取得
            if group_id:
                import asyncio
                asyncio.create_task(fetch_group_name(group_id))
        elif event_type == "leave":
            content = "[グループ退出]"
        else:
            content = f"[{msg_type or event_type}]"

        conn.execute("""
            INSERT INTO messages
                (received_at, source_type, source_id, group_name, site_name,
                 user_id, message_type, content, raw_event)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.utcnow().isoformat(),
            source_type,
            group_id,
            group_name,
            site_name,
            user_id,
            msg_type or event_type,
            content,
            json.dumps(event, ensure_ascii=False),
        ))
        logger.info(f"受信: [{source_type}] {group_name or group_id} / {user_id} / {msg_type}: {content[:50]}")

    conn.commit()
    conn.close()
    return {"status": "ok"}

# ── メッセージ取得API ────────────────────────────────────────
class MessageOut(BaseModel):
    id: int
    received_at: str
    source_type: Optional[str] = None
    source_id: Optional[str] = None
    group_name: Optional[str] = None
    site_name: Optional[str] = None
    user_id: Optional[str] = None
    display_name: Optional[str] = None
    message_type: Optional[str] = None
    content: Optional[str] = None
    saved_to_obsidian: int = 0

@app.get("/api/messages", response_model=List[MessageOut])
def get_messages(
    since: Optional[str] = Query(None),
    unsaved_only: bool = Query(False),
    limit: int = Query(200, le=500),
    key: str = Query(...),
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

@app.get("/api/messages/by-group")
def get_messages_by_group(
    unsaved_only: bool = Query(True),
    key: str = Query(...),
):
    """グループ別にグループ化して返す（Obsidian保存用）"""
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    conn = get_db()
    query = "SELECT * FROM messages WHERE message_type != '[グループ参加]'"
    if unsaved_only:
        query += " AND saved_to_obsidian = 0"
    query += " ORDER BY source_id, received_at ASC"

    rows = conn.execute(query).fetchall()
    conn.close()

    # グループ別に整理
    grouped = {}
    for row in rows:
        d = dict(row)
        key_name = d.get("group_name") or d.get("source_id") or "不明"
        if key_name not in grouped:
            grouped[key_name] = {
                "group_name": key_name,
                "site_name": d.get("site_name") or "",
                "source_id": d.get("source_id") or "",
                "messages": []
            }
        grouped[key_name]["messages"].append(d)

    return list(grouped.values())

@app.get("/api/groups")
def get_groups(key: str = Query(...)):
    """登録済みグループ一覧"""
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    conn = get_db()
    rows = conn.execute("SELECT * FROM groups ORDER BY updated_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/groups/update-name")
def update_group_name(
    group_id: str,
    group_name: str,
    site_name: Optional[str] = None,
    key: str = Query(...),
):
    """グループ名を手動設定"""
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not site_name:
        site_name = extract_site_name(group_name)
    conn = get_db()
    conn.execute("""
        INSERT OR REPLACE INTO groups (group_id, group_name, site_name, updated_at)
        VALUES (?, ?, ?, ?)
    """, (group_id, group_name, site_name, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()
    return {"ok": True, "group_name": group_name, "site_name": site_name}

@app.post("/api/messages/mark-saved")
def mark_saved(ids: List[int], key: str = Query(...)):
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
    groups = conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
    conn.close()
    return {"total": total, "unsaved": unsaved, "groups": groups}

@app.get("/api/health")
def health():
    return {"status": "ok", "version": "2.0.0"}
