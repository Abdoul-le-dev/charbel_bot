import os
import asyncio
import sqlite3
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
from telegram.ext import (
    ChatJoinRequestHandler, CallbackQueryHandler, Application,
    CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
)
from database.database import init_db, upsert_user, log_member, get_file_id, save_file_id

TOKEN = "8609131464:AAGK5k1jkLJvY1OSvHcR3YPnwqEqOFeWuAs"

ADMIN_IDS = {6992809421 , 6799962131  }   # ← ajoute ici tous les admins

# ── Étapes inscription ───────────────────────────────────────────────────────
PRENOM, LEVEL, OBJECTIF, WHATSAPP, EMAIL, CONFIRMATION = range(6)

# ── Étapes broadcast ─────────────────────────────────────────────────────────
BC_FORMAT, BC_MEDIA, BC_TEXT = range(6, 9)

PLACES_RESTANTES = 47
PLACES_TOTALES = 150


# ── Keyboards inscription ─────────────────────────────────────────────────────

def kb_level():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1️⃣ Je n'ai jamais tradé", callback_data="level_1")],
        [InlineKeyboardButton("2️⃣ J'ai débuté mais sans résultats", callback_data="level_2")],
        [InlineKeyboardButton("3️⃣ Je trade avec des résultats irréguliers", callback_data="level_3")],
        [InlineKeyboardButton("4️⃣ Je suis déjà rentable", callback_data="level_4")],
    ])

def kb_objectif():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📚 Apprendre une méthode simple et rapide", callback_data="obj_methode")],
        [InlineKeyboardButton("🎥 Voir une démonstration en direct", callback_data="obj_demo")],
        [InlineKeyboardButton("💪 Gagner en confiance", callback_data="obj_confiance")],
        [InlineKeyboardButton("✍️ Autre", callback_data="obj_autre")],
    ])

def kb_confirmation():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🚀 Confirmer mon inscription !", callback_data="confirme")
    ]])


# ── Envoi vidéo de bienvenue ─────────────────────────────────────────────────

async def send_welcome_video(bot, user_id: int):
    log_member(user_id)

    video_name = "welcomes_1"
    file_id = get_file_id(video_name)

    caption = (
        "🚀 *Bienvenue !*\n\n"
        "Tu es sur le point de réserver ta place à la masterclass gratuite :\n"
        "*« Capturer les meilleures impulsions d'une tendance en 5 minutes »*\n\n"
    )

    if file_id:
        await bot.send_video(
            chat_id=user_id,
            video=file_id,
            caption=caption,
            parse_mode="Markdown",
        )
    else:
        video_path = "video/welcome.mp4"
        msg = await bot.send_video(
            chat_id=user_id,
            video=open(video_path, "rb"),
            caption=caption,
            parse_mode="Markdown",
        )
        save_file_id(video_name, msg.video.file_id)

    await bot.send_message(
        chat_id=user_id,
        text=(
            f"🎁 *Cette masterclass est 100% GRATUITE.*\n\n"
            f"Justement parce qu'elle est gratuite, nous sommes *très sélectifs* "
            f"sur les participants — nous voulons des personnes vraiment motivées.\n\n"
            f"⚠️ *Il ne reste que {PLACES_RESTANTES} places sur {PLACES_TOTALES} !*\n"
            f"Les places s'envolent vite. Sécurise la tienne maintenant avant qu'il ne soit trop tard.\n\n"
            "👇 Clique ici pour réserver ta place :\n\n"
            "/JeMEnregistre"
        ),
        parse_mode="Markdown"
    )


async def _send_welcome_safe(bot, user_id: int):
    try:
        await send_welcome_video(bot, user_id)
    except Exception as e:
        print(f"Erreur envoi message à {user_id} : {e}")


# ── Approbation demande d'adhésion ───────────────────────────────────────────

async def approve_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.chat_join_request.from_user.id
    await update.chat_join_request.approve()
    asyncio.create_task(_send_welcome_safe(context.bot, user_id))


# ── Commande /start ───────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    asyncio.create_task(_send_welcome_safe(context.bot, user_id))


# ════════════════════════════════════════════════════════════════════════════
# ── BROADCAST — /envoyer ─────────────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════

