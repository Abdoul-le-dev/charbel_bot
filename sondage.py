"""
sondage.py — Module autonome de gestion des sondages.

Commandes admin :
  /ajout_sondage      → créer un sondage (question + options + messages retour)
  /envoyer_sondage    → choisir un sondage et l'envoyer (tous les users ou un seul)
  /stats_sondage      → voir les statistiques de réponses par sondage

Intégration dans bot.py (2 lignes uniquement) :
  from sondage import init_sondage_db, register_sondage_handlers

  # dans if __name__ == "__main__":
  init_sondage_db()
  register_sondage_handlers(app)    # à appeler AVANT app.run_polling()
"""

import asyncio
import sqlite3

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, ConversationHandler, CommandHandler,
    MessageHandler, CallbackQueryHandler, filters, ContextTypes
)

DB_PATH   = "preinscriptions.db"
ADMIN_IDS = {6992809421, 6799962131}

# ── États conversation /ajout_sondage ─────────────────────────────────────────
S_QUESTION, S_NB_OPTIONS, S_OPTION_LABEL, S_OPTION_MSG, S_CONFIRMER = range(30, 35)

# ── États conversation /envoyer_sondage ───────────────────────────────────────
E_CHOIX_SONDAGE, E_CHOIX_CIBLE, E_USER_ID = range(35, 38)


