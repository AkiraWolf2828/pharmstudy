"""Private PharmStudy pilot. Put this server behind an HTTPS reverse proxy."""
import argparse
import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
STATIC = {"index.html": "text/html", "adrenergic-synapse.html": "text/html", "sw.js": "text/javascript"}
DB = Path(os.environ.get("PHARMSTUDY_DB", Path.home() / ".pharmstudy" / "accounts.sqlite3"))
ORIGIN = os.environ.get("PHARMSTUDY_ORIGIN", "http://127.0.0.1:8080")
COOKIE = "pharmstudy_session"
TTL = 8 * 60 * 60


def connect():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    return db


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=salt, n=2**17, r=8, p=1, maxmem=256*1024**2)


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, payload, cookie=None):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def cookie(self, token, age):
        secure = "; Secure" if ORIGIN.startswith("https://") else ""
        return f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={age}{secure}"

    def session_key(self):
        cookies = SimpleCookie(self.headers.get("Cookie", ""))
        token = cookies.get(COOKIE)
        return hashlib.sha256(token.value.encode()).hexdigest() if token else ""

    def account(self, db):
        return db.execute("""SELECT u.id,u.email,u.role FROM users u JOIN sessions s
            ON s.user_id=u.id WHERE s.token=? AND s.expires>?""",
            (self.session_key(), int(time.time()))).fetchone()

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ("/api/account", "/api/groups"):
            with connect() as db:
                user = self.account(db)
                if not user:
                    return self.reply(401, {"error": "Войди в аккаунт."})
                if path == "/api/account":
                    return self.reply(200, dict(user))
                if user["role"] not in ("admin", "teacher"):
                    return self.reply(403, {"error": "Раздел доступен преподавателям."})
                rows = db.execute("""SELECT u.email,u.group_no,u.course,u.academic_year,u.progress
                    FROM users u WHERE u.role='student' AND (?='admin' OR EXISTS
                    (SELECT 1 FROM teaching t WHERE t.user_id=? AND t.group_no=u.group_no
                     AND t.course=u.course AND t.academic_year=u.academic_year))
                    ORDER BY u.academic_year,u.course,u.group_no,u.email""",
                    (user["role"], user["id"])).fetchall()
                return self.reply(200, [{**dict(row), "progress": json.loads(row["progress"])} for row in rows])
        name = path.removeprefix("/") or "index.html"
        if name not in STATIC:
            return self.send_error(404)
        body = (ROOT / name).read_bytes()
        if name == "index.html":
            body = body.replace(b'<html lang="ru"', b'<html data-accounts="enabled" lang="ru"', 1)
        self.send_response(200)
        self.send_header("Content-Type", STATIC[name] + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = urlsplit(self.path).path
        if self.headers.get("Origin") != ORIGIN:
            return self.reply(403, {"error": "Недопустимый источник запроса."})
        if path == "/api/logout":
            with connect() as db:
                db.execute("DELETE FROM sessions WHERE token=?", (self.session_key(),))
            return self.reply(200, {}, self.cookie("", 0))
        if path != "/api/login":
            return self.reply(404, {})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 4096:
                raise ValueError()
            body = json.loads(self.rfile.read(size))
            email = body["email"].strip().lower()
            password = body["password"]
            if not 0 < len(email) <= 254 or not isinstance(password, str) or len(password) > 256:
                raise ValueError()
        except (ValueError, TypeError, KeyError, AttributeError):
            return self.reply(400, {"error": "Проверь логин и пароль."})
        now = int(time.time())
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM attempts WHERE at<?", (now-300,))
            ip = self.client_address[0]
            if db.execute("SELECT COUNT(*) FROM attempts WHERE email=? OR ip=?", (email, ip)).fetchone()[0] >= 10:
                return self.reply(429, {"error": "Слишком много попыток. Повтори через несколько минут."})
            db.execute("INSERT INTO attempts VALUES (?,?,?)", (email, ip, now))
            db.commit()
            user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            digest = password_hash(password, user["salt"] if user else bytes(16))
            if not user or not hmac.compare_digest(digest, user["digest"]):
                return self.reply(401, {"error": "Неверный логин или пароль."})
            token = secrets.token_urlsafe(32)
            db.execute("DELETE FROM sessions WHERE expires<=?", (now,))
            db.execute("INSERT INTO sessions VALUES (?,?,?)",
                (hashlib.sha256(token.encode()).hexdigest(), user["id"], now+TTL))
            db.commit()
        return self.reply(200, {"email": email, "role": user["role"]}, self.cookie(token, TTL))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["serve", "add-user", "assign-group"])
    parser.add_argument("--email")
    parser.add_argument("--role", choices=["admin", "teacher", "student"], default="student")
    parser.add_argument("--group", default="")
    parser.add_argument("--course", type=int, default=0)
    parser.add_argument("--year", default="")
    args = parser.parse_args()
    os.umask(0o077)
    DB.parent.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL, salt BLOB NOT NULL, digest BLOB NOT NULL, group_no TEXT NOT NULL,
                course INTEGER NOT NULL, academic_year TEXT NOT NULL, progress TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,user_id INTEGER NOT NULL,expires INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts(email TEXT NOT NULL,ip TEXT NOT NULL,at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS teaching(user_id INTEGER NOT NULL,group_no TEXT NOT NULL,
                course INTEGER NOT NULL,academic_year TEXT NOT NULL,
                PRIMARY KEY(user_id,group_no,course,academic_year));
        """)
        if args.action == "assign-group":
            if not (args.email and args.group and args.course > 0 and args.year):
                parser.error("Укажи --email, --group, --course и --year")
            teacher = db.execute("SELECT id FROM users WHERE email=? AND role='teacher'",
                                 (args.email.strip().lower(),)).fetchone()
            if not teacher:
                parser.error("Аккаунт преподавателя не найден")
            db.execute("INSERT OR IGNORE INTO teaching VALUES (?,?,?,?)",
                       (teacher["id"], args.group, args.course, args.year))
            return
        if args.action == "add-user":
            email = (args.email or "").strip().lower()
            if "@" not in email or len(email) > 254:
                parser.error("Укажи корректный --email")
            if args.role == "student" and not (args.group and args.course > 0 and args.year):
                parser.error("Для студента нужны --group, --course и --year")
            password = getpass.getpass("Пароль: ")
            if len(password) < 12 or len(password) > 256 or password != getpass.getpass("Повтори пароль: "):
                parser.error("Пароли должны совпасть и содержать от 12 до 256 символов")
            salt = secrets.token_bytes(16)
            db.execute("""INSERT INTO users(email,role,salt,digest,group_no,course,academic_year)
                VALUES (?,?,?,?,?,?,?)""",
                (email,args.role,salt,password_hash(password,salt),args.group,args.course,args.year))
            return
    ThreadingHTTPServer(("127.0.0.1", 8080), Handler).serve_forever()


if __name__ == "__main__":
    main()
