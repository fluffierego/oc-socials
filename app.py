import os, sqlite3, secrets, urllib.parse
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
from flask import Flask, render_template, request, redirect, url_for, session, jsonify

try:
    import requests
except ImportError:
    requests = None

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "change-this-in-production")
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "social_apps.db")

DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "")
DISCORD_REDIRECT_URI = os.environ.get("DISCORD_REDIRECT_URI", "http://127.0.0.1:5050/oauth/callback")
DISCORD_API = "https://discord.com/api/v10"


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init():
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
    CREATE TABLE IF NOT EXISTS replies(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      post_id INTEGER NOT NULL,
      oc_id INTEGER NOT NULL,
      text TEXT NOT NULL,
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP
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
    """)
    # Small migrations so an older copy of this project keeps working.
    columns = {row["name"] for row in c.execute("PRAGMA table_info(users)").fetchall()}
    if "discord_avatar" not in columns:
        c.execute("ALTER TABLE users ADD COLUMN discord_avatar TEXT DEFAULT ''")
    chat_columns = {row["name"] for row in c.execute("PRAGMA table_info(chats)").fetchall()}
    if "owner_id" not in chat_columns:
        c.execute("ALTER TABLE chats ADD COLUMN owner_id INTEGER DEFAULT 0")
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
    return {"user_ocs": user_ocs}


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
    c = conn()
    posts = c.execute("""
      SELECT p.*, o.name, o.username, o.avatar, o.id AS author_oc_id
      FROM posts p JOIN ocs o ON o.id=p.oc_id
      WHERE p.platform='instagram' ORDER BY p.id DESC
    """).fetchall()
    replies = c.execute("""
      SELECT r.*, o.name, o.username, o.avatar
      FROM replies r JOIN ocs o ON o.id=r.oc_id
      ORDER BY r.id
    """).fetchall()
    c.close()
    return render_template("instagram.html", posts=posts, replies=replies, user=current_user())


@app.route("/twitter")
@login_required
def twitter():
    c = conn()
    posts = c.execute("""
      SELECT p.*, o.name, o.username, o.avatar, o.id AS author_oc_id
      FROM posts p JOIN ocs o ON o.id=p.oc_id
      WHERE p.platform='twitter' ORDER BY p.id DESC
    """).fetchall()
    replies = c.execute("""
      SELECT r.*, o.name, o.username, o.avatar
      FROM replies r JOIN ocs o ON o.id=r.oc_id
      ORDER BY r.id
    """).fetchall()
    c.close()
    return render_template("twitter.html", posts=posts, replies=replies, user=current_user())


@app.route("/messages")
@login_required
def messages():
    c = conn()
    user_id = current_user()["id"]
    chats = c.execute("""
      SELECT DISTINCT c.*
      FROM chats c
      JOIN chat_users cu ON cu.chat_id=c.id
      WHERE cu.user_id=?
      ORDER BY c.id DESC
    """, (user_id,)).fetchall()
    c.close()
    return render_template("messages.html", chats=chats, user=current_user())


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
    msgs = c.execute("""
      SELECT m.*, o.name, o.username, o.avatar
      FROM messages m JOIN ocs o ON o.id=m.oc_id
      WHERE m.chat_id=? ORDER BY m.id
    """, (chat_id,)).fetchall()
    mine = [m for m in members if m["owner_id"] == current_user()["id"]]
    c.close()
    return render_template("chat.html", chat=chat_row, members=members, mine=mine, messages=msgs, user=current_user())


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
        except sqlite3.IntegrityError:
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
        try:
            c.execute("""
              UPDATE ocs SET name=?,username=?,bio=?,avatar=?,banner=? WHERE id=? AND owner_id=?
            """, (name, username, request.form.get("bio","").strip(),
                  request.form.get("avatar","").strip(), request.form.get("banner","").strip(),
                  oc_id, user["id"]))
            c.commit()
        except sqlite3.IntegrityError:
            c.close()
            return render_template("oc_edit.html", oc=oc, user=user, error="That username is already used by another one of your OCs.")
        c.close()
        return redirect(url_for("profile", oc_id=oc_id))
    c.close()
    return render_template("oc_edit.html", oc=oc, user=user)


@app.route("/oc/<int:oc_id>")
@login_required
def profile(oc_id):
    c = conn()
    oc = c.execute("SELECT * FROM ocs WHERE id=?", (oc_id,)).fetchone()
    posts = c.execute("SELECT * FROM posts WHERE oc_id=? ORDER BY id DESC", (oc_id,)).fetchall()
    c.close()
    if not oc:
        return "Not found", 404
    return render_template("profile.html", oc=oc, posts=posts, user=current_user())


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
    oc = c.execute("SELECT id FROM ocs WHERE id=? AND owner_id=?", (oc_id, current_user()["id"])).fetchone()
    if not oc:
        c.close()
        return jsonify(error="You don't own that OC"), 403
    c.execute("INSERT INTO posts(oc_id,platform,text,image) VALUES(?,?,?,?)",
              (oc_id, platform, data.get("text","").strip(), data.get("image","").strip()))
    c.commit()
    c.close()
    return redirect(url_for(platform))


@app.route("/api/reply", methods=["POST"])
@login_required
def reply():
    data = request.json or request.form
    try:
        oc_id = int(data["oc_id"])
        post_id = int(data["post_id"])
    except (KeyError, TypeError, ValueError):
        return jsonify(error="Missing OC or post"), 400
    c = conn()
    oc = c.execute("SELECT * FROM ocs WHERE id=? AND owner_id=?", (oc_id, current_user()["id"])).fetchone()
    if not oc:
        c.close()
        return jsonify(error="You don't own that OC"), 403
    post = c.execute("SELECT id FROM posts WHERE id=?", (post_id,)).fetchone()
    if not post:
        c.close()
        return jsonify(error="Post not found"), 404
    c.execute("INSERT INTO replies(post_id,oc_id,text) VALUES(?,?,?)",
              (post_id, oc_id, data.get("text","").strip()))
    c.commit()
    c.close()
    return jsonify(ok=True)


@app.route("/api/message", methods=["POST"])
@login_required
def message():
    data = request.json or request.form
    try:
        chat_id = int(data["chat_id"])
        oc_id = int(data["oc_id"])
    except (KeyError, TypeError, ValueError):
        return jsonify(error="Missing chat or OC"), 400
    text = data.get("text", "").strip()
    if not text:
        return jsonify(error="Message is empty"), 400
    c = conn()
    member = c.execute("""
      SELECT o.* FROM ocs o JOIN chat_members cm ON cm.oc_id=o.id
      WHERE cm.chat_id=? AND o.id=? AND o.owner_id=?
    """, (chat_id, oc_id, current_user()["id"])).fetchone()
    if not member:
        c.close()
        return jsonify(error="That OC is not one of your OCs in this chat"), 403
    c.execute("INSERT INTO messages(chat_id,oc_id,text,image) VALUES(?,?,?,?)",
              (chat_id, oc_id, text, data.get("image","").strip()))
    c.commit()
    c.close()
    return jsonify(ok=True)


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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5050)), debug=False)