# ════════════════════════════════════════════════════════════════════════════
# ── BASE DE DONNÉES ───────────────────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_sondage_db():
    """Crée les 3 tables si elles n'existent pas. À appeler au démarrage du bot."""
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sondages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                question   TEXT NOT NULL,
                actif      INTEGER DEFAULT 1,
                cree_le    DATETIME DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS sondage_options (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                sondage_id     INTEGER NOT NULL REFERENCES sondages(id),
                label          TEXT NOT NULL,
                message_retour TEXT NOT NULL,
                ordre          INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS sondage_reponses (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                sondage_id  INTEGER NOT NULL REFERENCES sondages(id),
                option_id   INTEGER NOT NULL REFERENCES sondage_options(id),
                telegram_id INTEGER NOT NULL,
                repondu_le  DATETIME DEFAULT (datetime('now')),
                UNIQUE(sondage_id, telegram_id)
            );
        """)
        conn.commit()


# ── Helpers DB ────────────────────────────────────────────────────────────────

def _save_sondage(question: str, options: list[dict]) -> int:
    with _conn() as conn:
        cur = conn.execute("INSERT INTO sondages (question) VALUES (?)", (question,))
        sid = cur.lastrowid
        for i, opt in enumerate(options):
            conn.execute(
                "INSERT INTO sondage_options (sondage_id, label, message_retour, ordre) VALUES (?,?,?,?)",
                (sid, opt["label"], opt["message_retour"], i)
            )
        conn.commit()
    return sid


def _get_sondages_actifs() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, question, cree_le FROM sondages WHERE actif = 1 ORDER BY id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def _get_options(sondage_id: int) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, label, message_retour FROM sondage_options WHERE sondage_id = ? ORDER BY ordre",
            (sondage_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def _get_all_user_ids() -> list[int]:
    with _conn() as conn:
        rows = conn.execute("""
            SELECT telegram_id FROM users WHERE telegram_id IS NOT NULL
            UNION
            SELECT telegram_id FROM members_log
        """).fetchall()
    return [r["telegram_id"] for r in rows]


def _enregistrer_reponse(sondage_id: int, option_id: int, telegram_id: int) -> bool:
    try:
        with _conn() as conn:
            conn.execute(
                "INSERT INTO sondage_reponses (sondage_id, option_id, telegram_id) VALUES (?,?,?)",
                (sondage_id, option_id, telegram_id)
            )
            conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def _get_message_retour(option_id: int) -> str:
    with _conn() as conn:
        row = conn.execute(
            "SELECT message_retour FROM sondage_options WHERE id = ?", (option_id,)
        ).fetchone()
    return row["message_retour"] if row else ""


def _get_stats(sondage_id: int) -> dict:
    with _conn() as conn:
        sondage = conn.execute(
            "SELECT question FROM sondages WHERE id = ?", (sondage_id,)
        ).fetchone()
        rows = conn.execute("""
            SELECT o.label, COUNT(r.id) as nb
            FROM sondage_options o
            LEFT JOIN sondage_reponses r ON r.option_id = o.id AND r.sondage_id = o.sondage_id
            WHERE o.sondage_id = ?
            GROUP BY o.id ORDER BY o.ordre
        """, (sondage_id,)).fetchall()
    return {
        "question": sondage["question"] if sondage else "?",
        "options":  [dict(r) for r in rows],
        "total":    sum(r["nb"] for r in rows),
    }


# ════════════════════════════════════════════════════════════════════════════
# ── ENVOI D'UN SONDAGE ────────────────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════

def _kb_sondage(sondage_id: int, options: list[dict]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(opt["label"], callback_data=f"vote_{sondage_id}_{opt['id']}")]
        for opt in options
    ])


def _get_prenom_user(telegram_id: int) -> str:
    """
    Retourne le prénom du user depuis la table users.
    - Présent et <= 15 caractères → on l'utilise
    - Absent ou > 15 caractères   → "l'ami"
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT prenom FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
    if row and row["prenom"]:
        p = row["prenom"].strip()
        if 1 <= len(p) <= 15:
            return p
    return "l'ami"


def _injecter_prenom(texte: str, prenom: str) -> str:
    """Remplace toutes les occurrences de +prenom par le prénom du user."""
    return texte.replace("+prenom", prenom)


async def _send_to_user(bot, chat_id: int, sondage_id: int):
    with _conn() as conn:
        s = conn.execute(
            "SELECT question FROM sondages WHERE id = ? AND actif = 1", (sondage_id,)
        ).fetchone()
    if not s:
        return
    options = _get_options(sondage_id)
    if not options:
        return

    prenom   = _get_prenom_user(chat_id)
    question = _injecter_prenom(s["question"], prenom)

    # Pas de parse_mode : textes saisis librement par l'admin
    await bot.send_message(
        chat_id=chat_id,
        text=question + "\n\nClique sur un bouton pour valider ton choix :",
        reply_markup=_kb_sondage(sondage_id, options)
    )


# ════════════════════════════════════════════════════════════════════════════
# ── CALLBACK : vote utilisateur ───────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════

async def _vote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts      = query.data.split("_")
    sondage_id = int(parts[1])
    option_id  = int(parts[2])
    user_id    = query.from_user.id

    deja = not _enregistrer_reponse(sondage_id, option_id, user_id)

    if deja:
        await query.message.reply_text("Tu as deja repondu a ce sondage. Merci !")
        return

    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    # Injection du prénom dans le message_retour avant envoi
    prenom         = _get_prenom_user(user_id)
    message_retour = _injecter_prenom(_get_message_retour(option_id), prenom)
    await query.message.reply_text(message_retour)


# ════════════════════════════════════════════════════════════════════════════
# ── /ajout_sondage ────────────────────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════

async def _ajout_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Commande reservee a l'administrateur.")
        return ConversationHandler.END

    context.user_data["wip"] = {"options": []}
    await update.message.reply_text(
        "Creation d'un sondage\n\n"
        "Envoie la question du sondage :\n\n"
        "/cancel pour annuler"
    )
    return S_QUESTION


async def _ajout_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.message.text.strip()
    if len(q) < 5:
        await update.message.reply_text("Question trop courte (min 5 caracteres). Reessaie :")
        return S_QUESTION
    context.user_data["wip"]["question"] = q
    await update.message.reply_text(
        f"Question enregistree.\n\nCombien de reponses possibles ? (2 a 8)"
    )
    return S_NB_OPTIONS


async def _ajout_nb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if not t.isdigit() or not (2 <= int(t) <= 8):
        await update.message.reply_text("Envoie un chiffre entre 2 et 8 :")
        return S_NB_OPTIONS
    nb = int(t)
    context.user_data["wip"]["nb"] = nb
    context.user_data["wip"]["options"] = []
    await update.message.reply_text(
        f"Tu vas creer {nb} options.\n\nOption 1/{nb} — Libelle du bouton :"
    )
    return S_OPTION_LABEL


async def _ajout_label(update: Update, context: ContextTypes.DEFAULT_TYPE):
    label = update.message.text.strip()
    if not label:
        await update.message.reply_text("Libelle vide. Reessaie :")
        return S_OPTION_LABEL
    context.user_data["wip"]["label_tmp"] = label
    idx = len(context.user_data["wip"]["options"]) + 1
    await update.message.reply_text(
        f"Option {idx} — libelle : {label}\n\n"
        "Envoie maintenant le message de retour que recevra l'utilisateur "
        "s'il choisit cette option :"
    )
    return S_OPTION_MSG


async def _ajout_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message.text.strip()
    if len(msg) < 5:
        await update.message.reply_text("Message trop court. Reessaie :")
        return S_OPTION_MSG

    wip   = context.user_data["wip"]
    label = wip.pop("label_tmp")
    wip["options"].append({"label": label, "message_retour": msg})

    options = wip["options"]
    nb      = wip["nb"]
    idx     = len(options)

    if idx < nb:
        await update.message.reply_text(
            f"Option {idx} enregistree.\n\nOption {idx+1}/{nb} — Libelle du bouton :"
        )
        return S_OPTION_LABEL

    # Recapitulatif — texte brut uniquement, aucun parse_mode
    recap = "\n\n".join(
        f"{i+1}. {o['label']}\n-> {o['message_retour'][:100]}{'...' if len(o['message_retour']) > 100 else ''}"
        for i, o in enumerate(options)
    )
    await update.message.reply_text(
        f"Recapitulatif :\n\n"
        f"Question : {wip['question']}\n\n"
        f"{recap}\n\n"
        "Tape oui pour creer ce sondage, non pour annuler."
    )
    return S_CONFIRMER


async def _ajout_confirmer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rep = update.message.text.strip().lower()
    if rep not in ("oui", "non"):
        await update.message.reply_text("Tape oui ou non :")
        return S_CONFIRMER
    if rep == "non":
        await update.message.reply_text("Creation annulee.")
        return ConversationHandler.END

    wip = context.user_data["wip"]
    sid = _save_sondage(wip["question"], wip["options"])
    await update.message.reply_text(
        f"Sondage #{sid} enregistre en base !\n\n"
        f"Lance /envoyer_sondage pour l'envoyer a tes utilisateurs."
    )
    return ConversationHandler.END


async def _ajout_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Creation annulee.")
    return ConversationHandler.END


# ════════════════════════════════════════════════════════════════════════════
# ── /envoyer_sondage ──────────────────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════

def _kb_liste(sondages: list[dict], prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"#{s['id']} — {s['question'][:55]}", callback_data=f"{prefix}{s['id']}")]
        for s in sondages
    ])


