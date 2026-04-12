import re
import asyncio
import sqlite3
import traceback
import unicodedata
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
from telegram.ext import (
    ChatJoinRequestHandler, CallbackQueryHandler, Application,
    CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
)
from database.database import init_db, upsert_user, log_member, get_file_id, save_file_id

TOKEN = "8609131464:AAGK5k1jkLJvY1OSvHcR3YPnwqEqOFeWuAs"

ADMIN_IDS      = {6992809421, 6799962131}
ADMIN_USERNAME = "@Faiseur2Rois"

EVENEMENT_ACTUEL = "Masterclass Gratuite – Trading Tendance"

PLACES_RESTANTES = 47
PLACES_TOTALES   = 150

# ── États inscription ─────────────────────────────────────────────────────────
PRENOM, PRENOM_CONFIRM, LEVEL, OBJECTIF, WHATSAPP, EMAIL, CONFIRMATION = range(7)

# ── États broadcast ───────────────────────────────────────────────────────────
BC_FORMAT, BC_MEDIA, BC_TEXT = range(7, 10)

# ── États création de catégorie ───────────────────────────────────────────────
CAT_NOM = 10


# ════════════════════════════════════════════════════════════════════════════
# ── BASE DE DONNÉES ───────────────────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════

def db():
    conn = sqlite3.connect("preinscriptions.db")
    conn.row_factory = sqlite3.Row
    return conn


def _migrate_db():
    with db() as conn:
        cur = conn.cursor()
        colonnes_a_ajouter = {
            "categorie": f"TEXT DEFAULT '{EVENEMENT_ACTUEL}'",
            "last_seen":  "DATETIME",
        }
        cur.execute("PRAGMA table_info(users)")
        existantes = {row["name"] for row in cur.fetchall()}
        for col, definition in colonnes_a_ajouter.items():
            if col not in existantes:
                cur.execute(f"ALTER TABLE users ADD COLUMN {col} {definition}")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                nom       TEXT UNIQUE NOT NULL,
                creee_le  DATETIME DEFAULT (datetime('now')),
                active    INTEGER DEFAULT 1
            )
        """)
        cur.execute(
            "INSERT OR IGNORE INTO categories (nom) VALUES (?)",
            (EVENEMENT_ACTUEL,)
        )
        cur.execute("""
            CREATE TABLE IF NOT EXISTS messages_libres (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER,
                texte       TEXT,
                recu_le     DATETIME DEFAULT (datetime('now'))
            )
        """)
        conn.commit()


def _is_already_registered(user_id: int) -> bool:
    with db() as conn:
        row = conn.execute(
            """SELECT 1 FROM users
               WHERE telegram_id = ? AND completed = 1
                 AND prenom IS NOT NULL AND level IS NOT NULL
                 AND objectif IS NOT NULL AND whatsapp IS NOT NULL
                 AND email IS NOT NULL""",
            (user_id,)
        ).fetchone()
    return row is not None


def _get_incomplete_users() -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT telegram_id, prenom FROM users WHERE completed = 0 AND telegram_id IS NOT NULL"
        ).fetchall()
    return [dict(r) for r in rows]


def _get_all_user_ids() -> list[int]:
    """
    Retourne TOUS les telegram_id depuis members_log (source principale).
    Pour chaque id, on recupere le prenom dans users si disponible.
    C'est la table qui contient vraiment tout le monde.
    """
    with db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT telegram_id FROM members_log WHERE telegram_id IS NOT NULL"
        ).fetchall()
    return [r["telegram_id"] for r in rows]


def _get_prenom_for_broadcast(user_id: int) -> str:
    """Retourne le prenom de l'utilisateur ou 'Hello l ami' si inconnu."""
    with db() as conn:
        row = conn.execute(
            "SELECT prenom FROM users WHERE telegram_id = ? AND prenom IS NOT NULL",
            (user_id,)
        ).fetchone()
    return row["prenom"] if row else "Hello l ami"


def _get_categories() -> list[str]:
    with db() as conn:
        rows = conn.execute(
            "SELECT nom FROM categories WHERE active = 1 ORDER BY id"
        ).fetchall()
    return [r["nom"] for r in rows]


def _ajouter_categorie(nom: str):
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO categories (nom) VALUES (?)", (nom,))
        conn.commit()


def _touch_last_seen(user_id: int):
    with db() as conn:
        conn.execute(
            "UPDATE users SET last_seen = datetime('now') WHERE telegram_id = ?",
            (user_id,)
        )
        conn.commit()


