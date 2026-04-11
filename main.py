import os
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ChatJoinRequestHandler, CallbackQueryHandler, Application,
    CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
)
from database.database import init_db, upsert_user, log_member, get_file_id, save_file_id

TOKEN = "8609131464:AAGK5k1jkLJvY1OSvHcR3YPnwqEqOFeWuAs"

# ── Étapes ──────────────────────────────────────────────────────────────────
PRENOM, LEVEL, OBJECTIF, WHATSAPP, EMAIL, CONFIRMATION = range(6)


# ── Keyboards ────────────────────────────────────────────────────────────────

def kb_level():
    return InlineKeyboardMarkup([
    [InlineKeyboardButton("1️⃣ Débutant – je découvre encore", callback_data="level_1")],
    [InlineKeyboardButton("2️⃣ Intermédiaire – j’ai les bases", callback_data="level_2")],
    [InlineKeyboardButton("3️⃣ Avancé – je suis rentable", callback_data="level_3")],
])

def kb_objectif():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📚 Apprendre une méthode simple", callback_data="obj_methode")],
        [InlineKeyboardButton("🎥 Voir une démonstration en direct", callback_data="obj_demo")],
        [InlineKeyboardButton("💪 Gagner en confiance", callback_data="obj_confiance")],
        [InlineKeyboardButton("✍️ Autre", callback_data="obj_autre")],
    ])

def kb_confirmation():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🚀 Confirmer mon inscription !", callback_data="confirme")
    ]])


# ── Envoi vidéo de bienvenue (sans bouton) ───────────────────────────────────

async def send_welcome_video(bot, user_id: int):
    log_member(user_id)

    video_name = "welcome"
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
        video_path = "../video/welcome.mp4"
        msg = await bot.send_video(
            chat_id=user_id,
            video=open(video_path, "rb"),
            caption=caption,
            parse_mode="Markdown",
        )
        save_file_id(video_name, msg.video.file_id)

    # Message séparé avec l'instruction
    await bot.send_message(
        chat_id=user_id,
        text=(
            "Clique ici pour confirmer ta place 👇\n\n"
            "/JeMEnregistre"
        )
    )


# ── Approbation demande d'adhésion ───────────────────────────────────────────

async def approve_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.chat_join_request.from_user
    user_id = user.id


    await update.chat_join_request.approve()

    try:
        await send_welcome_video(context.bot, user_id)
    except Exception as e:
        print(f"Erreur envoi message à {user_id} : {e}")


# ── Commande /start ───────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        await send_welcome_video(context.bot, user_id)
    except Exception as e:
        print(f"❌ Erreur /start pour {user_id} : {e}")


# ── Conversation : /JeMEnregistre ────────────────────────────────────────────

async def je_me_enregistre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/JeMEnregistre → démarre le formulaire"""
    await update.message.reply_text(
    "Super ! 🎉\n\n"
    "Moi c’est Charbel Yayi 👋 "
    "et toi ? \n\n",
    parse_mode="Markdown"
    )
    return PRENOM


async def get_prenom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    prenom = update.message.text.strip()
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
        "level_1": "Débutant – je découvre à peine",
        "level_2": "Intermédiaire – bases mais pas encore rentable",
        "level_3": "Avancé – déjà rentable",
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
    }
    objectif = obj_map.get(query.data, "Non précisé")
    context.user_data["objectif"] = objectif
    upsert_user(user_id, objectif=objectif)

    await query.message.reply_text(
        "📱 *Quel est ton numéro(ex : +229 60619292) WhatsApp ?*\n\n",
        parse_mode="Markdown"
    )
    return WHATSAPP


async def get_whatsapp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    whatsapp = update.message.text.strip()
    context.user_data["whatsapp"] = whatsapp
    upsert_user(user_id, whatsapp=whatsapp)

    await update.message.reply_text(
        "📧 *Laisse moi aussi ton address mail :*\n\n",
        parse_mode="Markdown"
    )
    return EMAIL


async def get_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    email = update.message.text.strip()
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
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    prenom = context.user_data.get("prenom", "")
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

    app = Application.builder().token(TOKEN).read_timeout(30).write_timeout(30).build()

    # Demandes d'adhésion au canal
    app.add_handler(ChatJoinRequestHandler(approve_join_request))

    # /start → vidéo de bienvenue + instruction /JeMEnregistre
    app.add_handler(CommandHandler("start", start))

    # Conversation déclenchée par /JeMEnregistre
    conv = ConversationHandler(
        entry_points=[CommandHandler("JeMEnregistre", je_me_enregistre)],
        states={
            PRENOM:       [MessageHandler(filters.TEXT & ~filters.COMMAND, get_prenom)],
            LEVEL:        [CallbackQueryHandler(get_level, pattern="^level_")],
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
    app.add_handler(conv)

    print("start......................go")
    app.run_polling(poll_interval=1, allowed_updates=Update.ALL_TYPES)