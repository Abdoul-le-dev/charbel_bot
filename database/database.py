import sqlite3
from datetime import datetime

DB_PATH = "preinscriptions.db"


def get_conn():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE,
            prenom      TEXT,
            level       TEXT,
            objectif    TEXT,
            whatsapp    TEXT,
            email       TEXT,
            completed   INTEGER DEFAULT 0,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS videos (
            video_name  TEXT PRIMARY KEY,
            file_id     TEXT NOT NULL,
            created_at  TEXT NOT NULL
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS members_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE,
            joined_at   TEXT NOT NULL
        )
    ''')

    conn.commit()
    conn.close()
    print("✅ Base de données initialisée")


def upsert_user(telegram_id, **kwargs):
    """
    Crée ou met à jour l'utilisateur avec les champs fournis.
    Appelle cette fonction à chaque étape du formulaire.
    Ex: upsert_user(123456, prenom="Jean")
        upsert_user(123456, level="Débutant")
    Seuls les champs passés en kwargs sont modifiés.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
    exists = cur.fetchone()

    if not exists:
        fields = ["telegram_id", "created_at", "updated_at"] + list(kwargs.keys())
        values = [telegram_id, now, now] + list(kwargs.values())
        placeholders = ", ".join(["?"] * len(values))
        columns = ", ".join(fields)
        cur.execute(
            f"INSERT INTO users ({columns}) VALUES ({placeholders})",
            values
        )
    else:
        set_clause = ", ".join([f"{k} = ?" for k in kwargs.keys()])
        values = list(kwargs.values()) + [now, telegram_id]
        cur.execute(
            f"UPDATE users SET {set_clause}, updated_at = ? WHERE telegram_id = ?",
            values
        )

    conn.commit()
    conn.close()
    print(f"✅ DB update {telegram_id} → {kwargs}")


def log_member(telegram_id):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO members_log (telegram_id, joined_at) VALUES (?, ?)",
        (telegram_id, now)
    )
    conn.commit()
    conn.close()


def get_file_id(video_name):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT file_id FROM videos WHERE video_name = ?", (video_name,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def save_file_id(video_name, file_id):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO videos (video_name, file_id, created_at) VALUES (?, ?, ?)",
        (video_name, file_id, now)
    )
    conn.commit()
    conn.close()