def _log_message(user_id: int, texte: str):
    with db() as conn:
        conn.execute(
            "INSERT INTO messages_libres (telegram_id, texte) VALUES (?, ?)",
            (user_id, texte)
        )
        conn.commit()


# ════════════════════════════════════════════════════════════════════════════
# ── NETTOYAGE DU PRÉNOM ───────────────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════

def _nettoyer_prenom(texte: str) -> str:
    t = texte.strip()
    t = t.replace("\u2019", "'").replace("\u2018", "'")
    t = t.lower()
    t = "".join(c for c in t if unicodedata.category(c) not in ("So", "Sm", "Sk", "Cn"))

    for ancien, nouveau in [
        ("mappelle", " "), ("cest", " "), ("mest", " "),
        ("jsuis", " "),    ("chuis", " "),
    ]:
        t = t.replace(ancien, nouveau)

    parasites = [
        "tout le monde m'appelle", "vous pouvez m'appeler", "tu peux m'appeler",
        "ils m'appellent", "on m'appelle",
        "je me présente", "je me presente",
        "ravi de te rencontrer", "ravi de vous rencontrer",
        "comment ça va", "comment ca va",
        "bonne journée", "bonne soirée", "bonne nuit",
        "bien sûr", "bien sur",
        "je m'appelle", "j'me appelle",
        "mon prénom c'est", "mon prenom c'est",
        "mon prénom est", "mon prenom est",
        "mon nom c'est", "mon nom est",
        "je me nomme", "je me nome",
        "moi c'est", "c'est moi",
        "my name is", "my name's",
        "they call me", "call me",
        "people call me", "everyone calls me", "you can call me",
        "je suis", "je sui",
        "i am", "i'm",
        "avec plaisir",
        "bonjour", "bonsoir", "salut", "coucou", "hello",
        "hey", "wesh", "yo", "slt", "salu", "hi",
        "enchanté", "enchante", "enchantée",
        "voilà", "voila", "voici",
        "exactement", "exact",
        "ouais", "ouas", "oui",
        "alors", "donc", "bien",
        "prénom", "prenom", "appelle", "appeler", "nomme",
        "ça va", "ca va",
        "ok",
        "mon", "ma", "mes", "nom",
        "moi", "moa", "mwa",
        "suis", "est", "dj", "je", "et",
    ]

    for p in parasites:
        pattern = re.escape(p)
        t = re.sub(r"(?<![a-zà-ÿ])" + pattern + r"(?![a-zà-ÿ])", " ", t)

    t = re.sub(r"[^\w\s\u00C0-\u00FF\-']", " ", t)
    t = re.sub(r"(?<![a-zà-ÿ])'|'(?![a-zà-ÿ])", " ", t)
    t = re.sub(r"\s+", " ", t).strip()

    def cap_mot(m: str) -> str:
        return "-".join(p.capitalize() for p in m.split("-"))

    mots = [cap_mot(m) for m in t.split() if len(m) > 1]
    return " ".join(mots) if mots else texte.strip().split()[-1].capitalize()


def _extraire_prenom(texte: str) -> tuple[str, bool]:
    brut      = texte.strip()
    mots_brut = brut.split()

    if len(mots_brut) <= 2 and len(brut) <= 20:
        return brut.title(), False

    nettoye = _nettoyer_prenom(brut)
    return nettoye, True


# ════════════════════════════════════════════════════════════════════════════
# ── KEYBOARDS ─────────────────────────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════

def kb_prenom_confirm(prenom: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Oui, c'est bien {prenom}", callback_data="prenom_oui")],
        [InlineKeyboardButton("✏️ Non, je vais corriger",     callback_data="prenom_non")],
    ])

def kb_level():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1️⃣ Je n'ai jamais tradé",                    callback_data="level_1")],
        [InlineKeyboardButton("2️⃣ J'ai débuté mais sans résultats",         callback_data="level_2")],
        [InlineKeyboardButton("3️⃣ Je trade avec des résultats irréguliers", callback_data="level_3")],
        [InlineKeyboardButton("4️⃣ Je suis déjà rentable",                   callback_data="level_4")],
    ])

def kb_objectif():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📚 Apprendre une méthode simple et rapide", callback_data="obj_methode")],
        [InlineKeyboardButton("🎥 Voir une démonstration en direct",       callback_data="obj_demo")],
        [InlineKeyboardButton("💪 Gagner en confiance",                    callback_data="obj_confiance")],
        [InlineKeyboardButton("✍️ Autre",                                  callback_data="obj_autre")],
    ])

