import os
import json
import io
import tempfile
import zipfile
import uuid, sqlite3, secrets, urllib.parse, uuid
import base64, hashlib, hmac, time

# Load a local .env file without requiring another package.
_ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_ENV_FILE):
    with open(_ENV_FILE, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _key, _value = _line.split("=", 1)
            os.environ.setdefault(_key.strip(), _value.strip().strip("\"").strip("'"))
from functools import wraps
from werkzeug.utils import secure_filename
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file

try:
    import requests
except ImportError:
    requests = None

app = Flask(__name__)
# Allow larger multi-photo/video uploads while still imposing a firm request limit.
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024
app.secret_key = os.environ.get("FLASK_SECRET", "change-this-in-production")
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "social_apps.db")

DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "")
DISCORD_REDIRECT_URI = os.environ.get("DISCORD_REDIRECT_URI", "http://127.0.0.1:5050/oauth/callback")
DISCORD_API = "https://discord.com/api/v10"


# SUPABASE_POSTGRES_COMPAT_LAYER
# Use PostgreSQL only when DATABASE_URL is configured; local development stays on SQLite.
import re
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)
try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None

if USE_POSTGRES and psycopg is None:
    raise RuntimeError("DATABASE_URL is set but psycopg is not installed. Install requirements.txt.")

DB_INTEGRITY_ERRORS = (sqlite3.IntegrityError,)
if USE_POSTGRES:
    DB_INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg.IntegrityError)

class _CompatRow(dict):
    """Dict row that also supports SQLite-style numeric indexing."""
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)

class _CompatCursor:
    def __init__(self, cursor):
        self._cursor = cursor
    def fetchone(self):
        row = self._cursor.fetchone()
        return None if row is None else _CompatRow(row)
    def fetchall(self):
        return [_CompatRow(row) for row in self._cursor.fetchall()]
    def fetchmany(self, size=None):
        rows = self._cursor.fetchmany() if size is None else self._cursor.fetchmany(size)
        return [_CompatRow(row) for row in rows]
    def __iter__(self):
        for row in self._cursor:
            yield _CompatRow(row)
    def __getattr__(self, name):
        return getattr(self._cursor, name)

def _postgres_sql(sql):
    text = sql.strip()
    if re.fullmatch(r"SELECT\s+last_insert_rowid\(\)", text, flags=re.I):
        return "SELECT LASTVAL()"

    if re.match(r"INSERT\s+OR\s+REPLACE\s+INTO\s+login_codes\b", text, flags=re.I):
        text = re.sub(r"^INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", text, count=1, flags=re.I)
        text = text.rstrip().rstrip(";")
        text += " ON CONFLICT (code) DO UPDATE SET discord_id=EXCLUDED.discord_id, expires_at=EXCLUDED.expires_at"
        return text.replace("?", "%s")

    ignore = bool(re.match(r"INSERT\s+OR\s+IGNORE\s+INTO\b", text, flags=re.I))
    if ignore:
        text = re.sub(r"^INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", text, count=1, flags=re.I)
    text = text.replace("?", "%s")
    if ignore:
        text = text.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return text

class _PostgresCompatConnection:
    def __init__(self, raw):
        self._raw = raw
    def execute(self, sql, params=None):
        cur = self._raw.cursor()
        translated = _postgres_sql(sql)
        if params is None:
            cur.execute(translated)
        else:
            cur.execute(translated, params)
        return _CompatCursor(cur)
    def executemany(self, sql, seq_of_params):
        cur = self._raw.cursor()
        cur.executemany(_postgres_sql(sql), seq_of_params)
        return _CompatCursor(cur)
    def commit(self):
        return self._raw.commit()
    def rollback(self):
        return self._raw.rollback()
    def close(self):
        return self._raw.close()
    def __getattr__(self, name):
        return getattr(self._raw, name)

def conn():
    if USE_POSTGRES:
        raw = psycopg.connect(
            DATABASE_URL,
            row_factory=dict_row,
            connect_timeout=15,
            application_name="oc-socials",
        )
        return _PostgresCompatConnection(raw)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c