def _kb_cible() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Tous les utilisateurs", callback_data="cible_tous")],
        [InlineKeyboardButton("Un utilisateur precis",  callback_data="cible_un")],
    ])


async def _envoyer_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Commande reservee a l'administrateur.")
        return ConversationHandler.END

    sondages = _get_sondages_actifs()
    if not sondages:
        await update.message.reply_text(
            "Aucun sondage disponible. Cree-en un avec /ajout_sondage."
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "Envoyer un sondage\n\nChoisis lequel :",
        reply_markup=_kb_liste(sondages, "qs_")
    )
    return E_CHOIX_SONDAGE


async def _envoyer_choix_sondage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["send_sid"] = int(query.data.split("_")[1])
    await query.message.reply_text(
        "A qui envoyer ce sondage ?",
        reply_markup=_kb_cible()
    )
    return E_CHOIX_CIBLE


async def _envoyer_choix_cible(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    await query.answer()
    sid      = context.user_data["send_sid"]
    admin_id = query.from_user.id

    if query.data == "cible_tous":
        await query.message.reply_text("Diffusion en cours en arriere-plan...")
        asyncio.create_task(_broadcast(query.message.get_bot(), admin_id, sid))
        return ConversationHandler.END

    await query.message.reply_text("Envoie l'ID Telegram de l'utilisateur :")
    return E_USER_ID


async def _envoyer_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if not t.isdigit():
        await update.message.reply_text("ID invalide (chiffres uniquement) :")
        return E_USER_ID

    uid = int(t)
    sid = context.user_data["send_sid"]
    try:
        await _send_to_user(context.bot, uid, sid)
        await update.message.reply_text(f"Sondage envoye a l'utilisateur {uid}.")
    except Exception as e:
        await update.message.reply_text(f"Erreur : {e}")
    return ConversationHandler.END


async def _envoyer_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Envoi annule.")
    return ConversationHandler.END


async def _broadcast(bot, admin_id: int, sondage_id: int):
    user_ids = _get_all_user_ids()
    total    = len(user_ids)
    sent = errors = 0

    for uid in user_ids:
        try:
            await _send_to_user(bot, uid, sondage_id)
            sent += 1
        except Exception as e:
            errors += 1
            print(f"Broadcast sondage uid={uid} : {e}")
        await asyncio.sleep(0.1)

    await bot.send_message(
        admin_id,
        f"Diffusion terminee\n\nEnvoyes : {sent}/{total}\nErreurs : {errors}"
    )


# ════════════════════════════════════════════════════════════════════════════
# ── /stats_sondage ────────────────────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════

async def _stats_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Commande reservee a l'administrateur.")
        return

    sondages = _get_sondages_actifs()
    if not sondages:
        await update.message.reply_text("Aucun sondage disponible.")
        return

    await update.message.reply_text(
        "Statistiques — choisis un sondage :",
        reply_markup=_kb_liste(sondages, "st_")
    )


async def _stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if update.effective_user.id not in ADMIN_IDS:
        return

    sid   = int(query.data.split("_")[1])
    data  = _get_stats(sid)
    total = data["total"]

    lignes = []
    for opt in data["options"]:
        nb  = opt["nb"]
        pct = round((nb / total * 100) if total > 0 else 0)
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        lignes.append(
            f"{opt['label']}\n{bar} {pct}%  ({nb} reponse{'s' if nb != 1 else ''})"
        )

    await query.message.reply_text(
        f"Sondage #{sid}\n{data['question']}\n\n"
        + "\n\n".join(lignes)
        + f"\n\nTotal : {total} reponse{'s' if total != 1 else ''}"
    )


# ════════════════════════════════════════════════════════════════════════════
# ── POINT D'ENTRÉE ────────────────────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════

def register_sondage_handlers(app: Application):
    app.add_handler(CallbackQueryHandler(_vote_callback,  pattern=r"^vote_\d+_\d+$"))
    app.add_handler(CallbackQueryHandler(_stats_callback, pattern=r"^st_\d+$"))
    app.add_handler(CommandHandler("stats_sondage", _stats_start))

    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("ajout_sondage", _ajout_start)],
        states={
            S_QUESTION:     [MessageHandler(filters.TEXT & ~filters.COMMAND, _ajout_question)],
            S_NB_OPTIONS:   [MessageHandler(filters.TEXT & ~filters.COMMAND, _ajout_nb)],
            S_OPTION_LABEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, _ajout_label)],
            S_OPTION_MSG:   [MessageHandler(filters.TEXT & ~filters.COMMAND, _ajout_msg)],
            S_CONFIRMER:    [MessageHandler(filters.TEXT & ~filters.COMMAND, _ajout_confirmer)],
        },
        fallbacks=[CommandHandler("cancel", _ajout_cancel)],
        per_chat=False, per_user=True, allow_reentry=True,
    ))

    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("envoyer_sondage", _envoyer_start)],
        states={
            E_CHOIX_SONDAGE: [CallbackQueryHandler(_envoyer_choix_sondage, pattern=r"^qs_\d+$")],
            E_CHOIX_CIBLE:   [CallbackQueryHandler(_envoyer_choix_cible,   pattern=r"^cible_")],
            E_USER_ID:       [MessageHandler(filters.TEXT & ~filters.COMMAND, _envoyer_user_id)],
        },
        fallbacks=[CommandHandler("cancel", _envoyer_cancel)],
        per_chat=False, per_user=True, allow_reentry=True,
    ))