def kb_confirmation():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🚀 Confirmer mon inscription !", callback_data="confirme")
    ]])

def kb_relance():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Je finalise mon inscription", callback_data="relance_go")
    ]])


# ════════════════════════════════════════════════════════════════════════════
# ── VIDÉO DE BIENVENUE ───────────────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════

async def send_welcome_video(bot, user_id: int):
    log_member(user_id)
    with db() as conn:
        conn.execute(
            """INSERT INTO users (telegram_id, categorie)
               VALUES (?, ?)
               ON CONFLICT(telegram_id) DO UPDATE SET categorie = excluded.categorie""",
            (user_id, EVENEMENT_ACTUEL)
        )
        conn.commit()

    video_name = "welcomes_2"
    file_id    = get_file_id(video_name)
    caption    = (
        "🚀 *Bienvenue !*\n\n"
        "Tu es sur le point de réserver ta place à la masterclass gratuite :\n"
        "*« Capturer les meilleures impulsions d'une tendance en 5 minutes »*"
    )

    if file_id:
        await bot.send_video(chat_id=user_id, video=file_id,
                             caption=caption, parse_mode="Markdown")
    else:
        msg = await bot.send_video(
            chat_id=user_id,
            video=open("video/welcome.mp4", "rb"),
            caption=caption, parse_mode="Markdown"
        )
        save_file_id(video_name, msg.video.file_id)

    await bot.send_message(
        chat_id=user_id,
        text=(
            f"⚠️ *Il ne reste que peu de place\n"
            "Les places s'envolent vite. Sécurise la tienne maintenant.\n\n"
            "👇 Clique ici pour réserver ta place :\n\n"
            "/JeMEnregistre"
        ),
        parse_mode="Markdown"
    )


async def _send_welcome_safe(bot, user_id: int):
    try:
        await send_welcome_video(bot, user_id)
    except Exception as e:
        print(f"Erreur bienvenue uid={user_id} : {e}")


async def _reply_already_registered(bot, user_id: int):
    await bot.send_message(
        chat_id=user_id,
        text=(
            "✅ *Tu es déjà inscrit à la masterclass*\n\n"
            "Pas besoin de t'enregistrer une deuxième fois 😊\n\n"
            "Tu recevras le lien du live et tous les rappels "
            "par *WhatsApp et Telegram* avant l'événement.\n\n"
            "Hâte de te voir en ligne 🔥"
        ),
        parse_mode="Markdown"
    )


# ════════════════════════════════════════════════════════════════════════════
# ── HANDLERS ENTRÉE ──────────────────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════

async def _handle_join(bot, user_id: int):
    try:
        if _is_already_registered(user_id):
            await _reply_already_registered(bot, user_id)
        else:
            await send_welcome_video(bot, user_id)
    except Exception as e:
        print(f"Erreur join uid={user_id} : {e}")


async def approve_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.chat_join_request.from_user.id
    try:
        await update.chat_join_request.approve()
    except Exception as e:
        # "User_already_participant" : deja dans le canal, on envoie quand meme le message
        print(f"approve() uid={user_id} : {e}")
    asyncio.create_task(_handle_join(context.bot, user_id))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args    = context.args

    # Lien profond : t.me/bot?start=JeMenregistre
    if args and args[0] == "JeMenregistre":
        await update.message.reply_text(
            "Super 🎉 Clique sur /JeMEnregistre pour t'inscrire à la masterclass.",
            parse_mode="Markdown"
        )
        return

    _touch_last_seen(user_id)
    if _is_already_registered(user_id):
        await _reply_already_registered(context.bot, user_id)
        return
    asyncio.create_task(_send_welcome_safe(context.bot, user_id))


# ════════════════════════════════════════════════════════════════════════════
# ── MESSAGES LIBRES → REDIRECTION ADMIN ──────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════

async def message_libre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    user_id = user.id
    texte   = update.message.text or ""

    _touch_last_seen(user_id)
    _log_message(user_id, texte)

    await update.message.reply_text(
        "📨 Ton message a bien été reçu.\n\n"
        f"Pour une réponse rapide et personnalisée, contacte directement "
        f"*Charbel* sur Telegram : {ADMIN_USERNAME}\n\n"
        "Il te répondra dès que possible 😊",
        parse_mode="Markdown"
    )

    username  = f"@{user.username}" if user.username else f"id:{user_id}"
    prenom_tg = user.first_name or ""
    notif = (
        f"💬 *Nouveau message reçu*\n\n"
        f"👤 {prenom_tg} ({username})\n"
        f"ID : `{user_id}`\n\n"
        f"_{texte}_"
    )
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=notif,
                                           parse_mode="Markdown")
        except Exception as e:
            print(f"Notif admin {admin_id} : {e}")