def _get_all_user_ids() -> list[int]:
    """Récupère tous les telegram_id enregistrés dans la base."""
    conn = sqlite3.connect("preinscriptions.db")
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id FROM users WHERE telegram_id IS NOT NULL")
    rows = cursor.fetchall()
    conn.close()
    return [row[0] for row in rows]


async def _broadcast_all(bot, admin_id: int, data: dict):
    """
    Envoie le message à tous les utilisateurs.
    data = {
        "format":        "1"|"2"|"3"|"4"|"5",
        "text_content":  str (optionnel selon format),
        "media_file_id": str (optionnel selon format),
    }
    Formats :
        1 → Texte seul
        2 → Image + texte
        3 → Vidéo + texte
        4 → Image seule
        5 → Vidéo seule
    """
    user_ids   = _get_all_user_ids()
    total      = len(user_ids)
    fmt        = data.get("format")
    texte      = data.get("text_content", "")
    media_id   = data.get("media_file_id")

    if total == 0:
        await bot.send_message(admin_id, "❌ Aucun utilisateur enregistré.")
        return

    if fmt in {"2", "3", "4", "5"} and not media_id:
        await bot.send_message(admin_id, "❌ Fichier média manquant. Diffusion annulée.")
        return

    est = round(total * 0.1 / 60, 2)
    await bot.send_message(
        admin_id,
        f"📤 Envoi en cours à *{total}* utilisateurs…\n⏳ Estimé : {est} min",
        parse_mode="Markdown"
    )

    sent = 0
    for idx, uid in enumerate(user_ids, start=1):
        try:
            if fmt == "1":
                await bot.send_message(chat_id=uid, text=texte)
            elif fmt == "2":
                await bot.send_photo(chat_id=uid, photo=media_id, caption=texte)
            elif fmt == "3":
                await bot.send_video(chat_id=uid, video=media_id, caption=texte)
            elif fmt == "4":
                await bot.send_photo(chat_id=uid, photo=media_id)
            elif fmt == "5":
                await bot.send_video(chat_id=uid, video=media_id)
            sent += 1
        except Exception as e:
            print(f"Erreur envoi uid={uid} : {e}")

        # Rapport de progression 1/3 · 2/3 · fin
        if idx == total // 3:
            await bot.send_message(admin_id, "✅ 1/3 des messages envoyés")
        elif idx == (2 * total) // 3:
            await bot.send_message(admin_id, "✅ 2/3 des messages envoyés")
        elif idx == total:
            await bot.send_message(admin_id, f"🎉 Diffusion terminée — *{sent}/{total}* messages envoyés", parse_mode="Markdown")

        await asyncio.sleep(0.1)   # respecte les limites Telegram


# ── Étape 1 : /envoyer → choisir le format ───────────────────────────────────