def store_post_photo(uploaded, filename):
    """Store uploaded post images in Supabase Storage in production PostgreSQL mode."""
    if not USE_POSTGRES:
        upload_dir = os.path.join(app.root_path, "static", "uploads")
        os.makedirs(upload_dir, exist_ok=True)
        uploaded.save(os.path.join(upload_dir, filename))
        return request.host_url.rstrip("/") + url_for("static", filename=f"uploads/{filename}")

    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not supabase_url or not service_key:
        raise RuntimeError("Photo storage is not configured on the server (SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY).")
    if requests is None:
        raise RuntimeError("The requests package is required for Supabase Storage uploads.")

    content_type = uploaded.mimetype or "application/octet-stream"
    response = requests.post(
        f"{supabase_url}/storage/v1/object/oc-socials-uploads/{filename}",
        headers={
            "Authorization": f"Bearer {service_key}",
            "apikey": service_key,
            "Content-Type": content_type,
            "x-upsert": "true",
        },
        data=uploaded.read(),
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(f"Supabase Storage upload failed (HTTP {response.status_code}).")
    return f"{supabase_url}/storage/v1/object/public/oc-socials-uploads/{filename}"

def init():
    if USE_POSTGRES:
        c = conn()
        required = {
            "users", "ocs", "posts", "login_codes", "discord_outbox",
            "post_likes", "retweets", "reply_likes", "replies", "chats", "chat_members", "chat_users", "messages",
            "oc_follows",
        }
        existing = {
            row["table_name"] for row in c.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
            ).fetchall()
        }
        missing = sorted(required - existing)
        if missing:
            c.close()
            raise RuntimeError(
                "Supabase database is missing tables: " + ", ".join(missing)
                + ". Do not set DATABASE_URL on Render until the Supabase schema is complete."
            )
        # Safe additive schema for Instagram media carousels and Stories.
        c.execute("""CREATE TABLE IF NOT EXISTS post_media (
            id BIGSERIAL PRIMARY KEY, post_id BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
            media_url TEXT NOT NULL, media_type TEXT NOT NULL DEFAULT 'image', position INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        # Additive migration: allow image/GIF attachments on Instagram and Twitter comments.
        c.execute("ALTER TABLE replies ADD COLUMN IF NOT EXISTS media_url TEXT NOT NULL DEFAULT ''")
        c.execute("ALTER TABLE replies ADD COLUMN IF NOT EXISTS media_type TEXT NOT NULL DEFAULT ''")
        c.execute("""CREATE TABLE IF NOT EXISTS stories (
            id BIGSERIAL PRIMARY KEY, oc_id BIGINT NOT NULL REFERENCES ocs(id) ON DELETE CASCADE,
            media_url TEXT NOT NULL, media_type TEXT NOT NULL DEFAULT 'image', text_overlay TEXT DEFAULT '',
            text_color TEXT DEFAULT '#ffffff', text_background TEXT DEFAULT 'rgba(0,0,0,.35)',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, expires_at TIMESTAMP NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS story_replies (
            id BIGSERIAL PRIMARY KEY,
            story_id BIGINT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
            oc_id BIGINT NOT NULL REFERENCES ocs(id) ON DELETE CASCADE,
            text TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS instagram_dm_conversations (
            id BIGSERIAL PRIMARY KEY,
            oc_low_id BIGINT NOT NULL REFERENCES ocs(id) ON DELETE CASCADE,
            oc_high_id BIGINT NOT NULL REFERENCES ocs(id) ON DELETE CASCADE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(oc_low_id, oc_high_id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS instagram_dm_messages (
            id BIGSERIAL PRIMARY KEY,
            conversation_id BIGINT NOT NULL REFERENCES instagram_dm_conversations(id) ON DELETE CASCADE,
            sender_oc_id BIGINT NOT NULL REFERENCES ocs(id) ON DELETE CASCADE,
            text TEXT NOT NULL DEFAULT '',
            image_url TEXT NOT NULL DEFAULT '',
            story_id BIGINT,
            story_media_url TEXT NOT NULL DEFAULT '',
            story_media_type TEXT NOT NULL DEFAULT 'image',
            story_text TEXT NOT NULL DEFAULT '',
            story_username TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS oc_post_likes (
            post_id BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
            oc_id BIGINT NOT NULL REFERENCES ocs(id) ON DELETE CASCADE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(post_id, oc_id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS chat_reads (
            user_id BIGINT NOT NULL,
            chat_id BIGINT NOT NULL,
            last_read_message_id BIGINT NOT NULL DEFAULT 0,
            UNIQUE(user_id, chat_id))""")
        # Per-user conversation settings (wallpaper is personal, not shared).
        c.execute("""CREATE TABLE IF NOT EXISTS chat_preferences (
            user_id BIGINT NOT NULL,
            chat_id BIGINT NOT NULL,
            wallpaper TEXT NOT NULL DEFAULT 'preset:classic',
            PRIMARY KEY(user_id, chat_id))""")
        c.commit()
        c.close()
        return
    c = conn()
    c.execute("CREATE TABLE IF NOT EXISTS login_codes (code TEXT PRIMARY KEY, discord_id INTEGER NOT NULL, expires_at INTEGER NOT NULL)")
    c.execute("""CREATE TABLE IF NOT EXISTS login_codes (
        code TEXT PRIMARY KEY,
        discord_id INTEGER NOT NULL,
        expires_at INTEGER NOT NULL
    )""")
    c.commit()
    c.close()
    c = conn()
    c = conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY,
      discord_name TEXT NOT NULL,
      discord_avatar TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS ocs(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      owner_id INTEGER NOT NULL,
      name TEXT NOT NULL,
      username TEXT NOT NULL,
      avatar TEXT DEFAULT '',
      bio TEXT DEFAULT '',
      banner TEXT DEFAULT '',
      UNIQUE(owner_id, username)
    );
    CREATE TABLE IF NOT EXISTS posts(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      oc_id INTEGER NOT NULL,
      platform TEXT NOT NULL,
      text TEXT DEFAULT '',
      image TEXT DEFAULT '',
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS post_media(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
      media_url TEXT NOT NULL,
      media_type TEXT NOT NULL DEFAULT 'image',
      position INTEGER NOT NULL DEFAULT 0,
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS stories(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      oc_id INTEGER NOT NULL REFERENCES ocs(id) ON DELETE CASCADE,
      media_url TEXT NOT NULL,
      media_type TEXT NOT NULL DEFAULT 'image',
      text_overlay TEXT DEFAULT '',
      text_color TEXT DEFAULT '#ffffff',
      text_background TEXT DEFAULT 'rgba(0,0,0,.35)',
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
      expires_at DATETIME NOT NULL
    );
    CREATE TABLE IF NOT EXISTS story_replies(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      story_id INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
      oc_id INTEGER NOT NULL REFERENCES ocs(id) ON DELETE CASCADE,
      text TEXT NOT NULL,
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS instagram_dm_conversations(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      oc_low_id INTEGER NOT NULL REFERENCES ocs(id) ON DELETE CASCADE,
      oc_high_id INTEGER NOT NULL REFERENCES ocs(id) ON DELETE CASCADE,
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
      UNIQUE(oc_low_id, oc_high_id)
    );
    CREATE TABLE IF NOT EXISTS instagram_dm_messages(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      conversation_id INTEGER NOT NULL REFERENCES instagram_dm_conversations(id) ON DELETE CASCADE,
      sender_oc_id INTEGER NOT NULL REFERENCES ocs(id) ON DELETE CASCADE,
      text TEXT NOT NULL DEFAULT '',
      image_url TEXT NOT NULL DEFAULT '',
      story_id INTEGER,
      story_media_url TEXT NOT NULL DEFAULT '',
      story_media_type TEXT NOT NULL DEFAULT 'image',
      story_text TEXT NOT NULL DEFAULT '',
      story_username TEXT NOT NULL DEFAULT '',
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS discord_outbox(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      event_type TEXT NOT NULL,
      payload TEXT NOT NULL,
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
      sent INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS post_likes(
        post_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(post_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS retweets(
        post_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(post_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS reply_likes(
        reply_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(reply_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS replies(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      post_id INTEGER NOT NULL,
      oc_id INTEGER NOT NULL,
      text TEXT NOT NULL,
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
      parent_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS chats(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL,
      owner_id INTEGER NOT NULL,
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS chat_members(
      chat_id INTEGER NOT NULL,
      oc_id INTEGER NOT NULL,
      UNIQUE(chat_id, oc_id)
    );
    CREATE TABLE IF NOT EXISTS chat_users(
      chat_id INTEGER NOT NULL,
      user_id INTEGER NOT NULL,
      UNIQUE(chat_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS messages(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      chat_id INTEGER NOT NULL,
      oc_id INTEGER NOT NULL,
      text TEXT NOT NULL,
      image TEXT DEFAULT '',
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS chat_reads(
      user_id INTEGER NOT NULL,
      chat_id INTEGER NOT NULL,
      last_read_message_id INTEGER NOT NULL DEFAULT 0,
      UNIQUE(user_id, chat_id)
    );
    CREATE TABLE IF NOT EXISTS chat_preferences(
      user_id INTEGER NOT NULL,
      chat_id INTEGER NOT NULL,
      wallpaper TEXT NOT NULL DEFAULT 'preset:classic',
      PRIMARY KEY(user_id, chat_id)
    );
    """)
    c.execute("""CREATE TABLE IF NOT EXISTS oc_tweet_likes (
        post_id BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
        oc_id BIGINT NOT NULL REFERENCES ocs(id) ON DELETE CASCADE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(post_id, oc_id)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS oc_post_likes (
        post_id BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
        oc_id BIGINT NOT NULL REFERENCES ocs(id) ON DELETE CASCADE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(post_id, oc_id)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS oc_tweet_retweets (
        post_id BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
        oc_id BIGINT NOT NULL REFERENCES ocs(id) ON DELETE CASCADE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(post_id, oc_id)
    )""")
    # Small migrations so an older copy of this project keeps working.
    columns = {row["name"] for row in c.execute("PRAGMA table_info(users)").fetchall()}
    reply_columns = {row["name"] for row in c.execute("PRAGMA table_info(replies)").fetchall()}
    if "parent_id" not in reply_columns:
        c.execute("ALTER TABLE replies ADD COLUMN parent_id INTEGER")
    if "media_url" not in reply_columns:
        c.execute("ALTER TABLE replies ADD COLUMN media_url TEXT NOT NULL DEFAULT ''")
    if "media_type" not in reply_columns:
        c.execute("ALTER TABLE replies ADD COLUMN media_type TEXT NOT NULL DEFAULT ''")
    if "discord_avatar" not in columns:
        c.execute("ALTER TABLE users ADD COLUMN discord_avatar TEXT DEFAULT ''")
    chat_columns = {row["name"] for row in c.execute("PRAGMA table_info(chats)").fetchall()}
    if "owner_id" not in chat_columns:
        c.execute("ALTER TABLE chats ADD COLUMN owner_id INTEGER DEFAULT 0")
    c.execute("""CREATE TABLE IF NOT EXISTS oc_follows (
        follower_oc_id INTEGER NOT NULL,
        followed_oc_id INTEGER NOT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (follower_oc_id, followed_oc_id),
        CHECK (follower_oc_id <> followed_oc_id)
    )""")
    c.execute("""INSERT OR IGNORE INTO chat_users(chat_id,user_id) SELECT cm.chat_id,o.owner_id FROM chat_members cm JOIN ocs o ON o.id=cm.oc_id""")
    c.commit()
    c.close()


init()

@app.context_processor
def template_helpers():
    def user_ocs(user_id):
        c = conn()
        rows = c.execute("SELECT * FROM ocs WHERE owner_id=? ORDER BY name", (user_id,)).fetchall()
        c.close()
        return rows
    def oc_follower_count(oc_id):
        c = conn()
        row = c.execute("SELECT COUNT(*) FROM oc_follows WHERE followed_oc_id=?", (oc_id,)).fetchone()
        c.close()
        return row[0] if row else 0

    def oc_following_count(oc_id):
        c = conn()
        row = c.execute("SELECT COUNT(*) FROM oc_follows WHERE follower_oc_id=?", (oc_id,)).fetchone()
        c.close()
        return row[0] if row else 0

    def oc_is_following(follower_oc_id, followed_oc_id):
        c = conn()
        row = c.execute(
            "SELECT 1 FROM oc_follows WHERE follower_oc_id=? AND followed_oc_id=?",
            (follower_oc_id, followed_oc_id),
        ).fetchone()
        c.close()
        return row is not None

    return {
        "user_ocs": user_ocs,
        "oc_follower_count": oc_follower_count,
        "oc_following_count": oc_following_count,
        "oc_is_following": oc_is_following,
    }


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    c = conn()
    row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    c.close()
    return row


def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapped


def discord_configured():
    return bool(DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET and requests)


@app.route("/")
def home():
    user = current_user()
    if not user:
        return render_template("home.html", user=None)
    c = conn()
    ocs = c.execute("SELECT * FROM ocs WHERE owner_id=? ORDER BY name", (user["id"],)).fetchall()
    c.close()
    return render_template("home.html", user=user, ocs=ocs)


@app.route("/discord-test")
def discord_test():
    r = requests.post(
        "https://discord.com/api/oauth2/token",
        data={
            "client_id": "0",
            "client_secret": "0",
            "grant_type": "authorization_code",
            "code": "0",
            "redirect_uri": DISCORD_REDIRECT_URI,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    return f"Discord OAuth status: {r.status_code}\n\n{r.text[:500]}", r.status_code


@app.route("/social-login/create", methods=["POST"])
def social_login_create():
    secret = os.environ.get("SOCIAL_LOGIN_SECRET", "")
    if not secret or request.headers.get("X-Social-Secret") != secret:
        return jsonify(error="Unauthorized"), 401

    data = request.json or {}
    discord_id = data.get("discord_id")
    code = str(data.get("code") or "").strip().upper()

    if not discord_id or not code:
        return jsonify(error="Missing discord_id or code"), 400

    try:
        discord_id = int(discord_id)
    except (ValueError, TypeError):
        return jsonify(error="Invalid discord_id"), 400

    c = conn()
    c.execute("""CREATE TABLE IF NOT EXISTS login_codes (
        code TEXT PRIMARY KEY,
        discord_id INTEGER NOT NULL,
        expires_at INTEGER NOT NULL
    )""")
    c.execute("DELETE FROM login_codes WHERE expires_at < ?", (int(time.time()),))
    c.execute(
        "INSERT OR REPLACE INTO login_codes(code, discord_id, expires_at) VALUES(?,?,?)",
        (code, discord_id, int(time.time()) + 600)
    )
    c.commit()
    c.close()

    return jsonify(ok=True)

@app.route("/social-login/verify", methods=["POST"])
def social_login_verify():
    code = (request.form.get("code") or "").strip().upper()
    if not code:
        return render_template("login.html", error="Enter your login code.")

    c = conn()
    row = c.execute(
        "SELECT discord_id FROM login_codes WHERE code=? AND expires_at>=?",
        (code, int(time.time()))
    ).fetchone()

    if not row:
        c.close()
        return render_template("login.html", error="Invalid or expired login code.")

    discord_id = int(row["discord_id"])
    c.execute("DELETE FROM login_codes WHERE code=?", (code,))
    user = c.execute("SELECT * FROM users WHERE id=?", (discord_id,)).fetchone()

    if not user:
        c.execute(
            "INSERT INTO users(id, discord_name, discord_avatar) VALUES(?,?,?)",
            (discord_id, str(discord_id), "")
        )

    c.commit()
    c.close()
    session["user_id"] = discord_id
    return redirect(url_for("messages"))

@app.route("/social-login")
def social_login():
    token = request.args.get("token", "")
    secret = os.environ.get("SOCIAL_LOGIN_SECRET", "")
    if not secret or not token or "." not in token:
        return "Invalid social login link.", 400
    try:
        encoded, signature = token.rsplit(".", 1)
        padding = "=" * (-len(encoded) % 4)
        payload = base64.urlsafe_b64decode((encoded + padding).encode()).decode()
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return "Invalid social login link.", 403
        discord_id, timestamp = payload.split(":", 1)
        if time.time() - int(timestamp) > 600:
            return "This social login link has expired. Use !sociallogin again in Discord.", 403
        discord_id = int(discord_id)
    except Exception:
        return "Invalid social login link.", 400
    c = conn()
    user = c.execute("SELECT * FROM users WHERE id=?", (discord_id,)).fetchone()
    if not user:
        c.execute("INSERT INTO users(id, discord_name, discord_avatar) VALUES(?,?,?)", (discord_id, str(discord_id), ""))
        c.commit()
    c.close()
    session["user_id"] = discord_id
    return redirect(url_for("messages"))

@app.route("/login")
def login():
    if not discord_configured():
        return render_template("login.html", configured=False)
    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify",
        "state": state,
    }
    return redirect("https://discord.com/oauth2/authorize?" + urllib.parse.urlencode(params))


@app.route("/oauth/callback")
def oauth_callback():
    if not discord_configured():
        return "Discord OAuth is not configured.", 500
    if request.args.get("state") != session.pop("oauth_state", None):
        return "Invalid OAuth state.", 400
    code = request.args.get("code")
    if not code:
        return "Discord authorization was cancelled.", 400

    token_data = {
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": DISCORD_REDIRECT_URI,
    }
    token = requests.post(
        f"{DISCORD_API}/oauth2/token",
        data=token_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    if not token.ok:
        return f"Discord token exchange failed: {token.text}", 502

    access_token = token.json()["access_token"]
    me = requests.get(
        f"{DISCORD_API}/users/@me",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    if not me.ok:
        return "Could not read your Discord account.", 502

    data = me.json()
    discord_id = int(data["id"])
    name = data.get("global_name") or data.get("username") or str(discord_id)
    avatar = ""
    if data.get("avatar"):
        avatar = f"https://cdn.discordapp.com/avatars/{data['id']}/{data['avatar']}.png?size=256"

    c = conn()
    c.execute(
        "INSERT INTO users(id,discord_name,discord_avatar) VALUES(?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET discord_name=excluded.discord_name, discord_avatar=excluded.discord_avatar",
        (discord_id, name, avatar),
    )
    c.commit()
    c.close()
    session["user_id"] = discord_id
    return redirect(url_for("home"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.route("/instagram")
@login_required
def instagram():
    user = current_user()
    c = conn()
    owned_ocs = c.execute(
        "SELECT id, name, username FROM ocs WHERE owner_id=? ORDER BY name",
        (user["id"],)
    ).fetchall()
    owned_ids = {int(oc["id"]) for oc in owned_ocs}
    requested_oc_id = request.args.get("oc_id", type=int)
    if requested_oc_id in owned_ids:
        active_oc_id = requested_oc_id
    else:
        active_oc_id = int(owned_ocs[0]["id"]) if owned_ocs else None

    posts = c.execute("""
        SELECT p.*, o.name, o.username, o.avatar, o.id AS author_oc_id, o.owner_id AS author_owner_id,
               ((SELECT COUNT(*) FROM post_likes l WHERE l.post_id=p.id) +
                (SELECT COUNT(*) FROM oc_post_likes ol WHERE ol.post_id=p.id)) AS like_count,
               EXISTS(
                   SELECT 1 FROM oc_post_likes ol
                   WHERE ol.post_id=p.id AND ol.oc_id=?
               ) AS liked_by_me
        FROM posts p JOIN ocs o ON o.id=p.oc_id
        WHERE p.platform='instagram' ORDER BY p.id DESC
    """, (active_oc_id,)).fetchall()
    replies = c.execute("""
      SELECT r.*, o.name, o.username, o.avatar, o.owner_id AS reply_owner_id,
             (SELECT COUNT(*) FROM reply_likes rl WHERE rl.reply_id=r.id) AS reply_like_count,
             EXISTS(
                 SELECT 1 FROM reply_likes rl
                 WHERE rl.reply_id=r.id AND rl.user_id=?
             ) AS reply_liked_by_me
      FROM replies r JOIN ocs o ON o.id=r.oc_id
      ORDER BY r.id
    """, (current_user()["id"],)).fetchall()
    # Attach carousel media while preserving legacy single-image posts.
    post_rows = []
    for post in posts:
        item = dict(post)
        media = c.execute("SELECT media_url, media_type FROM post_media WHERE post_id=? ORDER BY position, id", (post["id"],)).fetchall()
        item["media_items"] = [dict(m) for m in media]
        if not item["media_items"] and item.get("image"):
            item["media_items"] = [{"media_url": item["image"], "media_type": "image"}]
        post_rows.append(item)
    stories = c.execute("""SELECT s.*, o.name, o.username, o.avatar, o.id AS author_oc_id,
                    o.owner_id AS author_owner_id
        FROM stories s JOIN ocs o ON o.id=s.oc_id
        WHERE s.expires_at > CURRENT_TIMESTAMP ORDER BY s.created_at DESC""").fetchall()
    c.close()
    return render_template(
        "instagram.html", posts=post_rows, replies=replies, stories=stories,
        user=user, owned_ocs=owned_ocs, active_oc_id=active_oc_id
    )


@app.route("/api/story", methods=["POST"])
@login_required
def create_story():
    try:
        oc_id = int(request.form.get("oc_id", ""))
    except (TypeError, ValueError):
        return jsonify(error="Choose an OC."), 400
    c = conn()
    oc = c.execute("SELECT id FROM ocs WHERE id=? AND owner_id=?", (oc_id, current_user()["id"])).fetchone()
    if not oc:
        c.close()
        return jsonify(error="You don't own that OC."), 403
    uploaded = request.files.get("story_media")
    if not uploaded or not uploaded.filename:
        c.close()
        return jsonify(error="Choose a photo or video for your Story."), 400
    ext = os.path.splitext(uploaded.filename)[1].lower()
    image_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    video_exts = {".mp4", ".webm", ".mov", ".m4v"}
    if ext not in image_exts | video_exts:
        c.close()
        return jsonify(error="Use JPG, PNG, WEBP, GIF, MP4, WEBM, MOV, or M4V."), 400
    uploaded.stream.seek(0, 2)
    size = uploaded.stream.tell()
    uploaded.stream.seek(0)
    if size > 50 * 1024 * 1024:
        c.close()
        return jsonify(error="Story media must be 50 MB or smaller."), 400
    media_type = "video" if ext in video_exts else "image"
    try:
        media_url = store_post_photo(uploaded, f"story_{uuid.uuid4().hex}{ext}")
    except Exception as exc:
        c.close()
        print(f"Story upload failed: {exc}")
        return jsonify(error="Could not store the Story. Check Supabase Storage configuration."), 502
    overlay = request.form.get("text_overlay", "").strip()[:500]
    text_color = request.form.get("text_color", "#ffffff")
    if text_color not in {"#ffffff", "#000000", "#ffcc00", "#ff5c8a", "#8be9fd"}:
        text_color = "#ffffff"
    # SQLite and PostgreSQL both accept this UTC timestamp format for comparison.
    expires_at = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + 86400))
    c.execute("INSERT INTO stories(oc_id,media_url,media_type,text_overlay,text_color,expires_at) VALUES(?,?,?,?,?,?)",
              (oc_id, media_url, media_type, overlay, text_color, expires_at))
    c.commit()
    c.close()
    return redirect(url_for("instagram"))


def _get_or_create_instagram_dm(c, oc_a_id, oc_b_id):
    """Return a stable DM conversation id for a pair of OCs."""
    low_id, high_id = sorted((int(oc_a_id), int(oc_b_id)))
    row = c.execute(
        "SELECT id FROM instagram_dm_conversations WHERE oc_low_id=? AND oc_high_id=?",
        (low_id, high_id),
    ).fetchone()
    if row:
        return int(row["id"])
    c.execute(
        "INSERT INTO instagram_dm_conversations(oc_low_id, oc_high_id) VALUES(?,?)",
        (low_id, high_id),
    )
    return int(c.execute("SELECT last_insert_rowid()").fetchone()[0])


@app.route("/api/story/<int:story_id>/reply", methods=["POST"])
@login_required
def reply_to_story(story_id):
    """Turn a Story reply into the first message of an Instagram DM thread."""
    data = request.get_json(silent=True) or request.form
    try:
        oc_id = int(data.get("oc_id", ""))
    except (TypeError, ValueError):
        return jsonify(error="Choose which OC is replying."), 400
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify(error="Write a reply first."), 400
    if len(text) > 1000:
        return jsonify(error="Story replies must be 1,000 characters or fewer."), 400

    user = current_user()
    c = conn()
    oc = c.execute(
        "SELECT id, username, owner_id FROM ocs WHERE id=? AND owner_id=?",
        (oc_id, user["id"]),
    ).fetchone()
    if not oc:
        c.close()
        return jsonify(error="You can only reply as one of your own OCs."), 403

    story = c.execute("""SELECT s.id, s.oc_id AS story_oc_id, s.media_url AS story_media_url,
               s.media_type AS story_media_type, s.text_overlay AS story_text,
               o.owner_id AS story_owner_id, o.username AS story_username
        FROM stories s JOIN ocs o ON o.id=s.oc_id
        WHERE s.id=? AND s.expires_at > CURRENT_TIMESTAMP""", (story_id,)).fetchone()
    if not story:
        c.close()
        return jsonify(error="This Story has expired or is unavailable."), 404
 if int(story["story_oc_id"]) == int(oc_id):
        c.close()
        return jsonify(error="You can't reply to your own Story."), 400

    try:
        conversation_id = _get_or_create_instagram_dm(c, oc_id, int(story["story_oc_id"]))
        c.execute("""INSERT INTO instagram_dm_messages
            (conversation_id, sender_oc_id, text, story_id, story_media_url, story_media_type, story_text, story_username)
            VALUES(?,?,?,?,?,?,?,?)""",
            (conversation_id, oc_id, text, story_id, story["story_media_url"],
             story["story_media_type"] or "image", story["story_text"] or "", story["story_username"] or ""),
        )
        c.commit()
    except Exception as exc:
        c.rollback()
        print(f"Instagram Story-to-DM failed: {exc}")
        c.close()
        return jsonify(error="Couldn't open the Instagram DM for this Story. Please try again."), 500
    c.close()
    return jsonify(
        ok=True,
        message=f"Story reply sent to @{story['story_username']}.",
        conversation_id=conversation_id,
        conversation_url=url_for("instagram_dm_conversation", conversation_id=conversation_id),
    )


@app.route("/api/story/<int:story_id>/replies", methods=["GET"])
@login_required
def get_story_replies(story_id):
    """Allow only the Story owner to view its private replies, with links into DMs."""
    user = current_user()
    c = conn()
    story = c.execute("""SELECT s.id, o.owner_id AS story_owner_id
        FROM stories s JOIN ocs o ON o.id=s.oc_id WHERE s.id=?""", (story_id,)).fetchone()
    if not story:
        c.close()
        return jsonify(error="Story not found."), 404
    if int(story["story_owner_id"]) != int(user["id"]):
        c.close()
        return jsonify(error="Only the Story owner can view replies."), 403

    # Legacy replies created before Story replies became DM messages.
    legacy_rows = c.execute("""SELECT sr.id, sr.text, sr.created_at, o.name, o.username, o.avatar,
                    NULL AS conversation_id
        FROM story_replies sr JOIN ocs o ON o.id=sr.oc_id
        WHERE sr.story_id=? ORDER BY sr.id""", (story_id,)).fetchall()
    dm_rows = c.execute("""SELECT m.id, m.text, m.created_at, o.name, o.username, o.avatar,
                    d.id AS conversation_id
        FROM instagram_dm_messages m
        JOIN instagram_dm_conversations d ON d.id=m.conversation_id
        JOIN ocs o ON o.id=m.sender_oc_id
        WHERE m.story_id=? AND o.owner_id<>?
        ORDER BY m.id""", (story_id, user["id"])).fetchall()
    c.close()
    replies = []
    for row in list(legacy_rows) + list(dm_rows):
        item = dict(row)
        item["conversation_url"] = (
            url_for("instagram_dm_conversation", conversation_id=int(item["conversation_id"]))
            if item.get("conversation_id") else ""
        )
        replies.append(item)
    replies.sort(key=lambda item: str(item.get("created_at") or ""))
    return jsonify(ok=True, replies=replies)


@app.route("/instagram/dms")
@login_required
def instagram_dms():
    """Instagram-style DM inbox, separate from the OC iMessage feature."""
    user = current_user()
    c = conn()
    rows = c.execute("""SELECT d.id, d.created_at,
          lo.id AS low_oc_id, lo.name AS low_name, lo.username AS low_username,
          lo.avatar AS low_avatar, lo.owner_id AS low_owner_id,
          hi.id AS high_oc_id, hi.name AS high_name, hi.username AS high_username,
          hi.avatar AS high_avatar, hi.owner_id AS high_owner_id,
          (SELECT text FROM instagram_dm_messages m WHERE m.conversation_id=d.id ORDER BY m.id DESC LIMIT 1) AS last_text,
          (SELECT image_url FROM instagram_dm_messages m WHERE m.conversation_id=d.id ORDER BY m.id DESC LIMIT 1) AS last_image_url,
          (SELECT story_media_url FROM instagram_dm_messages m WHERE m.conversation_id=d.id ORDER BY m.id DESC LIMIT 1) AS last_story_media_url
        FROM instagram_dm_conversations d
        JOIN ocs lo ON lo.id=d.oc_low_id
        JOIN ocs hi ON hi.id=d.oc_high_id
        WHERE lo.owner_id=? OR hi.owner_id=?
        ORDER BY COALESCE((SELECT MAX(m.id) FROM instagram_dm_messages m WHERE m.conversation_id=d.id), 0) DESC""",
        (user["id"], user["id"])).fetchall()
    conversations = []
    for row in rows:
        item = dict(row)
        if int(item["low_owner_id"]) == int(user["id"]):
            item["my_oc"] = {"id": item["low_oc_id"], "name": item["low_name"], "username": item["low_username"], "avatar": item["low_avatar"]}
            item["other_oc"] = {"id": item["high_oc_id"], "name": item["high_name"], "username": item["high_username"], "avatar": item["high_avatar"]}
        else:
            item["my_oc"] = {"id": item["high_oc_id"], "name": item["high_name"], "username": item["high_username"], "avatar": item["high_avatar"]}
            item["other_oc"] = {"id": item["low_oc_id"], "name": item["low_name"], "username": item["low_username"], "avatar": item["low_avatar"]}
        conversations.append(item)
    c.close()
    return render_template("instagram_dms.html", conversations=conversations, user=user)


@app.route("/instagram/dms/new", methods=["GET", "POST"])
@login_required
def instagram_dm_new():
    """Start an Instagram DM directly, without requiring a Story reply."""
    user = current_user()
    c = conn()
    owned_ocs = c.execute(
        "SELECT id, name, username, avatar FROM ocs WHERE owner_id=? ORDER BY name",
        (user["id"],),
    ).fetchall()
    other_ocs = c.execute(
        "SELECT id, name, username, avatar FROM ocs WHERE owner_id<>? ORDER BY username, name",
        (user["id"],),
    ).fetchall()
    error = ""
    if request.method == "POST":
        try:
            sender_id = int(request.form.get("sender_oc_id", ""))
            recipient_id = int(request.form.get("recipient_oc_id", ""))
        except (TypeError, ValueError):
            sender_id = recipient_id = 0
            error = "Choose both the OC sending the message and the OC receiving it."
        sender = next((oc for oc in owned_ocs if int(oc["id"]) == sender_id), None)
        recipient = next((oc for oc in other_ocs if int(oc["id"]) == recipient_id), None)
        if not error and not sender:
            error = "You can only send messages as one of your own OCs."
        elif not error and not recipient:
            error = "Choose an OC belonging to another account."
        if not error:
            try:
                conversation_id = _get_or_create_instagram_dm(c, sender_id, recipient_id)
                c.commit()
                c.close()
                return redirect(url_for("instagram_dm_conversation", conversation_id=conversation_id))
            except Exception as exc:
                c.rollback()
                print(f"Could not start Instagram DM: {exc}")
                error = "Couldn't start that conversation. Please try again."
    selected_sender = request.form.get("sender_oc_id", "") if request.method == "POST" else request.args.get("as_oc_id", "")
    selected_recipient = request.form.get("recipient_oc_id", "") if request.method == "POST" else request.args.get("to_oc_id", "")
    if not selected_sender and owned_ocs:
        selected_sender = str(owned_ocs[0]["id"])
    c.close()
    return render_template("instagram_dm_new.html", owned_ocs=owned_ocs, other_ocs=other_ocs,
                           selected_sender=selected_sender, selected_recipient=selected_recipient,
                           user=user, error=error)


@app.route("/instagram/dms/<int:conversation_id>", methods=["GET", "POST"])
@login_required
def instagram_dm_conversation(conversation_id):
    """Read or send a private Instagram DM with story previews and media."""
    user = current_user()
    c = conn()
    conv = c.execute("""SELECT d.*,
          lo.name AS low_name, lo.username AS low_username, lo.avatar AS low_avatar, lo.owner_id AS low_owner_id,
          hi.name AS high_name, hi.username AS high_username, hi.avatar AS high_avatar, hi.owner_id AS high_owner_id
        FROM instagram_dm_conversations d
        JOIN ocs lo ON lo.id=d.oc_low_id
        JOIN ocs hi ON hi.id=d.oc_high_id
        WHERE d.id=?""", (conversation_id,)).fetchone()
    if not conv:
        c.close()
        return "Instagram DM not found.", 404
    low = {"id": conv["oc_low_id"], "name": conv["low_name"], "username": conv["low_username"], "avatar": conv["low_avatar"], "owner_id": conv["low_owner_id"]}
    high = {"id": conv["oc_high_id"], "name": conv["high_name"], "username": conv["high_username"], "avatar": conv["high_avatar"], "owner_id": conv["high_owner_id"]}
    if int(low["owner_id"]) == int(user["id"]):
        my_oc, other_oc = low, high
    elif int(high["owner_id"]) == int(user["id"]):
        my_oc, other_oc = high, low
    else:
        c.close()
        return "You aren't part of this Instagram DM.", 403

    if request.method == "POST":
        text = (request.form.get("text") or "").strip()[:2000]
        image_url = ""
        uploaded = request.files.get("media_file")
        if uploaded and uploaded.filename:
            ext = Path(uploaded.filename).suffix.lower()
            if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
                c.close()
                return render_template("instagram_dm_conversation.html", conversation=conv, messages=[], user=user,
                    my_oc=my_oc, other_oc=other_oc, error="Use a JPG, PNG, WEBP, or GIF image."), 400
            uploaded.stream.seek(0, 2)
            size = uploaded.stream.tell()
            uploaded.stream.seek(0)
            if size > 20 * 1024 * 1024:
                c.close()
                return render_template("instagram_dm_conversation.html", conversation=conv, messages=[], user=user,
                    my_oc=my_oc, other_oc=other_oc, error="DM images must be 20 MB or smaller."), 400
            try:
                image_url = store_post_photo(uploaded, f"igdm_{conversation_id}_{uuid.uuid4().hex}{ext}")
            except Exception as exc:
                c.close()
                print(f"Instagram DM media upload failed: {exc}")
                return render_template("instagram_dm_conversation.html", conversation=conv, messages=[], user=user,
                    my_oc=my_oc, other_oc=other_oc, error="The image couldn't be uploaded. Please try again."), 502
        if not text and not image_url:
            c.close()
            return redirect(url_for("instagram_dm_conversation", conversation_id=conversation_id))
        c.execute("""INSERT INTO instagram_dm_messages(conversation_id, sender_oc_id, text, image_url)
            VALUES(?,?,?,?)""", (conversation_id, int(my_oc["id"]), text, image_url))
        c.commit()
        c.close()
        return redirect(url_for("instagram_dm_conversation", conversation_id=conversation_id))

    msg_rows = c.execute("""SELECT m.*, o.name AS sender_name, o.username AS sender_username,
            o.avatar AS sender_avatar, o.owner_id AS sender_owner_id
        FROM instagram_dm_messages m JOIN ocs o ON o.id=m.sender_oc_id
        WHERE m.conversation_id=? ORDER BY m.id""", (conversation_id,)).fetchall()
    messages = [dict(row) for row in msg_rows]
    c.close()
    return render_template("instagram_dm_conversation.html", conversation=conv, messages=messages,
                           user=user, my_oc=my_oc, other_oc=other_oc, error="")


def _attach_post_media(posts):
    """Attach ordered media rows; preserve legacy one-image posts."""
    result = []
    c = conn()
    for post in posts:
        item = dict(post)
        media = c.execute(
            "SELECT media_url, media_type FROM post_media WHERE post_id=? ORDER BY position, id",
            (post["id"],),
        ).fetchall()
        item["media_items"] = [dict(m) for m in media]
        if not item["media_items"] and item.get("image"):
            item["media_items"] = [{"media_url": item["image"], "media_type": "image"}]
        result.append(item)
    c.close()
    return result


@app.route("/twitter")
@login_required
def twitter():
    user = current_user()
    c = conn()
    owned_ocs = c.execute(
        "SELECT id, name, username, avatar FROM ocs WHERE owner_id=? ORDER BY name",
        (user["id"],)
    ).fetchall()
    owned_ids = {int(oc["id"]) for oc in owned_ocs}
    requested_oc_id = request.args.get("oc_id", type=int)
    if requested_oc_id in owned_ids:
        active_oc_id = requested_oc_id
    else:
        active_oc_id = int(owned_ocs[0]["id"]) if owned_ocs else None

    posts = c.execute("""
        SELECT p.*, o.name, o.username, o.avatar, o.id AS author_oc_id, o.owner_id AS author_owner_id,
            ((SELECT COUNT(*) FROM post_likes l WHERE l.post_id=p.id) +
             (SELECT COUNT(*) FROM oc_tweet_likes ol WHERE ol.post_id=p.id)) AS like_count,
            EXISTS(
                SELECT 1 FROM oc_tweet_likes ol
                WHERE ol.post_id=p.id AND ol.oc_id=?
            ) AS liked_by_me,
            ((SELECT COUNT(*) FROM retweets rt WHERE rt.post_id=p.id) +
             (SELECT COUNT(*) FROM oc_tweet_retweets ort WHERE ort.post_id=p.id)) AS retweet_count,
            EXISTS(
                SELECT 1 FROM oc_tweet_retweets ort
                WHERE ort.post_id=p.id AND ort.oc_id=?
            ) AS retweeted_by_me
        FROM posts p
        JOIN ocs o ON o.id=p.oc_id
        WHERE p.platform='twitter'
        ORDER BY p.id DESC
    """, (active_oc_id, active_oc_id)).fetchall()

    replies = c.execute("""
        SELECT r.*, o.name, o.username, o.avatar, o.owner_id AS reply_owner_id,
            (SELECT COUNT(*) FROM reply_likes rl WHERE rl.reply_id=r.id) AS reply_like_count,
            EXISTS(
                SELECT 1 FROM reply_likes rl
                WHERE rl.reply_id=r.id AND rl.user_id=?
            ) AS reply_liked_by_me
        FROM replies r
        JOIN ocs o ON o.id=r.oc_id
        ORDER BY r.id
    """, (user["id"],)).fetchall()
    c.close()
    posts = _attach_post_media(posts)
    return render_template(
        "twitter.html", posts=posts, replies=replies, user=user,
        owned_ocs=owned_ocs, active_oc_id=active_oc_id
    )

@app.route("/twitter/profile/<int:oc_id>")
@login_required
def twitter_profile(oc_id):
    user = current_user()
    c = conn()
    oc = c.execute("SELECT * FROM ocs WHERE id=?", (oc_id,)).fetchone()
    if not oc:
        c.close()
        return "Not found", 404

    owned_ocs = c.execute(
        "SELECT * FROM ocs WHERE owner_id=? ORDER BY name", (user["id"],)
    ).fetchall()
    requested_actor = request.args.get("actor", "")
    try:
        requested_actor = int(requested_actor) if requested_actor else None
    except (TypeError, ValueError):
        requested_actor = None
    active_oc = next(
        (item for item in owned_ocs if int(item["id"]) == requested_actor),
        owned_ocs[0] if owned_ocs else None,
    )
    active_oc_id = int(active_oc["id"]) if active_oc else None

    posts = c.execute("""
        SELECT p.*, o.name, o.username, o.avatar, o.id AS author_oc_id, o.owner_id AS author_owner_id,
            (SELECT COUNT(*) FROM oc_tweet_likes l WHERE l.post_id=p.id) AS like_count,
            EXISTS(
                SELECT 1 FROM oc_tweet_likes l
                WHERE l.post_id=p.id AND l.oc_id=?
            ) AS liked_by_me,
            ((SELECT COUNT(*) FROM retweets rt WHERE rt.post_id=p.id) +
             (SELECT COUNT(*) FROM oc_tweet_retweets ort WHERE ort.post_id=p.id)) AS retweet_count,
            EXISTS(
                SELECT 1 FROM oc_tweet_retweets ort
                WHERE ort.post_id=p.id AND ort.oc_id=?
            ) AS retweeted_by_me
        FROM posts p JOIN ocs o ON o.id=p.oc_id
        WHERE p.platform='twitter' AND p.oc_id=?
        ORDER BY p.id DESC
    """, (active_oc_id, active_oc_id, oc_id)).fetchall()

    replies = c.execute("""
        SELECT r.*, o.name, o.username, o.avatar, o.owner_id AS reply_owner_id,
            (SELECT COUNT(*) FROM reply_likes rl WHERE rl.reply_id=r.id) AS reply_like_count,
            EXISTS(
                SELECT 1 FROM reply_likes rl
                WHERE rl.reply_id=r.id AND rl.user_id=?
            ) AS reply_liked_by_me
        FROM replies r JOIN ocs o ON o.id=r.oc_id
        ORDER BY r.id
    """, (user["id"],)).fetchall()

    follower_count = c.execute(
        "SELECT COUNT(*) FROM oc_follows WHERE followed_oc_id=?", (oc_id,)
    ).fetchone()[0]
    following_count = c.execute(
        "SELECT COUNT(*) FROM oc_follows WHERE follower_oc_id=?", (oc_id,)
    ).fetchone()[0]
    is_owner = int(oc["owner_id"]) == int(user["id"])
    is_following = False
    if active_oc_id and active_oc_id != oc_id:
        is_following = bool(c.execute(
            "SELECT 1 FROM oc_follows WHERE follower_oc_id=? AND followed_oc_id=?",
            (active_oc_id, oc_id),
        ).fetchone())
    c.close()
    posts = _attach_post_media(posts)
    return render_template(
        "twitter_profile.html", oc=oc, posts=posts, replies=replies,
        user=user, owned_ocs=owned_ocs, active_oc_id=active_oc_id,
        follower_count=follower_count, following_count=following_count,
        is_owner=is_owner, is_following=is_following,
    )



@app.route("/following")
@login_required
def following_feed():
    """Show followed OCs' latest posts and the selected OC's following/follower lists."""
    user = current_user()
    c = conn()
    owned_ocs = c.execute(
        "SELECT id, name, username, avatar, owner_id FROM ocs WHERE owner_id=? ORDER BY name",
        (user["id"],),
    ).fetchall()
    owned_ids = {int(row["id"]) for row in owned_ocs}
    requested_oc_id = request.args.get("oc_id", type=int)
    if requested_oc_id in owned_ids:
        active_oc_id = requested_oc_id
    else:
        active_oc_id = int(owned_ocs[0]["id"]) if owned_ocs else None

    view = (request.args.get("view", "feed") or "feed").lower()
    if view not in {"feed", "following", "followers"}:
        view = "feed"

    following_ocs = []
    follower_ocs = []
    posts = []
    stories = []
    follower_count = 0
    following_count = 0

    if active_oc_id:
        following_ocs = c.execute("""
            SELECT o.id, o.name, o.username, o.avatar, o.bio, o.owner_id, f.created_at
            FROM oc_follows f JOIN ocs o ON o.id=f.followed_oc_id
            WHERE f.follower_oc_id=?
            ORDER BY lower(o.username), lower(o.name)
        """, (active_oc_id,)).fetchall()
        follower_ocs = c.execute("""
            SELECT o.id, o.name, o.username, o.avatar, o.bio, o.owner_id, f.created_at
            FROM oc_follows f JOIN ocs o ON o.id=f.follower_oc_id
            WHERE f.followed_oc_id=?
            ORDER BY lower(o.username), lower(o.name)
        """, (active_oc_id,)).fetchall()
        following_count = len(following_ocs)
        follower_count = len(follower_ocs)

        # Feed is limited to posts from accounts this OC follows, across Instagram and Twitter/X.
        if view == "feed":
            posts = c.execute("""
                SELECT p.*, o.name, o.username, o.avatar,
                       o.id AS author_oc_id, o.owner_id AS author_owner_id,
                       CASE WHEN p.platform='instagram' THEN
                           (SELECT COUNT(*) FROM post_likes pl WHERE pl.post_id=p.id) +
                           (SELECT COUNT(*) FROM oc_post_likes opl WHERE opl.post_id=p.id)
                       ELSE
                           (SELECT COUNT(*) FROM post_likes pl WHERE pl.post_id=p.id) +
                           (SELECT COUNT(*) FROM oc_tweet_likes otl WHERE otl.post_id=p.id)
                       END AS like_count
                FROM posts p
                JOIN ocs o ON o.id=p.oc_id
                JOIN oc_follows f ON f.followed_oc_id=o.id AND f.follower_oc_id=?
                WHERE p.platform IN ('instagram', 'twitter')
                ORDER BY p.id DESC
                LIMIT 100
            """, (active_oc_id,)).fetchall()
            posts = _attach_post_media(posts)
            stories = c.execute("""
                SELECT s.*, o.name, o.username, o.avatar, o.id AS author_oc_id
                FROM stories s JOIN ocs o ON o.id=s.oc_id
                JOIN oc_follows f ON f.followed_oc_id=o.id AND f.follower_oc_id=?
                WHERE s.expires_at > CURRENT_TIMESTAMP
                ORDER BY s.created_at DESC LIMIT 30
            """, (active_oc_id,)).fetchall()

    c.close()
    return render_template(
        "following.html", user=user, owned_ocs=owned_ocs,
        active_oc_id=active_oc_id, view=view, posts=posts,
        following_ocs=following_ocs, follower_ocs=follower_ocs,
        follower_count=follower_count, following_count=following_count,
        stories=stories,
    )

@app.route("/messages")
@login_required
def messages():
    c = conn()
    user = current_user()
    user_id = user["id"]
    chats = c.execute("""
      SELECT DISTINCT c.*,
        (SELECT COUNT(*)
         FROM messages m
         JOIN ocs sender_oc ON sender_oc.id=m.oc_id
         WHERE m.chat_id=c.id
           AND m.id > COALESCE(
             (SELECT cr.last_read_message_id FROM chat_reads cr
              WHERE cr.user_id=? AND cr.chat_id=c.id), 0)
           AND sender_oc.owner_id<>?) AS unread_count
      FROM chats c
      JOIN chat_users cu ON cu.chat_id=c.id
      WHERE cu.user_id=?
      ORDER BY c.id DESC
    """, (user_id, user_id, user_id)).fetchall()
    total_unread = sum(int(chat["unread_count"] or 0) for chat in chats)
    c.close()
    return render_template("messages.html", chats=chats, total_unread=total_unread, user=user)


@app.route("/chat/new", methods=["GET", "POST"])
@login_required
def new_chat():
    user = current_user()
    c = conn()

    chat_type = request.args.get("type", "group")
    if chat_type not in ("direct", "group"):
        chat_type = "group"

    own_ocs = c.execute(
        "SELECT * FROM ocs WHERE owner_id=? ORDER BY name",
        (user["id"],)
    ).fetchall()

    all_ocs = c.execute(
        "SELECT * FROM ocs ORDER BY name"
    ).fetchall()

    if request.method == "POST":
        name = request.form.get("name", "").strip()

        if chat_type == "direct":
            selected = request.form.getlist("oc_ids")

            if len(selected) != 1:
                c.close()
                return render_template(
                    "chat_new.html",
                    ocs=all_ocs,
                    own_ocs=own_ocs,
                    user=user,
                    chat_type=chat_type,
                    error="Choose one OC to start the 1-on-1 chat."
                )

            try:
                other_oc_id = int(selected[0])
            except ValueError:
                c.close()
                return render_template(
                    "chat_new.html",
                    ocs=all_ocs,
                    own_ocs=own_ocs,
                    user=user,
                    chat_type=chat_type,
                    error="Invalid OC."
                )

            other_oc = c.execute(
                "SELECT * FROM ocs WHERE id=?",
                (other_oc_id,)
            ).fetchone()

            if not other_oc:
                c.close()
                return render_template(
                    "chat_new.html",
                    ocs=all_ocs,
                    own_ocs=own_ocs,
                    user=user,
                    chat_type=chat_type,
                    error="That OC could not be found."
                )

            if other_oc["owner_id"] == user["id"]:
                c.close()
                return render_template(
                    "chat_new.html",
                    ocs=all_ocs,
                    own_ocs=own_ocs,
                    user=user,
                    chat_type=chat_type,
                    error="Choose another person's OC for a 1-on-1 chat."
                )

            if not own_ocs:
                c.close()
                return render_template(
                    "chat_new.html",
                    ocs=all_ocs,
                    own_ocs=own_ocs,
                    user=user,
                    chat_type=chat_type,
                    error="You need one of your own OCs first."
                )

            my_oc_id = request.form.get("my_oc_id")
            try:
                my_oc_id = int(my_oc_id)
            except (TypeError, ValueError):
                c.close()
                return render_template("chat_new.html", ocs=all_ocs, own_ocs=own_ocs, user=user, chat_type=chat_type, error="Choose which of your OCs you want to use for this chat.")
            my_oc = c.execute("SELECT * FROM ocs WHERE id=? AND owner_id=?", (my_oc_id, user["id"])).fetchone()
            if not my_oc:
                c.close()
                return render_template("chat_new.html", ocs=all_ocs, own_ocs=own_ocs, user=user, chat_type=chat_type, error="That is not one of your OCs.")
            chat_name = name or other_oc["name"]

            c.execute(
                "INSERT INTO chats(name,owner_id) VALUES(?,?)",
                (chat_name, user["id"])
            )
            chat_id = c.execute(
                "SELECT last_insert_rowid()"
            ).fetchone()[0]

            c.executemany(
                "INSERT OR IGNORE INTO chat_members(chat_id,oc_id) VALUES(?,?)",
                [(chat_id, my_oc["id"]), (chat_id, other_oc["id"])]
            )
            c.executemany(
                "INSERT OR IGNORE INTO chat_users(chat_id,user_id) VALUES(?,?)",
                [(chat_id, user["id"]), (chat_id, other_oc["owner_id"])]
            )

        else:
            selected = request.form.getlist("oc_ids")
            valid = []

            for value in selected:
                try:
                    oid = int(value)
                except ValueError:
                    continue

                row = c.execute(
                    "SELECT id FROM ocs WHERE id=? AND owner_id=?",
                    (oid, user["id"])
                ).fetchone()

                if row:
                    valid.append(oid)

            if not valid:
                c.close()
                return render_template(
                    "chat_new.html",
                    ocs=all_ocs,
                    own_ocs=own_ocs,
                    user=user,
                    chat_type=chat_type,
                    error="Choose at least one of your OCs."
                )

            chat_name = name or "New Group Chat"

            c.execute(
                "INSERT INTO chats(name,owner_id) VALUES(?,?)",
                (chat_name, user["id"])
            )
            chat_id = c.execute(
                "SELECT last_insert_rowid()"
            ).fetchone()[0]

            c.executemany(
                "INSERT OR IGNORE INTO chat_members(chat_id,oc_id) VALUES(?,?)",
                [(chat_id, oid) for oid in valid]
            )
            c.executemany(
                "INSERT OR IGNORE INTO chat_users(chat_id,user_id) VALUES(?,?)",
                [(chat_id, user["id"])]
            )

        c.commit()
        c.close()
        return redirect(url_for("chat", chat_id=chat_id))

    c.close()

    return render_template(
        "chat_new.html",
        ocs=all_ocs,
        own_ocs=own_ocs,
        user=user,
        chat_type=chat_type
    )


@app.route("/chat/<int:chat_id>")
@login_required
def chat(chat_id):
    c = conn()
    chat_row = c.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
    if not chat_row:
        c.close()
        return "Chat not found", 404
    members = c.execute("""
      SELECT o.* FROM ocs o JOIN chat_members cm ON cm.oc_id=o.id
      WHERE cm.chat_id=? ORDER BY o.name
    """, (chat_id,)).fetchall()
    # Access is granted to Discord users listed in chat_users.
    allowed = c.execute(
        "SELECT 1 FROM chat_users WHERE chat_id=? AND user_id=?",
        (chat_id, current_user()["id"])
    ).fetchone()
    if not allowed:
        c.close()
        return "You are not a member of this chat.", 403
    user = current_user()
    msgs = c.execute("""
      SELECT m.*, o.name, o.username, o.avatar, o.owner_id
      FROM messages m JOIN ocs o ON o.id=m.oc_id
      WHERE m.chat_id=? ORDER BY m.id
    """, (chat_id,)).fetchall()
    # Opening a conversation marks all messages currently in it as read for this user.
    if msgs:
        last_read_id = max(int(msg["id"]) for msg in msgs)
        c.execute("""
          INSERT INTO chat_reads(user_id, chat_id, last_read_message_id)
          VALUES(?,?,?)
          ON CONFLICT(user_id, chat_id) DO UPDATE
          SET last_read_message_id=excluded.last_read_message_id
        """, (user["id"], chat_id, last_read_id))
        c.commit()
    mine = [m for m in members if m["owner_id"] == user["id"]]
    all_ocs = c.execute(
        "SELECT id, name, username, avatar, owner_id FROM ocs ORDER BY name"
    ).fetchall()
    unread_total_row = c.execute("""
      SELECT COUNT(*) AS total
      FROM messages m
      JOIN chats ch ON ch.id=m.chat_id
      JOIN chat_users cu ON cu.chat_id=ch.id AND cu.user_id=?
      JOIN ocs sender_oc ON sender_oc.id=m.oc_id
      LEFT JOIN chat_reads cr ON cr.chat_id=m.chat_id AND cr.user_id=cu.user_id
      WHERE m.id > COALESCE(cr.last_read_message_id, 0)
        AND sender_oc.owner_id<>?
    """, (user["id"], user["id"])).fetchone()
    total_unread = int(unread_total_row["total"] or 0)
    preference = c.execute(
        "SELECT wallpaper FROM chat_preferences WHERE user_id=? AND chat_id=?",
        (user["id"], chat_id),
    ).fetchone()
    chat_wallpaper = str(preference["wallpaper"] if preference else "preset:classic")
    c.close()
    return render_template("chat.html", chat=chat_row, members=members, mine=mine,
                           messages=msgs, user=user, all_ocs=all_ocs,
                           total_unread=total_unread, chat_wallpaper=chat_wallpaper)


@app.route("/oc/new", methods=["GET", "POST"])
@login_required
def new_oc():
    user = current_user()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        username = request.form.get("username", "").strip().lstrip("@")
        if not name or not username:
            return render_template("oc_new.html", user=user, error="Name and username are required.")
        c = conn()
        try:
            c.execute(
                "INSERT INTO ocs(owner_id,name,username,bio,avatar,banner) VALUES(?,?,?,?,?,?)",
                (user["id"], name, username, request.form.get("bio", "").strip(),
                 request.form.get("avatar", "").strip(), request.form.get("banner", "").strip()),
            )
            c.commit()
        except DB_INTEGRITY_ERRORS:
            c.close()
            return render_template("oc_new.html", user=user, error="You already have an OC with that username.")
        c.close()
        return redirect(url_for("home"))
    return render_template("oc_new.html", user=user)


@app.route("/oc/<int:oc_id>/edit", methods=["GET", "POST"])
@login_required
def edit_oc(oc_id):
    user = current_user()
    c = conn()
    oc = c.execute("SELECT * FROM ocs WHERE id=?", (oc_id,)).fetchone()
    if not oc:
        c.close()
        return "Not found", 404
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        username = request.form.get("username", "").strip().lstrip("@")
        avatar_url = request.form.get("avatar", "").strip()
        uploaded_avatar = request.files.get("avatar_file")
        if uploaded_avatar and uploaded_avatar.filename:
            extension = Path(uploaded_avatar.filename).suffix.lower()
            allowed_extensions = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
            if extension not in allowed_extensions:
                c.close()
                return render_template(
                    "oc_edit.html", oc=oc, user=user,
                    error="Please upload a JPG, PNG, WEBP, or GIF profile photo."
                )
            uploaded_avatar.stream.seek(0, 2)
            size = uploaded_avatar.stream.tell()
            uploaded_avatar.stream.seek(0)
            if size > 10 * 1024 * 1024:
                c.close()
                return render_template(
                    "oc_edit.html", oc=oc, user=user,
                    error="Profile photos must be 10 MB or smaller."
                )
            try:
                avatar_url = store_post_photo(
                    uploaded_avatar, f"avatar_{oc_id}_{uuid.uuid4().hex}{extension}"
                )
            except Exception as exc:
                c.close()
                print(f"OC profile photo upload failed: {exc}")
                return render_template(
                    "oc_edit.html", oc=oc, user=user,
                    error="Could not upload the profile photo. Check Supabase Storage configuration and try again."
                )

        banner_url = request.form.get("banner", "").strip()
        uploaded_banner = request.files.get("banner_file")
        if uploaded_banner and uploaded_banner.filename:
            extension = Path(uploaded_banner.filename).suffix.lower()
            allowed_extensions = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
            if extension not in allowed_extensions:
                c.close()
                return render_template(
                    "oc_edit.html", oc=oc, user=user,
                    error="Please upload a JPG, PNG, WEBP, or GIF banner."
                )
            # Limit banner files to 10 MiB before uploading to storage.
            uploaded_banner.stream.seek(0, 2)
            size = uploaded_banner.stream.tell()
            uploaded_banner.stream.seek(0)
            if size > 10 * 1024 * 1024:
                c.close()
                return render_template(
                    "oc_edit.html", oc=oc, user=user,
                    error="Banner images must be 10 MB or smaller."
                )
            try:
                banner_url = store_post_photo(
                    uploaded_banner, f"banner_{oc_id}_{uuid.uuid4().hex}{extension}"
                )
            except Exception as exc:
                c.close()
                print(f"OC banner upload failed: {exc}")
                return render_template(
                    "oc_edit.html", oc=oc, user=user,
                    error="Could not upload the banner. Check Supabase Storage configuration and try again."
                )

        try:
            c.execute("""
              UPDATE ocs SET name=?,username=?,bio=?,avatar=?,banner=? WHERE id=? AND owner_id=?
            """, (name, username, request.form.get("bio", "").strip(),
                  avatar_url, banner_url, oc_id, user["id"]))
            c.commit()
        except DB_INTEGRITY_ERRORS:
            c.close()
            return render_template("oc_edit.html", oc=oc, user=user, error="That username is already used by another one of your OCs.")
        c.close()
        return redirect(url_for("profile", oc_id=oc_id))
    c.close()
    return render_template("oc_edit.html", oc=oc, user=user)


@app.route("/oc/<int:oc_id>")
@login_required
def profile(oc_id):
    user = current_user()
    c = conn()
    oc = c.execute("SELECT * FROM ocs WHERE id=?", (oc_id,)).fetchone()
    if not oc:
        c.close()
        return "Not found", 404
    posts = c.execute("SELECT * FROM posts WHERE oc_id=? AND platform='instagram' ORDER BY id DESC", (oc_id,)).fetchall()
    posts = _attach_post_media(posts)
    is_owner = int(oc["owner_id"]) == int(user["id"])
    owned_ocs = c.execute("SELECT id, name, username, avatar FROM ocs WHERE owner_id=? ORDER BY name", (user["id"],)).fetchall()
    mutual_connections = []
    if int(oc["owner_id"]) != int(user["id"]):
        mutual_connections = c.execute("""SELECT DISTINCT mutual.id, mutual.name, mutual.username, mutual.avatar
            FROM oc_follows follows_target
            JOIN ocs mutual ON mutual.id=follows_target.follower_oc_id
            JOIN oc_follows actor_follows ON actor_follows.followed_oc_id=mutual.id
            JOIN ocs actor ON actor.id=actor_follows.follower_oc_id
            WHERE follows_target.followed_oc_id=? AND actor.owner_id=?
              AND actor.id<>? AND mutual.id<>? AND mutual.owner_id<>?
            ORDER BY mutual.username""",
            (oc_id, user["id"], oc_id, oc_id, user["id"])).fetchall()
    c.close()
    return render_template("profile.html", oc=oc, posts=posts, user=user, owned_ocs=owned_ocs,
                           is_owner=is_owner, mutual_connections=mutual_connections,
                           mutual_count=len(mutual_connections))


@app.route("/api/oc-follow", methods=["POST"])
@login_required
def toggle_oc_follow():
    data = request.get_json(silent=True) or request.form
    try:
        follower_oc_id = int(data.get("follower_oc_id"))
        followed_oc_id = int(data.get("followed_oc_id"))
    except (TypeError, ValueError):
        return jsonify(error="Choose an OC to follow as"), 400

    if follower_oc_id == followed_oc_id:
        return jsonify(error="An OC cannot follow itself"), 400

    user = current_user()
    c = conn()
    actor = c.execute(
        "SELECT id FROM ocs WHERE id=? AND owner_id=?",
        (follower_oc_id, user["id"]),
    ).fetchone()
    target = c.execute(
        "SELECT id FROM ocs WHERE id=?",
        (followed_oc_id,),
    ).fetchone()
    if not actor:
        c.close()
        return jsonify(error="You can only follow as one of your own OCs"), 403
    if not target:
        c.close()
        return jsonify(error="Profile not found"), 404

    existing = c.execute(
        "SELECT 1 FROM oc_follows WHERE follower_oc_id=? AND followed_oc_id=?",
        (follower_oc_id, followed_oc_id),
    ).fetchone()
    if existing:
        c.execute(
            "DELETE FROM oc_follows WHERE follower_oc_id=? AND followed_oc_id=?",
            (follower_oc_id, followed_oc_id),
        )
        following = False
    else:
        c.execute(
            "INSERT OR IGNORE INTO oc_follows(follower_oc_id, followed_oc_id) VALUES(?, ?)",
            (follower_oc_id, followed_oc_id),
        )
        following = True
    c.commit()
    followers = c.execute(
        "SELECT COUNT(*) FROM oc_follows WHERE followed_oc_id=?",
        (followed_oc_id,),
    ).fetchone()[0]
    following_count = c.execute(
        "SELECT COUNT(*) FROM oc_follows WHERE follower_oc_id=?",
        (followed_oc_id,),
    ).fetchone()[0]
    c.close()
    return jsonify(ok=True, following=following, followers=followers, following_count=following_count)


@app.route("/api/oc-remove-follower", methods=["POST"])
@login_required
def remove_oc_follower():
    """Let an OC owner remove another OC's follow from their own OC."""
    data = request.get_json(silent=True) or request.form
    try:
        target_oc_id = int(data.get("target_oc_id"))
        follower_oc_id = int(data.get("follower_oc_id"))
    except (TypeError, ValueError):
        return jsonify(error="Invalid follower selection."), 400
    user = current_user()
    c = conn()
    target = c.execute("SELECT id FROM ocs WHERE id=? AND owner_id=?", (target_oc_id, user["id"])).fetchone()
    follower = c.execute("SELECT id FROM ocs WHERE id=?", (follower_oc_id,)).fetchone()
    if not target or not follower:
        c.close()
        return jsonify(error="You can only remove a follower from one of your own OCs."), 403
    c.execute("DELETE FROM oc_follows WHERE follower_oc_id=? AND followed_oc_id=?", (follower_oc_id, target_oc_id))
    c.commit()
    followers = c.execute("SELECT COUNT(*) FROM oc_follows WHERE followed_oc_id=?", (target_oc_id,)).fetchone()[0]
    c.close()
    return jsonify(ok=True, followers=followers)


@app.route("/api/post/delete", methods=["POST"])
@login_required
def delete_post():
    data = request.get_json(silent=True) or request.form
    try:
        post_id = int(data.get("post_id"))
    except (TypeError, ValueError):
        return jsonify(error="Invalid post ID"), 400

    c = conn()
    owned_post = c.execute("""
        SELECT p.id
        FROM posts p JOIN ocs o ON o.id=p.oc_id
        WHERE p.id=? AND o.owner_id=?
    """, (post_id, current_user()["id"])).fetchone()
    if not owned_post:
        c.close()
        return jsonify(error="You can only delete posts made by your own OCs."), 403

    # Remove related records before the post so likes/retweets/comments don't linger.
    c.execute("DELETE FROM reply_likes WHERE reply_id IN (SELECT id FROM replies WHERE post_id=?)", (post_id,))
    c.execute("DELETE FROM replies WHERE post_id=?", (post_id,))
    c.execute("DELETE FROM post_likes WHERE post_id=?", (post_id,))
    c.execute("DELETE FROM retweets WHERE post_id=?", (post_id,))
    c.execute("DELETE FROM oc_tweet_likes WHERE post_id=?", (post_id,))
    c.execute("DELETE FROM oc_tweet_retweets WHERE post_id=?", (post_id,))
    c.execute("DELETE FROM posts WHERE id=?", (post_id,))
    c.commit()
    c.close()
    return jsonify(ok=True, post_id=post_id)


@app.route("/api/post", methods=["POST"])
@login_required
def create_post():
    data = request.form if request.form else (request.json or {})
    platform = data.get("platform", "")
    if platform not in ("instagram", "twitter"):
        return jsonify(error="Invalid platform"), 400
    try:
        oc_id = int(data.get("oc_id"))
    except (TypeError, ValueError):
        return jsonify(error="Choose an OC"), 400
    c = conn()
    oc = c.execute("SELECT * FROM ocs WHERE id=? AND owner_id=?", (oc_id, current_user()["id"])).fetchone()
    if not oc:
        c.close()
        return jsonify(error="You don't own that OC"), 403
    post_text = data.get("text","").strip()
    post_image = data.get("image","").strip()

    uploaded_items = request.files.getlist("media_files")
    legacy_uploaded = request.files.get("image_file")
    if legacy_uploaded and legacy_uploaded.filename and not any(x.filename for x in uploaded_items):
        uploaded_items = [legacy_uploaded]
    uploaded_items = [x for x in uploaded_items if x and x.filename]
    stored_media = []
    max_media = 4 if platform == "twitter" else 10
    if len(uploaded_items) > max_media:
        c.close()
        if platform == "twitter":
            return jsonify(error="Choose up to 4 photos/GIFs per Twitter/X post."), 400
        return jsonify(error="Choose up to 10 photos/videos per Instagram post."), 400
    allowed_images = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    allowed_videos = {".mp4", ".webm", ".mov", ".m4v"}
    for uploaded in uploaded_items:
        ext = os.path.splitext(uploaded.filename)[1].lower()
        if ext not in allowed_images | allowed_videos:
            c.close()
            return jsonify(error="Use JPG, PNG, WEBP, GIF, MP4, WEBM, MOV, or M4V files."), 400
        if platform == "twitter" and ext in allowed_videos:
            # Video support requested specifically for Instagram; avoid changing Twitter behaviour unexpectedly.
            c.close()
            return jsonify(error="Video uploads are currently supported for Instagram posts and Stories."), 400
        uploaded.stream.seek(0, 2)
        size = uploaded.stream.tell()
        uploaded.stream.seek(0)
        if size > 50 * 1024 * 1024:
            c.close()
            return jsonify(error="Each media file must be 50 MB or smaller."), 400
        filename = f"{uuid.uuid4().hex}{ext}"
        try:
            media_url = store_post_photo(uploaded, filename)
        except Exception as exc:
            c.close()
            print(f"Post media upload failed: {exc}")
            return jsonify(error="Could not store the uploaded media. Check the server's Supabase Storage configuration."), 502
        media_type = "video" if ext in allowed_videos else "image"
        stored_media.append((media_url, media_type))
    if stored_media:
        post_image = stored_media[0][0]

    c.execute("INSERT INTO posts(oc_id,platform,text,image) VALUES(?,?,?,?)",
              (oc_id, platform, post_text, post_image))
    post_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    for position, (media_url, media_type) in enumerate(stored_media):
        c.execute("INSERT INTO post_media(post_id,media_url,media_type,position) VALUES(?,?,?,?)",
                  (post_id, media_url, media_type, position))
    c.commit()

    import json

    payload = {
        "post_id": post_id,
        "oc_id": oc["id"],
        "oc_name": oc["name"],
        "username": oc["username"],
        "avatar": oc["avatar"] or "",
        "platform": platform,
        "text": post_text,
        "image": post_image
    }

    c.execute(
        "INSERT INTO discord_outbox(event_type, payload) VALUES(?, ?)",
        ("post", json.dumps(payload))
    )
    c.commit()

    c.close()
    return redirect(url_for(platform))


@app.route("/api/retweet", methods=["POST"])
@login_required
def api_retweet():
    data = request.get_json(silent=True) or request.form
    try:
        post_id = int(data.get("post_id"))
        oc_id = int(data.get("oc_id"))
    except (TypeError, ValueError):
        return jsonify(error="Choose an OC and a post"), 400

    user = current_user()
    c = conn()
    post = c.execute(
        "SELECT id FROM posts WHERE id=? AND platform='twitter'", (post_id,)
    ).fetchone()
    if not post:
        c.close()
        return jsonify(error="Twitter/X post not found"), 404
    actor = c.execute(
        "SELECT id FROM ocs WHERE id=? AND owner_id=?", (oc_id, user["id"])
    ).fetchone()
    if not actor:
        c.close()
        return jsonify(error="You can only retweet as one of your own OCs"), 403

    existing = c.execute(
        "SELECT 1 FROM oc_tweet_retweets WHERE post_id=? AND oc_id=?",
        (post_id, oc_id)
    ).fetchone()
    if existing:
        c.execute(
            "DELETE FROM oc_tweet_retweets WHERE post_id=? AND oc_id=?",
            (post_id, oc_id)
        )
        retweeted = False
    else:
        c.execute(
            "INSERT OR IGNORE INTO oc_tweet_retweets(post_id, oc_id) VALUES(?, ?)",
            (post_id, oc_id)
        )
        retweeted = True
    c.commit()
    count = (
        c.execute("SELECT COUNT(*) FROM retweets WHERE post_id=?", (post_id,)).fetchone()[0]
        + c.execute("SELECT COUNT(*) FROM oc_tweet_retweets WHERE post_id=?", (post_id,)).fetchone()[0]
    )
    c.close()
    return jsonify(ok=True, retweeted=retweeted, count=count, actor_oc_id=oc_id)

@app.route("/api/like", methods=["POST"])
@login_required
def api_like():
    data = request.get_json(silent=True) or request.form
    try:
        post_id = int(data.get("post_id"))
    except (TypeError, ValueError):
        return jsonify(error="Invalid post"), 400

    user = current_user()
    c = conn()
    post = c.execute("SELECT id, platform FROM posts WHERE id=?", (post_id,)).fetchone()
    if not post:
        c.close()
        return jsonify(error="Post not found"), 404

    # Likes are performed by an OC on both platforms; keep Twitter's existing table.
    if post["platform"] == "instagram":
        try:
            oc_id = int(data.get("oc_id"))
        except (TypeError, ValueError):
            c.close()
            return jsonify(error="Choose which OC is liking this post"), 400
        actor = c.execute(
            "SELECT id FROM ocs WHERE id=? AND owner_id=?", (oc_id, user["id"])
        ).fetchone()
        if not actor:
            c.close()
            return jsonify(error="You can only like posts as one of your own OCs"), 403
        existing = c.execute(
            "SELECT 1 FROM oc_post_likes WHERE post_id=? AND oc_id=?",
            (post_id, oc_id)
        ).fetchone()
        if existing:
            c.execute(
                "DELETE FROM oc_post_likes WHERE post_id=? AND oc_id=?",
                (post_id, oc_id)
            )
            liked = False
        else:
            c.execute(
                "INSERT OR IGNORE INTO oc_post_likes(post_id, oc_id) VALUES(?, ?)",
                (post_id, oc_id)
            )
            liked = True
        c.commit()
        count = (
            c.execute("SELECT COUNT(*) FROM post_likes WHERE post_id=?", (post_id,)).fetchone()[0]
            + c.execute("SELECT COUNT(*) FROM oc_post_likes WHERE post_id=?", (post_id,)).fetchone()[0]
        )
        c.close()
        return jsonify(ok=True, liked=liked, count=count, actor_oc_id=oc_id)

    if post["platform"] == "twitter":
        try:
            oc_id = int(data.get("oc_id"))
        except (TypeError, ValueError):
            c.close()
            return jsonify(error="Choose which OC is liking this tweet"), 400
        actor = c.execute(
            "SELECT id FROM ocs WHERE id=? AND owner_id=?", (oc_id, user["id"])
        ).fetchone()
        if not actor:
            c.close()
            return jsonify(error="You can only like tweets as one of your own OCs"), 403
        existing = c.execute(
            "SELECT 1 FROM oc_tweet_likes WHERE post_id=? AND oc_id=?",
            (post_id, oc_id)
        ).fetchone()
        if existing:
            c.execute(
                "DELETE FROM oc_tweet_likes WHERE post_id=? AND oc_id=?",
                (post_id, oc_id)
            )
            liked = False
        else:
            c.execute(
                "INSERT OR IGNORE INTO oc_tweet_likes(post_id, oc_id) VALUES(?, ?)",
                (post_id, oc_id)
            )
            liked = True
        c.commit()
        count = (
            c.execute("SELECT COUNT(*) FROM post_likes WHERE post_id=?", (post_id,)).fetchone()[0]
            + c.execute("SELECT COUNT(*) FROM oc_tweet_likes WHERE post_id=?", (post_id,)).fetchone()[0]
        )
        c.close()
        return jsonify(ok=True, liked=liked, count=count, actor_oc_id=oc_id)

    user_id = user["id"]
    existing = c.execute(
        "SELECT 1 FROM post_likes WHERE post_id=? AND user_id=?", (post_id, user_id)
    ).fetchone()
    if existing:
        c.execute("DELETE FROM post_likes WHERE post_id=? AND user_id=?", (post_id, user_id))
        liked = False
    else:
        c.execute("INSERT OR IGNORE INTO post_likes(post_id, user_id) VALUES(?, ?)", (post_id, user_id))
        liked = True
    c.commit()
    count = c.execute("SELECT COUNT(*) FROM post_likes WHERE post_id=?", (post_id,)).fetchone()[0]
    c.close()
    return jsonify(ok=True, liked=liked, count=count)

@app.route("/api/reply-like", methods=["POST"])
@login_required
def api_reply_like():
    data = request.get_json(silent=True) or request.form
    try:
        reply_id = int(data.get("reply_id"))
    except (TypeError, ValueError):
        return jsonify(error="Invalid comment ID"), 400

    user_id = current_user()["id"]
    c = conn()
    if not c.execute("SELECT id FROM replies WHERE id=?", (reply_id,)).fetchone():
        c.close()
        return jsonify(error="Comment not found"), 404

    existing = c.execute(
        "SELECT 1 FROM reply_likes WHERE reply_id=? AND user_id=?",
        (reply_id, user_id)
    ).fetchone()
    if existing:
        c.execute(
            "DELETE FROM reply_likes WHERE reply_id=? AND user_id=?",
            (reply_id, user_id)
        )
        liked = False
    else:
        c.execute(
            "INSERT OR IGNORE INTO reply_likes(reply_id, user_id) VALUES(?, ?)",
            (reply_id, user_id)
        )
        liked = True

    c.commit()
    count = c.execute(
        "SELECT COUNT(*) FROM reply_likes WHERE reply_id=?", (reply_id,)
    ).fetchone()[0]
    c.close()
    return jsonify(ok=True, liked=liked, count=count)


@app.route("/api/reply", methods=["POST"])
@login_required
def reply():
    # Accept ordinary form/JSON comments plus multipart photo or GIF attachments.
    data = request.get_json(silent=True) or request.form
    try:
        oc_id = int(data["oc_id"])
        post_id = int(data["post_id"])
    except (KeyError, TypeError, ValueError):
        return jsonify(error="Missing OC or post"), 400

    raw_parent_id = data.get("parent_id")
    try:
        parent_id = int(raw_parent_id) if raw_parent_id not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify(error="Invalid comment reply"), 400

    text = (data.get("text") or "").strip()
    uploaded = request.files.get("media_file")
    has_upload = bool(uploaded and uploaded.filename)
    raw_media_url = (data.get("media_url") or "").strip()
    if has_upload and raw_media_url:
        return jsonify(error="Choose an uploaded photo/GIF or a direct GIF URL, not both."), 400
    if not text and not has_upload and not raw_media_url:
        return jsonify(error="Write a comment or attach a photo/GIF first."), 400

    media_url = ""
    media_type = ""
    if raw_media_url:
        parsed = urllib.parse.urlparse(raw_media_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return jsonify(error="Enter a direct HTTP or HTTPS media URL."), 400
        suffix = Path(parsed.path).suffix.lower()
        if suffix not in {".gif", ".png", ".jpg", ".jpeg", ".webp"}:
            return jsonify(error="Use a direct GIF or image URL ending in .gif, .png, .jpg, .jpeg, or .webp."), 400
        media_url = raw_media_url
        media_type = "gif" if suffix == ".gif" else "image"

    if has_upload:
        extension = Path(secure_filename(uploaded.filename or "")).suffix.lower()
        allowed_extensions = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
        if extension not in allowed_extensions:
            return jsonify(error="Comments support JPG, PNG, WEBP, or GIF files."), 400
        uploaded.stream.seek(0, os.SEEK_END)
        media_size = uploaded.stream.tell()
        uploaded.stream.seek(0)
        if media_size > 10 * 1024 * 1024:
            return jsonify(error="Photo/GIF comments must be 10 MB or smaller."), 413
        stored_name = f"comment_{uuid.uuid4().hex}{extension}"
        try:
            media_url = store_post_photo(uploaded, stored_name)
        except Exception as exc:
            print(f"Comment media upload failed: {exc}")
            return jsonify(error="Couldn't upload that photo/GIF. Please try again."), 502
        media_type = "gif" if extension == ".gif" else "image"

    c = conn()
    oc = c.execute(
        "SELECT * FROM ocs WHERE id=? AND owner_id=?",
        (oc_id, current_user()["id"])
    ).fetchone()
    if not oc:
        c.close()
        return jsonify(error="You don't own that OC"), 403

    post = c.execute("SELECT id FROM posts WHERE id=? AND platform IN ('instagram','twitter')", (post_id,)).fetchone()
    if not post:
        c.close()
        return jsonify(error="Post not found"), 404

    if parent_id is not None:
        parent = c.execute(
            "SELECT id, post_id FROM replies WHERE id=?", (parent_id,)
        ).fetchone()
        if not parent or int(parent["post_id"]) != post_id:
            c.close()
            return jsonify(error="That comment does not belong to this post"), 400

    c.execute(
        "INSERT INTO replies(post_id,oc_id,text,parent_id,media_url,media_type) VALUES(?,?,?,?,?,?)",
        (post_id, oc_id, text, parent_id, media_url, media_type)
    )
    c.commit()
    c.close()
    return jsonify(ok=True)


@app.route("/api/reply/<int:reply_id>/delete", methods=["POST"])
@login_required
def delete_reply_comment(reply_id):
    """Delete a comment/reply only when it was posted as an OC owned by this account."""
    user_id = int(current_user()["id"])
    c = conn()
    row = c.execute("""
        SELECT r.id, r.post_id, r.parent_id, p.platform, o.owner_id
        FROM replies r
        JOIN posts p ON p.id=r.post_id
        JOIN ocs o ON o.id=r.oc_id
        WHERE r.id=?
    """, (reply_id,)).fetchone()
    if not row:
        c.close()
        return "Comment not found", 404
    if str(row["platform"]).lower() not in {"instagram", "twitter"}:
        c.close()
        return "This comment cannot be deleted here", 400
    if int(row["owner_id"]) != user_id:
        c.close()
        return "You can only delete comments made by your own OCs.", 403

    # Keep other people's replies visible if a parent comment is removed.
    c.execute("UPDATE replies SET parent_id=NULL WHERE parent_id=?", (reply_id,))
    c.execute("DELETE FROM reply_likes WHERE reply_id=?", (reply_id,))
    c.execute("DELETE FROM replies WHERE id=?", (reply_id,))
    c.commit()
    platform = str(row["platform"]).lower()
    c.close()

    # Return to the page the user was viewing, but never redirect to another host.
    referrer = request.referrer or ""
    parsed = urllib.parse.urlparse(referrer)
    if parsed.netloc == request.host and parsed.scheme in {"http", "https"}:
        return redirect(referrer)
    return redirect(url_for("instagram" if platform == "instagram" else "twitter"))


@app.route("/api/chat/<int:chat_id>/wallpaper", methods=["POST"])
@login_required
def save_chat_wallpaper(chat_id):
    """Save a personal wallpaper for a conversation."""
    user = current_user()
    c = conn()
    allowed = c.execute(
        "SELECT 1 FROM chat_users WHERE chat_id=? AND user_id=?",
        (chat_id, user["id"]),
    ).fetchone()
    if not allowed:
        c.close()
        return jsonify(error="You are not a member of this chat."), 403

    preset_names = {"classic", "midnight", "ocean", "rose", "lavender", "sunset", "forest", "stars"}
    uploaded = request.files.get("wallpaper_file")
    if uploaded and uploaded.filename:
        safe_name = secure_filename(uploaded.filename or "")
        extension = Path(safe_name).suffix.lower()
        if extension not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
            c.close()
            return jsonify(error="Choose a JPG, PNG, GIF, or WEBP wallpaper."), 400
        try:
            uploaded.stream.seek(0, os.SEEK_END)
            size = uploaded.stream.tell()
            uploaded.stream.seek(0)
        except (AttributeError, OSError):
            size = 0
        if size > 10 * 1024 * 1024:
            c.close()
            return jsonify(error="Wallpaper images must be 10 MB or smaller."), 413
        stored_name = f"chat_wallpaper_{chat_id}_{user['id']}_{uuid.uuid4().hex}{extension}"
        try:
            wallpaper = store_post_photo(uploaded, stored_name)
        except Exception as exc:
            c.close()
            print(f"iMessage wallpaper upload failed: {exc}")
            return jsonify(error="Could not upload that wallpaper. Please try again."), 502
    else:
        data = request.get_json(silent=True) or request.form
        wallpaper = str(data.get("wallpaper", "preset:classic") or "preset:classic").strip()
        if not wallpaper.startswith("preset:") or wallpaper.split(":", 1)[1] not in preset_names:
            c.close()
            return jsonify(error="Choose one of the available wallpaper styles."), 400

    c.execute("""INSERT INTO chat_preferences(user_id, chat_id, wallpaper) VALUES(?,?,?)
        ON CONFLICT(user_id, chat_id) DO UPDATE SET wallpaper=excluded.wallpaper""",
        (user["id"], chat_id, wallpaper))
    c.commit()
    c.close()
    return jsonify(ok=True, wallpaper=wallpaper)


@app.route("/api/message", methods=["POST"])
@login_required
def message():
    # Accept JSON (text / direct GIF URL) and multipart form data (photo/GIF uploads).
    data = (request.get_json(silent=True) or {}) if request.is_json else request.form
    try:
        chat_id = int(data["chat_id"])
        oc_id = int(data["oc_id"])
    except (KeyError, TypeError, ValueError):
        return jsonify(error="Missing chat or OC"), 400

    text = str(data.get("text", "") or "").strip()
    image = str(data.get("image", "") or "").strip()
    uploaded_media = request.files.get("media_file")
    has_upload = bool(uploaded_media and uploaded_media.filename)
    if not text and not image and not has_upload:
        return jsonify(error="Write a message or attach a photo/GIF first."), 400

    # GIF URLs can be sent directly; only web URLs are accepted.
    if image:
        parsed_image_url = urllib.parse.urlparse(image)
        if parsed_image_url.scheme.lower() not in ("http", "https") or not parsed_image_url.netloc:
            return jsonify(error="GIF links must start with http:// or https://."), 400

    c = conn()
    member = c.execute("""
      SELECT o.* FROM ocs o JOIN chat_members cm ON cm.oc_id=o.id
      WHERE cm.chat_id=? AND o.id=? AND o.owner_id=?
    """, (chat_id, oc_id, current_user()["id"])).fetchone()
    if not member:
        c.close()
        return jsonify(error="That OC is not one of your OCs in this chat"), 403

    # Store uploaded photos and GIFs in persistent storage in production. The existing
    # messages.image column stores their public URL, so no database migration is needed.
    if has_upload:
        safe_original_name = secure_filename(uploaded_media.filename or "")
        extension = Path(safe_original_name).suffix.lower()
        allowed_extensions = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}
        if extension not in allowed_extensions:
            c.close()
            return jsonify(error="Please attach a JPG, PNG, GIF, WEBP, HEIC, or HEIF image."), 400
        try:
            uploaded_media.stream.seek(0, os.SEEK_END)
            media_size = uploaded_media.stream.tell()
            uploaded_media.stream.seek(0)
        except (AttributeError, OSError):
            media_size = 0
        if media_size > 20 * 1024 * 1024:
            c.close()
            return jsonify(error="Photos and GIFs must be 20 MB or smaller."), 413
        stored_name = f"message_{chat_id}_{uuid.uuid4().hex}{extension}"
        try:
            image = store_post_photo(uploaded_media, stored_name)
        except Exception as exc:
            c.close()
            print(f"iMessage media upload failed: {exc}")
            return jsonify(error="Could not upload that photo/GIF. Please try again."), 502

    c.execute("INSERT INTO messages(chat_id,oc_id,text,image) VALUES(?,?,?,?)",
              (chat_id, oc_id, text, image))
    message_id = int(c.execute("SELECT last_insert_rowid()").fetchone()[0])

    # Notify every other Discord account in this conversation (direct and group chats).
    chat_row = c.execute("SELECT id, name FROM chats WHERE id=?", (chat_id,)).fetchone()
    recipients = c.execute("""
      SELECT DISTINCT user_id FROM chat_users
      WHERE chat_id=? AND user_id<>?
    """, (chat_id, current_user()["id"])).fetchall()
    recipient_ids = [int(row["user_id"]) for row in recipients]
    if recipient_ids:
        payload = {
            "message_id": message_id,
            "chat_id": chat_id,
            "chat_name": str(chat_row["name"] if chat_row else "iMessage"),
            "recipient_discord_ids": recipient_ids,
            "sender_oc_name": str(member["name"]),
            "sender_username": str(member["username"]),
            "sender_avatar": str(member["avatar"] or ""),
            "text": text[:1000],
            "image": image,
        }
        c.execute("INSERT INTO discord_outbox(event_type, payload) VALUES(?, ?)",
                  ("message", json.dumps(payload)))
    c.commit()
    c.close()
    return jsonify(ok=True)


@app.route("/api/message/<int:message_id>/delete", methods=["POST"])
@login_required
def delete_message(message_id):
    """Delete a message only if its sender OC belongs to the signed-in account."""
    user_id = int(current_user()["id"])
    c = conn()
    row = c.execute("""
        SELECT m.id, m.chat_id, o.owner_id
        FROM messages m JOIN ocs o ON o.id=m.oc_id
        WHERE m.id=?
    """, (message_id,)).fetchone()
    if not row:
        c.close()
        return "Message not found", 404
    allowed = c.execute(
        "SELECT 1 FROM chat_users WHERE chat_id=? AND user_id=?",
        (row["chat_id"], user_id),
    ).fetchone()
    if not allowed:
        c.close()
        return "You are not a member of this chat.", 403
    if int(row["owner_id"]) != user_id:
        c.close()
        return "You can only delete messages sent by your own OCs.", 403
    chat_id = int(row["chat_id"])
    c.execute("DELETE FROM messages WHERE id=?", (message_id,))
    c.commit()
    c.close()
    if request.is_json:
        return jsonify(ok=True)
    return redirect(url_for("chat", chat_id=chat_id))


@app.route("/api/chat/<int:chat_id>/add-oc", methods=["POST"])
@login_required
def add_oc_to_chat(chat_id):
    user = current_user()
    try:
        oc_id = int((request.json or request.form)["oc_id"])
    except (KeyError, TypeError, ValueError):
        return jsonify(error="Missing OC"), 400
    c = conn()
    chat_row = c.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
    oc = c.execute("SELECT * FROM ocs WHERE id=?", (oc_id,)).fetchone()
    if not chat_row or not oc:
        c.close()
        return jsonify(error="Chat or OC not found"), 404
    # Only the chat creator can add OCs to the chat.
    if chat_row["owner_id"] != user["id"]:
        c.close()
        return jsonify(error="Only the chat creator can add OCs"), 403
    c.execute("INSERT OR IGNORE INTO chat_members(chat_id,oc_id) VALUES(?,?)", (chat_id, oc_id))
    c.execute("INSERT OR IGNORE INTO chat_users(chat_id,user_id) VALUES(?,?)", (chat_id, oc["owner_id"]))
    c.commit()
    c.close()
    return jsonify(ok=True)


# Optional Tupperbox bridge endpoint.
# A separate Discord bot can POST the Discord user ID + Tupper name here.
# The web app then finds that user's matching OC and returns its OC id.
@app.route("/tupperbox/import", methods=["POST"])
def tupperbox_import():
    secret = os.environ.get("TUPPER_BRIDGE_SECRET", "")
    if secret and request.headers.get("X-Tupper-Secret") != secret:
        return jsonify(error="Unauthorized"), 401

    data = request.json or {}
    owner_id = data.get("discord_id")
    name = (data.get("name") or "").strip()
    avatar = (data.get("avatar") or "").strip()

    if not owner_id or not name:
        return jsonify(error="discord_id and name are required"), 400

    try:
        owner_id = int(owner_id)
    except (ValueError, TypeError):
        return jsonify(error="Invalid discord_id"), 400

    c = conn()

    existing = c.execute(
        "SELECT * FROM ocs WHERE owner_id=? AND lower(name)=lower(?)",
        (owner_id, name)
    ).fetchone()

    if existing:
        if avatar:
            c.execute(
                "UPDATE ocs SET avatar=? WHERE id=?",
                (avatar, existing["id"])
            )
            c.commit()

        c.close()
        return jsonify(ok=True, created=False, oc=dict(existing))

    username = "".join(
        ch.lower() if ch.isalnum() else "_"
        for ch in name
    ).strip("_") or "oc"

    base = username
    number = 2

    while c.execute(
        "SELECT id FROM ocs WHERE owner_id=? AND username=?",
        (owner_id, username)
    ).fetchone():
        username = f"{base}_{number}"
        number += 1

    c.execute(
        """INSERT INTO ocs
           (owner_id, name, username, avatar, bio, banner)
           VALUES (?, ?, ?, ?, '', '')""",
        (owner_id, name, username, avatar)
    )

    oc_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.commit()

    oc = c.execute(
        "SELECT * FROM ocs WHERE id=?",
        (oc_id,)
    ).fetchone()

    c.close()

    return jsonify(ok=True, created=True, oc=dict(oc))


@app.route("/tupperbox/resolve", methods=["POST"])
def tupperbox_resolve():
    secret = os.environ.get("TUPPER_BRIDGE_SECRET", "")
    if secret and request.headers.get("X-Tupper-Secret") != secret:
        return jsonify(error="Unauthorized"), 401
    data = request.json or {}
    owner_id = data.get("discord_id")
    tupper_name = (data.get("name") or data.get("username") or "").strip()
    if not owner_id or not tupper_name:
        return jsonify(error="discord_id and Tupper name are required"), 400
    try:
        owner_id = int(owner_id)
    except ValueError:
        return jsonify(error="Invalid discord_id"), 400
    c = conn()
    oc = c.execute("""
      SELECT id,name,username,avatar,bio,banner FROM ocs
      WHERE owner_id=? AND (lower(name)=lower(?) OR lower(username)=lower(?))
    """, (owner_id, tupper_name, tupper_name.lstrip("@"))).fetchone()
    c.close()
    if not oc:
        return jsonify(ok=False, error="No matching OC"), 404
    return jsonify(ok=True, oc=dict(oc))



@app.route("/api/discord/outbox", methods=["GET"])
def discord_outbox():
    secret = os.environ.get("SOCIAL_BRIDGE_SECRET", "")
    if secret and request.headers.get("X-Bridge-Secret") != secret:
        return jsonify(error="Unauthorized"), 401

    c = conn()
    rows = c.execute(
        "SELECT id, event_type, payload, created_at FROM discord_outbox WHERE sent=0 ORDER BY id LIMIT 25"
    ).fetchall()
    c.close()

    return jsonify(events=[dict(row) for row in rows])


@app.route("/api/discord/outbox/ack", methods=["POST"])
def discord_outbox_ack():
    secret = os.environ.get("SOCIAL_BRIDGE_SECRET", "")
    if secret and request.headers.get("X-Bridge-Secret") != secret:
        return jsonify(error="Unauthorized"), 401

    data = request.json or {}
    ids = data.get("ids", [])

    if not isinstance(ids, list):
        return jsonify(error="ids must be a list"), 400

    c = conn()
    for event_id in ids:
        try:
            event_id = int(event_id)
        except (TypeError, ValueError):
            continue
        c.execute("UPDATE discord_outbox SET sent=1 WHERE id=?", (event_id,))
    c.commit()
    c.close()

    return jsonify(ok=True)



@app.route("/admin/export-live-backup", methods=["GET"])
def export_live_backup():
    if USE_POSTGRES:
        return jsonify(error="SQLite export is disabled because the app now uses PostgreSQL."), 409
    token = os.environ.get("MIGRATION_EXPORT_TOKEN", "")
    if not token:
        return jsonify(error="Export is not enabled"), 404

    supplied = request.headers.get("Authorization", "")
    if not hmac.compare_digest(supplied, "Bearer " + token):
        return jsonify(error="Unauthorized"), 401

    if not os.path.isfile(DB):
        return jsonify(error="Database file not found"), 404

    archive_buffer = io.BytesIO()

    with tempfile.TemporaryDirectory() as temp_dir:
        snapshot_path = os.path.join(temp_dir, "social_apps.db")

        source = sqlite3.connect(DB)
        try:
            destination = sqlite3.connect(snapshot_path)
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()

        upload_root = os.path.join(app.root_path, "static", "uploads")
        upload_count = 0

        with zipfile.ZipFile(
            archive_buffer, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.write(snapshot_path, "social_apps.db")

            if os.path.isdir(upload_root):
                for root, _, files in os.walk(upload_root):
                    for filename in files:
                        full_path = os.path.join(root, filename)
                        relative_path = os.path.relpath(full_path, app.root_path)
                        archive.write(full_path, relative_path)
                        upload_count += 1

            archive.writestr(
                "backup_info.txt",
                "Live SQLite database snapshot\n"
                f"Created UTC: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Uploaded files included: {upload_count}\n"
            )

    archive_buffer.seek(0)
    response = send_file(
        archive_buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name="oc-socials-live-backup.zip",
        max_age=0,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5050)), debug=False)