# ════════════════════════════════════════════════════════════════════════════
# ── RELANCE INSCRIPTIONS INCOMPLÈTES ─────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════

async def relancer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Commande réservée à l'administrateur.")
        return

    users = _get_incomplete_users()
    if not users:
        await update.message.reply_text("✅ Aucune inscription incomplète à relancer.")
        return

    await update.message.reply_text(
        f"📤 Relance en cours pour *{len(users)}* utilisateurs...",
        parse_mode="Markdown"
    )
    asyncio.create_task(_broadcast_relance(context.bot, update.effective_user.id, users))


async def _broadcast_relance(bot, admin_id: int, users: list[dict]):
    sent = 0
    for u in users:
        uid    = u["telegram_id"]
        prenom = u["prenom"] or "Hello l'ami"
        try:
            await bot.send_message(
                chat_id=uid,
                text=(
                    f"⚠️ *{prenom}, ton inscription n'a pas encore été validée*\n\n"
                    "Tu as commencé à t'inscrire à la masterclass gratuite, "
                    "mais tu n'as pas finalisé ta demande.\n\n"
                    f"Il ne reste que *{PLACES_RESTANTES} places sur {PLACES_TOTALES}* "
                    "et elles partent vite.\n\n"
                    "Clique sur le bouton ci-dessous pour sécuriser ta place :"
                ),
                parse_mode="Markdown",
                reply_markup=kb_relance()
            )
            sent += 1
        except Exception as e:
            print(f"Relance uid={uid} : {e}")
        await asyncio.sleep(0.1)

    await bot.send_message(
        admin_id,
        f"Relance terminée — *{sent}/{len(users)}* messages envoyés",
        parse_mode="Markdown"
    )


async def relance_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "Super 🎉 Clique sur /JeMEnregistre pour finaliser ton inscription.",
        parse_mode="Markdown"
    )


# ════════════════════════════════════════════════════════════════════════════
# ── GESTION DES CATÉGORIES ────────────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════

async def nouvelle_categorie_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Commande réservée à l'administrateur.")
        return ConversationHandler.END

    cats  = _get_categories()
    liste = "\n".join(f"• {c}" for c in cats) if cats else "_(aucune pour l'instant)_"
    await update.message.reply_text(
        f"*Catégories actuelles :*\n\n{liste}\n\n"
        "Envoie le *nom* du nouvel événement à créer :",
        parse_mode="Markdown"
    )
    return CAT_NOM


async def nouvelle_categorie_nom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nom = update.message.text.strip()
    if len(nom) < 3:
        await update.message.reply_text("❌ Nom trop court (min 3 caractères). Réessaie :")
        return CAT_NOM
    _ajouter_categorie(nom)
    await update.message.reply_text(
        f"✅ Catégorie *{nom}* créée avec succès.",
        parse_mode="Markdown"
    )
    return ConversationHandler.END


async def nouvelle_categorie_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Création annulée.")
    return ConversationHandler.END


# ════════════════════════════════════════════════════════════════════════════
# ── BROADCAST ─────────────────────────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════

async def _broadcast_all(bot, admin_id: int, data: dict):
    user_ids = _get_all_user_ids()
    total    = len(user_ids)
    fmt      = data.get("format")
    texte    = data.get("text_content", "")
    media_id = data.get("media_file_id")

    if total == 0:
        await bot.send_message(admin_id, "❌ Aucun utilisateur enregistré.")
        return
    if fmt in {"2", "3", "4", "5"} and not media_id:
        await bot.send_message(admin_id, "❌ Fichier média manquant. Diffusion annulée.")
        return

    est = round(total * 0.1 / 60, 2)
    await bot.send_message(admin_id,
        f"📤 Envoi en cours à *{total}* utilisateurs\nEstimé : {est} min",
        parse_mode="Markdown")

    sent = 0
    for idx, uid in enumerate(user_ids, start=1):
        try:
            if fmt == "1":   await bot.send_message(chat_id=uid, text=texte)
            elif fmt == "2": await bot.send_photo(chat_id=uid, photo=media_id, caption=texte)
            elif fmt == "3": await bot.send_video(chat_id=uid, video=media_id, caption=texte)
            elif fmt == "4": await bot.send_photo(chat_id=uid, photo=media_id)
            elif fmt == "5": await bot.send_video(chat_id=uid, video=media_id)
            sent += 1
        except Exception as e:
            print(f"Broadcast uid={uid} : {e}")

        if   idx == total // 3:      await bot.send_message(admin_id, "1/3 des messages envoyés")
        elif idx == (2*total) // 3:  await bot.send_message(admin_id, "2/3 des messages envoyés")
        elif idx == total:           await bot.send_message(admin_id,
            f"Diffusion terminée — *{sent}/{total}* messages envoyés", parse_mode="Markdown")

        await asyncio.sleep(0.1)


