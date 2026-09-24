"""
Base de données SQLite — Trading Pour Tous.
Toutes les dates sont en heure du Bénin (UTC+1), format "AAAA-MM-JJ HH:MM:SS".
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

DB_PATH = "preinscriptions.db"
BENIN = timezone(timedelta(hours=1))

# Statuts d'un rappel planifié
PLANIFIE, ENVOYE, MANQUE = 0, 1, 2

# Champs autorisés dans les filtres / statistiques
CHAMPS_REPONSES = {"deja_trade", "frein", "presence"}


# ════════════════════════════════════════════════════════════════════════════
# CONNEXION
# ════════════════════════════════════════════════════════════════════════════

def now_benin() -> datetime:
    return datetime.now(BENIN).replace(tzinfo=None)


def _maintenant() -> str:
    return now_benin().strftime("%Y-%m-%d %H:%M:%S")


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def connexion():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════════════════════
# CRÉATION DES TABLES
# ════════════════════════════════════════════════════════════════════════════

SCHEMA = """
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
);
CREATE TABLE IF NOT EXISTS videos (
    video_name TEXT PRIMARY KEY,
    file_id    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS members_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER UNIQUE,
    joined_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS categories (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    nom      TEXT UNIQUE NOT NULL,
    creee_le TEXT,
    active   INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS messages_libres (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER,
    texte       TEXT,
    recu_le     TEXT
);
-- Chaque clic / code est une ligne : type = agenda_android | agenda_iphone | live | code
CREATE TABLE IF NOT EXISTS suivi (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER,
    type        TEXT,
    jour        INTEGER,
    detail      TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS codes (
    jour INTEGER,
    rang INTEGER,
    mot  TEXT NOT NULL,
    PRIMARY KEY (jour, rang)
);
CREATE TABLE IF NOT EXISTS rappels (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    nom        TEXT,
    cible      TEXT,
    date_envoi TEXT,
    texte      TEXT,
    video      TEXT,
    bouton     TEXT,
    statut     INTEGER DEFAULT 0
);
"""

# Colonnes ajoutées aux anciennes bases (ignorées si elles existent déjà)
COLONNES_USERS = {
    "categorie": "TEXT",
    "last_seen": "TEXT",
    "pays": "TEXT",
    "interet": "TEXT",
    "deja_trade": "TEXT",       # Q3
    "frein": "TEXT",            # Q4
    "presence": "TEXT",         # Q5 : "Les deux soirs" / "Un seul soir"
    "veut_j1": "INTEGER",       # présent prévu le 30 septembre (1/0)
    "veut_j2": "INTEGER",       # présent prévu le 1er octobre  (1/0)
    "webinaire": "TEXT",        # webinaire pour lequel la personne est inscrite
    "en_cours": "INTEGER DEFAULT 0",   # 1 = est en train de remplir le questionnaire
    "relance5": "INTEGER DEFAULT 0",
    "relance15": "INTEGER DEFAULT 0",
    "relance30": "INTEGER DEFAULT 0",
}


def _ajouter_colonnes(conn, table, colonnes):
    existantes = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    for nom, definition in colonnes.items():
        if nom not in existantes:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {nom} {definition}")


def init_db(categorie_defaut="Webinaire 30 septembre - 1er octobre 2026"):
    with connexion() as conn:
        conn.executescript(SCHEMA)
        _ajouter_colonnes(conn, "users", COLONNES_USERS)
        _ajouter_colonnes(conn, "members_log", {"lien_entree": "TEXT"})
        if not conn.execute("SELECT 1 FROM categories").fetchone():
            conn.execute("INSERT INTO categories (nom, creee_le) VALUES (?, ?)",
                         (categorie_defaut, _maintenant()))
    print("✅ Base de données initialisée")


# ════════════════════════════════════════════════════════════════════════════
# UTILISATEURS
# ════════════════════════════════════════════════════════════════════════════

def upsert_user(telegram_id, **champs):
    """Crée ou met à jour l'utilisateur. Seuls les champs passés sont modifiés.
    Ex : upsert_user(123, prenom="Awa") ; upsert_user(123, presence=None)"""
    champs["updated_at"] = _maintenant()
    with connexion() as conn:
        existe = conn.execute("SELECT 1 FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        if existe:
            sets = ", ".join(f"{k} = ?" for k in champs)
            conn.execute(f"UPDATE users SET {sets} WHERE telegram_id = ?",
                         (*champs.values(), telegram_id))
        else:
            champs = {"telegram_id": telegram_id, "created_at": champs["updated_at"], **champs}
            colonnes = ", ".join(champs)
            marques = ", ".join("?" * len(champs))
            conn.execute(f"INSERT INTO users ({colonnes}) VALUES ({marques})", tuple(champs.values()))


def get_user(telegram_id) -> dict | None:
    with connexion() as conn:
        row = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
    return dict(row) if row else None


def touch_last_seen(telegram_id):
    """Note l'activité SANS toucher updated_at (qui sert au calcul des relances)."""
    with connexion() as conn:
        conn.execute("UPDATE users SET last_seen = ? WHERE telegram_id = ?",
                     (_maintenant(), telegram_id))


def log_member(telegram_id, lien_entree=None):
    """Enregistre l'arrivée dans le canal + le lien d'invitation utilisé (A, B, C...)."""
    with connexion() as conn:
        conn.execute("INSERT OR IGNORE INTO members_log (telegram_id, joined_at) VALUES (?, ?)",
                     (telegram_id, _maintenant()))
        if lien_entree:
            conn.execute("UPDATE members_log SET lien_entree = ? "
                         "WHERE telegram_id = ? AND lien_entree IS NULL", (lien_entree, telegram_id))


def log_message(telegram_id, texte):
    with connexion() as conn:
        conn.execute("INSERT INTO messages_libres (telegram_id, texte, recu_le) VALUES (?, ?, ?)",
                     (telegram_id, texte, _maintenant()))


# Ordre des 3 relances automatiques : 5 min, 15 min, 30 min après la dernière activité.
# Après la 3e (relance30) sans réponse, on arrête définitivement (pas de 4e colonne).
SEQUENCE_RELANCES = ("relance5", "relance15", "relance30")


def marquer_relance(telegram_id, colonne):
    assert colonne in SEQUENCE_RELANCES
    with connexion() as conn:
        conn.execute(f"UPDATE users SET {colonne} = 1 WHERE telegram_id = ?", (telegram_id,))


def users_a_relancer(webinaire, colonne, avant: datetime) -> list[dict]:
    """Questionnaire commencé, pas terminé, sans activité depuis `avant`, relance pas encore envoyée
    (et, si ce n'est pas la 1re relance, la précédente doit déjà avoir été envoyée)."""
    assert colonne in SEQUENCE_RELANCES
    index = SEQUENCE_RELANCES.index(colonne)
    precedente = SEQUENCE_RELANCES[index - 1] if index > 0 else None
    condition_precedente = f"AND {precedente} = 1" if precedente else ""
    with connexion() as conn:
        rows = conn.execute(
            f"""SELECT * FROM users
                WHERE completed = 0 AND en_cours = 1 AND webinaire = ?
                  AND {colonne} = 0 {condition_precedente} AND updated_at <= ?""",
            (webinaire, avant.strftime("%Y-%m-%d %H:%M:%S"))).fetchall()
    return [dict(r) for r in rows]


# ════════════════════════════════════════════════════════════════════════════
# VIDÉOS (cache des file_id Telegram)
# ════════════════════════════════════════════════════════════════════════════

def get_file_id(video_name):
    with connexion() as conn:
        row = conn.execute("SELECT file_id FROM videos WHERE video_name = ?", (video_name,)).fetchone()
    return row[0] if row else None


def save_file_id(video_name, file_id):
    with connexion() as conn:
        conn.execute("INSERT OR REPLACE INTO videos (video_name, file_id, created_at) VALUES (?, ?, ?)",
                     (video_name, file_id, _maintenant()))


# ════════════════════════════════════════════════════════════════════════════
# CATÉGORIES
# ════════════════════════════════════════════════════════════════════════════

def get_categories() -> list[str]:
    with connexion() as conn:
        return [r[0] for r in conn.execute("SELECT nom FROM categories WHERE active = 1 ORDER BY id")]


def derniere_categorie() -> str | None:
    cats = get_categories()
    return cats[-1] if cats else None


def ajouter_categorie(nom):
    with connexion() as conn:
        conn.execute("INSERT OR IGNORE INTO categories (nom, creee_le) VALUES (?, ?)", (nom, _maintenant()))


# ════════════════════════════════════════════════════════════════════════════
# SUIVI : clics et codes du soir
# ════════════════════════════════════════════════════════════════════════════

def enregistrer_suivi(telegram_id, type_, jour=None, detail=None, quand=None):
    with connexion() as conn:
        conn.execute(
            "INSERT INTO suivi (telegram_id, type, jour, detail, created_at) VALUES (?, ?, ?, ?, ?)",
            (telegram_id, type_, jour, detail, quand or _maintenant()))


def nb_clics(type_, jour=None) -> int:
    sql, params = "SELECT COUNT(DISTINCT telegram_id) FROM suivi WHERE type = ?", [type_]
    if jour:
        sql += " AND jour = ?"
        params.append(jour)
    with connexion() as conn:
        return conn.execute(sql, params).fetchone()[0]


def ids_presents(jour, depuis: str) -> list[int]:
    """Présent = a envoyé un code ce soir-là OU a cliqué sur le live à partir de `depuis`."""
    with connexion() as conn:
        rows = conn.execute(
            """SELECT DISTINCT telegram_id FROM suivi
               WHERE jour = ? AND (type = 'code' OR (type = 'live' AND created_at >= ?))""",
            (jour, depuis)).fetchall()
    return [r[0] for r in rows]


def definir_codes(jour, mots: list[str]):
    with connexion() as conn:
        conn.execute("DELETE FROM codes WHERE jour = ?", (jour,))
        conn.executemany("INSERT INTO codes (jour, rang, mot) VALUES (?, ?, ?)",
                         [(jour, i, m) for i, m in enumerate(mots, start=1)])


def get_codes() -> list[dict]:
    with connexion() as conn:
        return [dict(r) for r in conn.execute("SELECT jour, rang, mot FROM codes ORDER BY jour, rang")]


def trouver_code(mot) -> list[tuple[int, int]]:
    with connexion() as conn:
        return [(r["jour"], r["rang"]) for r in
                conn.execute("SELECT jour, rang FROM codes WHERE mot = ?", (mot,))]


# ════════════════════════════════════════════════════════════════════════════
# CIBLES (listes d'identifiants)
# ════════════════════════════════════════════════════════════════════════════

def ids_tous() -> list[int]:
    with connexion() as conn:
        rows = conn.execute("""SELECT telegram_id FROM users WHERE telegram_id IS NOT NULL
                               UNION SELECT telegram_id FROM members_log""").fetchall()
    return [r[0] for r in rows]


def ids_confirmes(webinaire, jour=None, champ=None, valeur=None) -> list[int]:
    """Personnes confirmées pour ce webinaire, avec filtres facultatifs (soir, réponse)."""
    sql, params = "SELECT telegram_id FROM users WHERE completed = 1 AND webinaire = ?", [webinaire]
    if jour:
        sql += f" AND veut_j{int(jour)} = 1"
    if champ:
        assert champ in CHAMPS_REPONSES
        sql += f" AND {champ} = ?"
        params.append(valeur)
    with connexion() as conn:
        return [r[0] for r in conn.execute(sql, params)]


def ids_septembre() -> list[int]:
    """Anciens confirmés (webinaire de septembre : colonne webinaire vide)."""
    with connexion() as conn:
        return [r[0] for r in conn.execute(
            "SELECT telegram_id FROM users WHERE completed = 1 AND webinaire IS NULL")]


def compter_reponses(webinaire, champ) -> dict:
    assert champ in CHAMPS_REPONSES
    with connexion() as conn:
        rows = conn.execute(
            f"""SELECT {champ} AS valeur, COUNT(*) AS n FROM users
                WHERE completed = 1 AND webinaire = ? AND {champ} IS NOT NULL
                GROUP BY {champ} ORDER BY n DESC""", (webinaire,)).fetchall()
    return {r["valeur"]: r["n"] for r in rows}


def compter_liens() -> dict:
    with connexion() as conn:
        rows = conn.execute("""SELECT COALESCE(lien_entree, '(sans lien)') AS lien, COUNT(*) AS n
                               FROM members_log GROUP BY lien ORDER BY lien""").fetchall()
    return {r["lien"]: r["n"] for r in rows}


# ════════════════════════════════════════════════════════════════════════════
# RAPPELS PLANIFIÉS
# ════════════════════════════════════════════════════════════════════════════

def seed_rappels(planning):
    """Remplit la table au premier lancement seulement. planning = liste de
    (nom, cible, date_envoi, texte, video, bouton)."""
    with connexion() as conn:
        if conn.execute("SELECT 1 FROM rappels").fetchone():
            return
        conn.executemany(
            "INSERT INTO rappels (nom, cible, date_envoi, texte, video, bouton) VALUES (?, ?, ?, ?, ?, ?)",
            planning)


def get_rappels() -> list[dict]:
    with connexion() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM rappels ORDER BY id")]


def get_rappel(rappel_id) -> dict | None:
    with connexion() as conn:
        row = conn.execute("SELECT * FROM rappels WHERE id = ?", (rappel_id,)).fetchone()
    return dict(row) if row else None


def modifier_texte_rappel(rappel_id, texte):
    with connexion() as conn:
        conn.execute("UPDATE rappels SET texte = ? WHERE id = ?", (texte, rappel_id))


def set_statut_rappel(rappel_id, statut):
    with connexion() as conn:
        conn.execute("UPDATE rappels SET statut = ? WHERE id = ?", (statut, rappel_id))


def rappels_a_traiter(maintenant: str) -> list[dict]:
    with connexion() as conn:
        rows = conn.execute("SELECT * FROM rappels WHERE statut = ? AND date_envoi <= ? "
                            "ORDER BY date_envoi, id", (PLANIFIE, maintenant)).fetchall()
    return [dict(r) for r in rows]


# ════════════════════════════════════════════════════════════════════════════
# EXPORT : une ligne par personne
# ════════════════════════════════════════════════════════════════════════════

def lignes_export(ids, seuils: dict) -> list[dict]:
    """seuils = {jour: "AAAA-MM-JJ HH:MM:SS"} : un clic sur le live à partir de cette heure = présent."""
    with connexion() as conn:
        users = {r["telegram_id"]: dict(r) for r in conn.execute("SELECT * FROM users")}
        membres = {r["telegram_id"]: dict(r) for r in conn.execute("SELECT * FROM members_log")}
        suivi = [dict(r) for r in conn.execute("SELECT * FROM suivi ORDER BY created_at, id")]

    evenements = {}
    for s in suivi:
        evenements.setdefault(s["telegram_id"], []).append(s)

    def oui_non(condition):
        return "Oui" if condition else "Non"

    lignes = []
    for uid in ids:
        u, m, evts = users.get(uid, {}), membres.get(uid, {}), evenements.get(uid, [])

        def premier(type_, jour=None):
            return next((e for e in evts if e["type"] == type_ and (jour is None or e["jour"] == jour)), None)

        ligne = {
            "ID Telegram": uid,
            "Prénom": u.get("prenom") or "",
            "WhatsApp": u.get("whatsapp") or "",
            "Lien d'entrée": m.get("lien_entree") or "",
            "Date d'arrivée": m.get("joined_at") or "",
            "Questionnaire terminé": oui_non(u.get("completed") and u.get("webinaire")),
            "Q3 Déjà tradé": u.get("deja_trade") or "",
            "Q4 Frein": u.get("frein") or "",
            "Q5 Présence": u.get("presence") or "",
            "Prévu 30 sept": "" if u.get("veut_j1") is None else oui_non(u["veut_j1"]),
            "Prévu 1er oct": "" if u.get("veut_j2") is None else oui_non(u["veut_j2"]),
            "Clic calendrier": oui_non(any(e["type"].startswith("agenda") for e in evts)),
        }
        for jour, seuil in seuils.items():
            clic = premier("live", jour)
            ligne[f"Clic live J{jour}"] = oui_non(clic)
            ligne[f"Heure 1er clic J{jour}"] = clic["created_at"][11:] if clic else ""
            for rang in (1, 2, 3):
                code = next((e for e in evts if e["type"] == "code" and e["jour"] == jour
                             and e["detail"] == str(rang)), None)
                ligne[f"J{jour} code {rang}"] = code["created_at"][11:] if code else ""
            a_code = any(e["type"] == "code" and e["jour"] == jour for e in evts)
            clic_tard = any(e["type"] == "live" and e["jour"] == jour and e["created_at"] >= seuil for e in evts)
            ligne[f"Présent J{jour}"] = oui_non(a_code or clic_tard)
        lignes.append(ligne)
    return lignes