import os 
from telegram import Update
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ChatJoinRequestHandler,CallbackQueryHandler, Application, CommandHandler, MessageHandler, filters, ContextTypes, PollAnswerHandler,ConversationHandler


from database.database import init_db, save_user_default, get_file_id, save_file_id

chanel =""
token = os.getenv("token")

LEVEL_WELCOME, WHY_WELCOME, NUMERO_WHATSAPP_WELCOME, MAIL_WELCOME, NOM_WELCOME = range(5)

def build_answer_keyboards():
    keyboard = [
        [
            InlineKeyboardButton("✅ Je m'enregistre ", callback_data="enregistre")
               
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

async def approve_join_request(update: Update, Context: ContextTypes.DEFAULT_TYPE):
    user = update.chat_join_request.from_user
    user_id = user.id
    user_name = update.effective_user.first_name or " CONFERENCE 1"

    chat_id = update.chat_join_request.chat.id

    save_user_default(user_id)

    args = Context.args
    print("chat_id")
    print(chat_id)

        # Envoie un message privé
    try:
        video_name = "welcomes"

        file_id = get_file_id(video_name)

        if file_id:
                # Réutiliser le file_id
            await Context.bot.send_video(chat_id=user_id , video=file_id, reply_markup= build_answer_keyboards())
                                
        else:
                # Envoyer depuis fichier local, puis sauvegarder le file_id
                video_path = "welcomes.mp4"
                msg = await Context.bot.send_video(chat_id=user_id , video=video_path, reply_markup= build_answer_keyboards())
                new_file_id = msg.video.file_id
                save_file_id(video_name, new_file_id)
            
            #await Context.bot.send_message(
            #chat_id=user_id,
            #text="🔥🔥✍️  Clique sur /JeMEnregistre Maintenant"
            #)

            
    except Exception as e:
            print(f"Impossible d’envoyer un message à {user_id} : {e}")
 
async def get_level_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id


    await query.message.reply_text(
    "📊 J’ai besoin de connaître ton niveau actuel en trading.\n\n"
    "Dis-moi où tu te situes aujourd’hui :\n\n"
    "1️⃣ DÉBUTANT – JE DÉCOUVRE À PEINE LE TRADING\n\n"
    "2️⃣ INTERMÉDIAIRE – J’AI DES BASES, MAIS JE NE SUIS PAS ENCORE RENTABLE\n\n"
    "3️⃣ AVANCÉ – JE SUIS DÉJÀ RENTABLE ET JE CHERCHE À ALLER PLUS LOIN\n\n"

    "✍️ Réponds maintenant par **1**, **2** ou **3**.\n\n"
    "Peu importe où tu démarres… c’est la suite qui compte 🔥",
    parse_mode='Markdown'
)


    return LEVEL_WELCOME


if __name__ == '__main__':

    init_db()

    app = Application.builder().token(token).read_timeout(30).write_timeout(30).build()

    app.add_handler(ChatJoinRequestHandler(approve_join_request))

    conv_handler_welcome = ConversationHandler(
    entry_points=[CallbackQueryHandler(get_level_welcome, pattern='^(enregistre)$')],
    
    states={
        LEVEL_WELCOME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_why_welcome)],
        WHY_WELCOME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_numero_whatsapp_welcome)],
        NUMERO_WHATSAPP_WELCOME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_mail_welcome)],
        MAIL_WELCOME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name_welcome)],
        NOM_WELCOME: [MessageHandler(filters.TEXT & ~filters.COMMAND, last_step_welcome)],
        


    }, fallbacks=[CommandHandler('cancel', cancel)])

    app.add_handler(conv_handler_welcome)
    