async def bc_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Commande réservée à l'administrateur.")
        return ConversationHandler.END
    await update.message.reply_text(
        "*Format du message a diffuser :*\n\n"
        "1 - Texte seul\n"
        "2 - Image + texte\n"
        "3 - Video + texte\n"
        "4 - Image seule\n"
        "5 - Video seule\n\n"
        "_(max 4096 caracteres)_",
        parse_mode="Markdown", reply_markup=ReplyKeyboardRemove()
    )
    return BC_FORMAT


async def bc_get_format(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choix = update.message.text.strip()[0]
    if choix not in {"1","2","3","4","5"}:
        await update.message.reply_text("❌ Chiffre entre 1 et 5 uniquement.")
        return BC_FORMAT
    context.user_data["bc_format"] = choix
    if choix in {"2","3"}:
        await update.message.reply_text(f"Envoie ton fichier {'image' if choix=='2' else 'video'}.")
        return BC_MEDIA
    await update.message.reply_text("Envoie maintenant ton texte." if choix == "1"
                                    else "Envoie ton fichier.")
    return BC_TEXT


async def bc_get_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choix = context.user_data["bc_format"]
    if choix == "2":
        if not update.message.photo:
            await update.message.reply_text("❌ Ce n'est pas une image. Reessaie.")
            return BC_MEDIA
        context.user_data["bc_media_id"] = update.message.photo[-1].file_id
    elif choix == "3":
        if not update.message.video:
            await update.message.reply_text("❌ Ce n'est pas une video. Reessaie.")
            return BC_MEDIA
        context.user_data["bc_media_id"] = update.message.video.file_id
    await update.message.reply_text("Envoie maintenant le texte associe.")
    return BC_TEXT


async def bc_get_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id
    choix    = context.user_data["bc_format"]
    if choix == "4":
        if not update.message.photo:
            await update.message.reply_text("❌ Ce n'est pas une image.")
            return BC_TEXT
        context.user_data["bc_media_id"] = update.message.photo[-1].file_id
        context.user_data["bc_text"]     = ""
    elif choix == "5":
        if not update.message.video:
            await update.message.reply_text("❌ Ce n'est pas une video.")
            return BC_TEXT
        context.user_data["bc_media_id"] = update.message.video.file_id
        context.user_data["bc_text"]     = ""
    else:
        if not update.message.text:
            await update.message.reply_text("❌ Merci d'envoyer du texte.")
            return BC_TEXT
        context.user_data["bc_text"] = update.message.text

    await update.message.reply_text("Diffusion lancee en arriere-plan...")
    asyncio.create_task(_broadcast_all(context.bot, admin_id, {
        "format":        choix,
        "text_content":  context.user_data.get("bc_text", ""),
        "media_file_id": context.user_data.get("bc_media_id"),
    }))
    return ConversationHandler.END


async def bc_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Diffusion annulee.")
    return ConversationHandler.END


# ════════════════════════════════════════════════════════════════════════════
# ── /stats ────────────────────────────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Commande réservée à l'administrateur.")
        return

    with db() as conn:
        total    = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        complets = conn.execute("SELECT COUNT(*) FROM users WHERE completed=1").fetchone()[0]
        try:
            members = conn.execute("SELECT COUNT(*) FROM members").fetchone()[0]
        except Exception:
            members = "—"
        try:
            msgs = conn.execute("SELECT COUNT(*) FROM messages_libres").fetchone()[0]
        except Exception:
            msgs = "—"

    await update.message.reply_text(
        f"*Statistiques :*\n\n"
        f"Membres canal : *{members}*\n\n"
        f"Formulaire demarre : *{total}*\n"
        f"Inscriptions completes : *{complets}*\n"
        f"En cours : *{total - complets}*\n\n"
        f"Messages libres recus : *{msgs}*",
        parse_mode="Markdown"
    )


# ════════════════════════════════════════════════════════════════════════════
# ── CONVERSATION INSCRIPTION ──────────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════

async def je_me_enregistre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    _touch_last_seen(user_id)
    if _is_already_registered(user_id):
        await _reply_already_registered(context.bot, user_id)
        return ConversationHandler.END

    await update.message.reply_text(
        "Super 🎉\n\n"
        "Je suis l'assistant de Charbel Yayi 👋\n"
        "Je vais te guider etape par etape pour ton inscription.\n\n"
        "Comment tu t'appelles ? 😊\n\n"
        "_Reponds simplement avec ton prenom_",
        parse_mode="Markdown"
    )
    return PRENOM


async def get_prenom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    texte   = update.message.text.strip()
    _touch_last_seen(user_id)

    prenom_candidat, confirmation_requise = _extraire_prenom(texte)

    if confirmation_requise:
        context.user_data["prenom_candidat"] = prenom_candidat
        context.user_data["prenom_original"]  = texte

        if len(prenom_candidat) > 15:
            await update.message.reply_text(
                f"J'ai du mal a identifier ton prenom dans ce que tu as ecrit.\n\n"
                "Peux-tu m'envoyer *uniquement ton prenom* s'il te plait ?",
                parse_mode="Markdown"
            )
            return PRENOM

        await update.message.reply_text(
            f"Est-ce que ton prenom est *{prenom_candidat}* ?",
            parse_mode="Markdown",
            reply_markup=kb_prenom_confirm(prenom_candidat)
        )
        return PRENOM_CONFIRM

    prenom = prenom_candidat
    context.user_data["prenom"] = prenom
    upsert_user(user_id, prenom=prenom)
    return await _ask_level(update.message, prenom)


async def confirm_prenom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "prenom_oui":
        prenom = context.user_data["prenom_candidat"]
        context.user_data["prenom"] = prenom
        upsert_user(user_id, prenom=prenom)
        await query.message.reply_text(f"Parfait *{prenom}* 👋", parse_mode="Markdown")
        return await _ask_level(query.message, prenom)

    await query.message.reply_text(
        "Pas de souci 😊\n\nEnvoie-moi juste ton prenom :",
        parse_mode="Markdown"
    )
    return PRENOM


async def _ask_level(message, prenom: str) -> int:
    await message.reply_text(
        f"Enchante *{prenom}* 👋\n\n"
        "*Quelle est ton experience actuelle en trading ?*",
        parse_mode="Markdown",
        reply_markup=kb_level()
    )
    return LEVEL


async def get_level(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    level_map = {
        "level_1": "Je n'ai jamais trade",
        "level_2": "J'ai debute mais sans resultats",
        "level_3": "Je trade avec des resultats irreguliers",
        "level_4": "Je suis deja rentable",
    }
    level = level_map.get(query.data, "Non precise")
    context.user_data["level"] = level
    upsert_user(user_id, level=level)
    await query.message.reply_text(
        "*Quel est ton principal objectif en rejoignant cette masterclass ?*",
        parse_mode="Markdown", reply_markup=kb_objectif()
    )
    return OBJECTIF


async def get_objectif(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    obj_map = {
        "obj_methode":   "Apprendre une methode simple et rapide",
        "obj_demo":      "Voir une demonstration en direct",
        "obj_confiance": "Gagner en confiance",
        "obj_autre":     "Autre",
    }
    objectif = obj_map.get(query.data, "Non precise")
    context.user_data["objectif"] = objectif
    upsert_user(user_id, objectif=objectif)
    await query.message.reply_text(
        "*Quel est ton numero WhatsApp ?*\n"
        "Je t'enverrai les rappels pour la masterclass 😊\n\n"
        "_(Ex : +229 60619292)_",
        parse_mode="Markdown"
    )
    return WHATSAPP


async def get_whatsapp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id  = update.message.from_user.id
    whatsapp = update.message.text.strip()
    context.user_data["whatsapp"] = whatsapp
    upsert_user(user_id, whatsapp=whatsapp)
    await update.message.reply_text(
        "*Laisse-moi aussi ton adresse mail :*",
        parse_mode="Markdown"
    )
    return EMAIL


async def get_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id  = update.message.from_user.id
    email    = update.message.text.strip()
    context.user_data["email"] = email
    upsert_user(user_id, email=email)

    prenom   = context.user_data.get("prenom", "")
    level    = context.user_data.get("level", "")
    objectif = context.user_data.get("objectif", "")
    whatsapp = context.user_data.get("whatsapp", "")

    await update.message.reply_text(
        f"*Recapitulatif de ton inscription :*\n\n"
        f"Prenom : *{prenom}*\n"
        f"Niveau : *{level}*\n"
        f"Objectif : *{objectif}*\n"
        f"WhatsApp : *{whatsapp}*\n"
        f"Email : *{email}*\n\n"
        "En confirmant, tu acceptes de recevoir les rappels par WhatsApp et Telegram.\n"
        "Tu peux te desinscrire a tout moment.\n\n"
        "👇",
        parse_mode="Markdown", reply_markup=kb_confirmation()
    )
    return CONFIRMATION


async def confirmer_inscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    prenom  = context.user_data.get("prenom", "")
    upsert_user(user_id, completed=1)
    await query.message.reply_text(
        f"*Felicitations {prenom}*\n\n"
        "Tu es officiellement inscrit a la masterclass gratuite :\n"
        "*« Capturer les meilleures impulsions d'une tendance en 5 minutes »*\n\n"
        "Tu recevras le lien du live et les rappels par *WhatsApp et Telegram*.\n\n"
        "Hate de te voir en ligne 🔥",
        parse_mode="Markdown"
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Inscription annulee. Tape /JeMEnregistre pour recommencer."
    )
    return ConversationHandler.END


async def timeout_inscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Declenche automatiquement apres 5 minutes d'inactivite.
    update.message peut etre None dans ce contexte, on utilise effective_chat.
    """
    user_id = update.effective_user.id if update.effective_user else None
    if user_id:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "⏰ Ta session a expire apres 5 minutes d'inactivite.\n\n"
                    "Ton inscription n'a pas ete enregistree.\n\n"
                    "Quand tu es pret, clique sur /JeMEnregistre pour recommencer !"
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            print(f"Timeout message uid={user_id} : {e}")


# ════════════════════════════════════════════════════════════════════════════
# ── GESTIONNAIRE D'ERREURS GLOBAL ────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════

async def export_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /export_users — admin uniquement.
    Exporte toute la table users en fichier Excel et l'envoie en DM.
    """
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Commande reservee a l administrateur.")
        return

    await update.message.reply_text("Generation du fichier Excel en cours...")

    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
        import io

        with db() as conn:
            rows = conn.execute("""
                SELECT telegram_id, prenom, level, objectif, whatsapp, email,
                       categorie, completed, last_seen
                FROM users
                ORDER BY rowid DESC
            """).fetchall()

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Inscrits"

        # En-têtes
        headers = ["ID Telegram", "Prenom", "Niveau", "Objectif",
                   "WhatsApp", "Email", "Categorie", "Complet", "Derniere activite"]
        header_fill = PatternFill("solid", fgColor="1F4E79")
        header_font = Font(bold=True, color="FFFFFF")

        for col, h in enumerate(headers, start=1):
            cell = ws.cell(row=1, column=col, value=h)
            cell.fill   = header_fill
            cell.font   = header_font
            cell.alignment = Alignment(horizontal="center")
            ws.column_dimensions[cell.column_letter].width = 22

        # Données
        for row_idx, row in enumerate(rows, start=2):
            values = [
                row["telegram_id"],
                row["prenom"]   or "",
                row["level"]    or "",
                row["objectif"] or "",
                row["whatsapp"] or "",
                row["email"]    or "",
                row["categorie"] or "",
                "Oui" if row["completed"] == 1 else "Non",
                row["last_seen"] or "",
            ]
            fill_color = "EBF3FB" if row_idx % 2 == 0 else "FFFFFF"
            fill = PatternFill("solid", fgColor=fill_color)
            for col_idx, val in enumerate(values, start=1):
                cell = ws.cell(row=row_idx, column=col_idx, value=val)
                cell.fill = fill

        # Sauvegarder en memoire
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        total = len(rows)
        complets = sum(1 for r in rows if r["completed"] == 1)

        await update.message.reply_document(
            document=buf,
            filename="inscrits.xlsx",
            caption=(
                "Export de la table users\n\n"
                + "Total : *" + str(total) + "* utilisateurs\n"
                + "Inscriptions completes : *" + str(complets) + "*"
            ),
            parse_mode="Markdown"
        )

    except ImportError:
        await update.message.reply_text(
            "Le module openpyxl n est pas installe. Lance : pip install openpyxl"
        )
    except Exception as e:
        await update.message.reply_text(f"Erreur lors de l export : {e}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """
    Capture toutes les exceptions non gerees et notifie les admins.
    Envoie : type d'erreur + traceback complet + contexte (user, commande).
    """
    tb = "".join(traceback.format_exception(
        type(context.error), context.error, context.error.__traceback__
    ))

    uname = "?"
    texte = ""
    if isinstance(update, Update):
        user  = update.effective_user
        uname = f"@{user.username}" if user and user.username else str(user.id if user else "?")
        msg   = update.message or (update.callback_query.message if update.callback_query else None)
        texte = (msg.text or "")[:100] if msg else ""

    # Tronquer le traceback a 2000 chars max (limite Telegram)
    tb_court = tb[-2000:] if len(tb) > 2000 else tb

    ligne_sep = "\n"
    notif = (
        "*ERREUR BOT*" + ligne_sep + ligne_sep
        + "User : " + uname + ligne_sep
        + "Message : _" + texte + "_" + ligne_sep + ligne_sep
        + "`" + tb_court + "`"
    )
    print(f"[ERROR] {tb}")

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=notif,
                parse_mode="Markdown"
            )
        except Exception as e:
            print(f"Impossible d'envoyer l'erreur a l'admin {admin_id} : {e}")

# ════════════════════════════════════════════════════════════════════════════
# ── MAIN ──────────────────────────────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    init_db()
    _migrate_db()

    app = (
        Application.builder()
        .token(TOKEN)
        .read_timeout(30)
        .write_timeout(30)
        .build()
    )

    app.add_handler(ChatJoinRequestHandler(approve_join_request))
    app.add_handler(CommandHandler("start",              start))
    app.add_handler(CommandHandler("stats",              stats))
    app.add_handler(CommandHandler("relancer",           relancer))
    app.add_handler(CommandHandler("export_users",       export_users))
    app.add_handler(CallbackQueryHandler(relance_callback, pattern="^relance_go$"))

    conv_inscription = ConversationHandler(
        entry_points=[CommandHandler("JeMEnregistre", je_me_enregistre)],
        states={
            PRENOM:         [MessageHandler(filters.TEXT & ~filters.COMMAND, get_prenom)],
            PRENOM_CONFIRM: [CallbackQueryHandler(confirm_prenom, pattern="^prenom_(oui|non)$")],
            LEVEL:          [CallbackQueryHandler(get_level,    pattern="^level_")],
            OBJECTIF:       [CallbackQueryHandler(get_objectif, pattern="^obj_")],
            WHATSAPP:       [MessageHandler(filters.TEXT & ~filters.COMMAND, get_whatsapp)],
            EMAIL:          [MessageHandler(filters.TEXT & ~filters.COMMAND, get_email)],
            CONFIRMATION:   [CallbackQueryHandler(confirmer_inscription, pattern="^confirme$")],
            ConversationHandler.TIMEOUT: [
                MessageHandler(filters.ALL, timeout_inscription),
                CallbackQueryHandler(timeout_inscription),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=False, per_user=True, allow_reentry=True,
        conversation_timeout=300,   # 5 minutes d'inactivite
    )

    conv_broadcast = ConversationHandler(
        entry_points=[CommandHandler("envoyer", bc_start)],
        states={
            BC_FORMAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, bc_get_format)],
            BC_MEDIA:  [MessageHandler(filters.PHOTO | filters.VIDEO,   bc_get_media)],
            BC_TEXT:   [MessageHandler(filters.TEXT | filters.PHOTO | filters.VIDEO, bc_get_text)],
        },
        fallbacks=[CommandHandler("cancel", bc_cancel)],
        per_chat=False, per_user=True, allow_reentry=True,
    )

    conv_categorie = ConversationHandler(
        entry_points=[CommandHandler("nouvelle_categorie", nouvelle_categorie_start)],
        states={
            CAT_NOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, nouvelle_categorie_nom)],
        },
        fallbacks=[CommandHandler("cancel", nouvelle_categorie_cancel)],
        per_chat=False, per_user=True, allow_reentry=True,
    )

    app.add_error_handler(error_handler)
    app.add_handler(conv_inscription)
    app.add_handler(conv_broadcast)
    app.add_handler(conv_categorie)

    # En dernier — capture tout message hors conversation
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_libre))

    print("start...")
    app.run_polling(poll_interval=1, allowed_updates=Update.ALL_TYPES)