async def bc_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Commande réservée à l'administrateur.")
        return ConversationHandler.END

    await update.message.reply_text(
        "📤 *Choisis le format du message à diffuser* :\n\n"
        "1 — Texte seul\n"
        "2 — Image + texte\n"
        "3 — Vidéo + texte\n"
        "4 — Image seule\n"
        "5 — Vidéo seule\n\n"
        "_(max 4096 caractères pour le texte)_",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    return BC_FORMAT


# ── Étape 2 : réception du format ────────────────────────────────────────────

async def bc_get_format(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choix = update.message.text.strip()[0]
    if choix not in {"1", "2", "3", "4", "5"}:
        await update.message.reply_text("❌ Choix invalide. Envoie un chiffre entre 1 et 5.")
        return BC_FORMAT

    context.user_data["bc_format"] = choix

    if choix in {"2", "3"}:
        type_media = "image" if choix == "2" else "vidéo"
        await update.message.reply_text(f"📁 Envoie maintenant ton fichier {type_media}.")
        return BC_MEDIA

    # Formats sans média séparé (1, 4, 5)
    if choix == "1":
        await update.message.reply_text("✏️ Envoie maintenant ton texte.")
    else:
        await update.message.reply_text("📁 Envoie maintenant ton fichier (image ou vidéo).")
    return BC_TEXT


# ── Étape 3 : réception du média (formats 2 & 3) ─────────────────────────────

async def bc_get_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choix = context.user_data["bc_format"]

    if choix == "2":
        if not update.message.photo:
            await update.message.reply_text("❌ Ce n'est pas une image. Réessaie.")
            return BC_MEDIA
        context.user_data["bc_media_id"] = update.message.photo[-1].file_id

    elif choix == "3":
        if not update.message.video:
            await update.message.reply_text("❌ Ce n'est pas une vidéo. Réessaie.")
            return BC_MEDIA
        context.user_data["bc_media_id"] = update.message.video.file_id

    await update.message.reply_text("✏️ Envoie maintenant le texte associé.")
    return BC_TEXT


# ── Étape 4 : réception du contenu final → lancement broadcast ───────────────

async def bc_get_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id
    choix    = context.user_data["bc_format"]

    # Formats image/vidéo seuls (4 & 5) → le "texte" reçu est en fait un fichier
    if choix == "4":
        if not update.message.photo:
            await update.message.reply_text("❌ Ce n'est pas une image. Réessaie.")
            return BC_TEXT
        context.user_data["bc_media_id"] = update.message.photo[-1].file_id
        context.user_data["bc_text"]     = ""

    elif choix == "5":
        if not update.message.video:
            await update.message.reply_text("❌ Ce n'est pas une vidéo. Réessaie.")
            return BC_TEXT
        context.user_data["bc_media_id"] = update.message.video.file_id
        context.user_data["bc_text"]     = ""

    else:
        # Formats 1, 2, 3 → on attend du texte
        if not update.message.text:
            await update.message.reply_text("❌ Merci d'envoyer du texte.")
            return BC_TEXT
        context.user_data["bc_text"] = update.message.text

    await update.message.reply_text("✅ Message reçu ! Diffusion lancée en arrière-plan…")

    # Lance le broadcast en tâche parallèle → le handler est libéré immédiatement
    asyncio.create_task(_broadcast_all(
        context.bot,
        admin_id,
        {
            "format":        choix,
            "text_content":  context.user_data.get("bc_text", ""),
            "media_file_id": context.user_data.get("bc_media_id"),
        }
    ))

    return ConversationHandler.END


async def bc_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Diffusion annulée.")
    return ConversationHandler.END


# ── /stats — nombre d'inscrits ────────────────────────────────────────────────

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Commande réservée à l'administrateur.")
        return

    conn = sqlite3.connect("preinscriptions.db")
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users")
    total = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM users WHERE completed = 1")
    complets = cursor.fetchone()[0]
    conn.close()

    places_prises = PLACES_TOTALES - PLACES_RESTANTES

    await update.message.reply_text(
        f"📊 *Statistiques d'inscription :*\n\n"
        f"👥 Utilisateurs ayant démarré : *{total}*\n"
        f"✅ Inscriptions complètes : *{complets}*\n"
        f"⏳ Inscriptions en cours : *{total - complets}*\n\n",
        parse_mode="Markdown"
    )


# ── Conversation : /JeMEnregistre ────────────────────────────────────────────

async def je_me_enregistre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Super ! 🎉\n\n"
        "Moi c'est Charbel Yayi 👋 "
        "et toi ?\n\n",
        parse_mode="Markdown"
    )
    return PRENOM


async def get_prenom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    prenom  = update.message.text.strip()
    context.user_data["prenom"] = prenom
    upsert_user(user_id, prenom=prenom)

    await update.message.reply_text(
        f"Enchanté *{prenom}* ! 👋\n\n"
        "📊 *Quelle est ton expérience actuelle en trading ?*",
        parse_mode="Markdown",
        reply_markup=kb_level()
    )
    return LEVEL


async def get_level(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    level_map = {
        "level_1": "Je n'ai jamais tradé",
        "level_2": "J'ai débuté mais sans résultats",
        "level_3": "Je trade avec des résultats irréguliers",
        "level_4": "Je suis déjà rentable",
    }
    level = level_map.get(query.data, "Non précisé")
    context.user_data["level"] = level
    upsert_user(user_id, level=level)

    await query.message.reply_text(
        "🎯 *Quel est ton principal objectif en rejoignant cette masterclass ?*",
        parse_mode="Markdown",
        reply_markup=kb_objectif()
    )
    return OBJECTIF


async def get_objectif(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    obj_map = {
        "obj_methode":   "Apprendre une méthode simple et rapide",
        "obj_demo":      "Voir une démonstration en direct",
        "obj_confiance": "Gagner en confiance",
        "obj_autre":     "Autre",
    }
    objectif = obj_map.get(query.data, "Non précisé")
    context.user_data["objectif"] = objectif
    upsert_user(user_id, objectif=objectif)

    await query.message.reply_text(
        "📱 *Quel est ton numéro WhatsApp ?*\n"
        "Je t'enverrai les rappels pour la masterclass afin que tu sois avec nous 😊\n\n"
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
        "📧 *Laisse-moi aussi ton adresse mail :*\n\n",
        parse_mode="Markdown"
    )
    return EMAIL


async def get_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    email   = update.message.text.strip()
    context.user_data["email"] = email
    upsert_user(user_id, email=email)

    prenom   = context.user_data.get("prenom", "")
    level    = context.user_data.get("level", "")
    objectif = context.user_data.get("objectif", "")
    whatsapp = context.user_data.get("whatsapp", "")

    await update.message.reply_text(
        f"✅ *Récapitulatif de ton inscription :*\n\n"
        f"👤 Prénom : *{prenom}*\n"
        f"📊 Niveau : *{level}*\n"
        f"🎯 Objectif : *{objectif}*\n"
        f"📱 WhatsApp : *{whatsapp}*\n"
        f"📧 Email : *{email}*\n\n"
        "En confirmant, tu acceptes de recevoir les rappels par WhatsApp et Telegram.\n"
        "Tu peux te désinscrire à tout moment.\n\n"
        "👇",
        parse_mode="Markdown",
        reply_markup=kb_confirmation()
    )
    return CONFIRMATION


async def confirmer_inscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    prenom  = context.user_data.get("prenom", "")
    upsert_user(user_id, completed=1)

    await query.message.reply_text(
        f"🎉 *Félicitations {prenom} !*\n\n"
        "Tu es officiellement inscrit(e) à la masterclass gratuite :\n"
        "*« Capturer les meilleures impulsions d'une tendance en 5 minutes »*\n\n"
        "📲 Tu recevras le lien du live et les rappels par *WhatsApp et Telegram*.\n\n"
        "Hâte de te voir en ligne ! 🔥",
        parse_mode="Markdown"
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Inscription annulée. Tape /JeMEnregistre pour recommencer.")
    return ConversationHandler.END


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()

    app = (
        Application.builder()
        .token(TOKEN)
        .read_timeout(30)
        .write_timeout(30)
        .build()
    )

    app.add_handler(ChatJoinRequestHandler(approve_join_request))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))

    # Conversation inscription
    conv_inscription = ConversationHandler(
        entry_points=[CommandHandler("JeMEnregistre", je_me_enregistre)],
        states={
            PRENOM:       [MessageHandler(filters.TEXT & ~filters.COMMAND, get_prenom)],
            LEVEL:        [CallbackQueryHandler(get_level,   pattern="^level_")],
            OBJECTIF:     [CallbackQueryHandler(get_objectif, pattern="^obj_")],
            WHATSAPP:     [MessageHandler(filters.TEXT & ~filters.COMMAND, get_whatsapp)],
            EMAIL:        [MessageHandler(filters.TEXT & ~filters.COMMAND, get_email)],
            CONFIRMATION: [CallbackQueryHandler(confirmer_inscription, pattern="^confirme$")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=False,
        per_user=True,
        allow_reentry=True,
    )

    # Conversation broadcast (admin seulement)
    conv_broadcast = ConversationHandler(
        entry_points=[CommandHandler("envoyer", bc_start)],
        states={
            BC_FORMAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, bc_get_format)],
            BC_MEDIA:  [MessageHandler(filters.PHOTO | filters.VIDEO,    bc_get_media)],
            BC_TEXT:   [MessageHandler(filters.TEXT | filters.PHOTO | filters.VIDEO, bc_get_text)],
        },
        fallbacks=[CommandHandler("cancel", bc_cancel)],
        per_chat=False,
        per_user=True,
        allow_reentry=True,
    )

    app.add_handler(conv_inscription)
    app.add_handler(conv_broadcast)

    print("start......................go")
    app.run_polling(poll_interval=1, allowed_updates=Update.ALL_TYPES)