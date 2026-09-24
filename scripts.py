
from dotenv import load_dotenv

load_dotenv()      # DOIT rester avant l'import de client (qui lit TOKEN dès son chargement)

from telegram import Update
from telegram.ext import (
    Application, CallbackQueryHandler, ChatJoinRequestHandler, CommandHandler,
    ConversationHandler, MessageHandler, filters,
)

from database import database as db
from sondage import init_sondage_db, register_sondage_handlers   # module sondage : inchangé

from client import (
    TOKEN, approuver_demande, boutons, cmd_inscription, cmd_start, message_libre,
)
from admin import (
    CHOIX_CIBLE, PLANNING, RECEVOIR_MESSAGE,
    annuler, choisir_cible, cmd_aide, cmd_categorie, cmd_codes, cmd_envoyer,
    cmd_envoyer_rappel, cmd_export, cmd_modifier, cmd_rappels, cmd_stats,
    cmd_tester_rappel, demarrer_planificateur, error_handler, recevoir_message,
)


def main():
    if not TOKEN:
        raise SystemExit("❌ Définis TOKEN dans le fichier .env ou en variable d'environnement "
                         "(token régénéré via @BotFather).")

    db.init_db()
    db.seed_rappels(PLANNING)
    init_sondage_db()

    app = (Application.builder().token(TOKEN).read_timeout(30).write_timeout(30)
           .post_init(demarrer_planificateur).build())

    # ── Côté client
    app.add_handler(ChatJoinRequestHandler(approuver_demande))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("JeMEnregistre", cmd_inscription))

    # ── Côté admin
    for nom, fonction in [("aide", cmd_aide), ("stats", cmd_stats), ("rappels", cmd_rappels),
                          ("modifier", cmd_modifier), ("tester_rappel", cmd_tester_rappel),
                          ("envoyer_rappel", cmd_envoyer_rappel), ("codes", cmd_codes),
                          ("nouvelle_categorie", cmd_categorie)]:
        app.add_handler(CommandHandler(nom, fonction))

    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("envoyer", cmd_envoyer), CommandHandler("export", cmd_export)],
        states={
            CHOIX_CIBLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, choisir_cible)],
            RECEVOIR_MESSAGE: [MessageHandler(filters.ALL & ~filters.COMMAND, recevoir_message)],
        },
        fallbacks=[CommandHandler("cancel", annuler)],
        per_chat=False, per_user=True, allow_reentry=True,
    ))

    # ── Boutons, erreurs, sondage
    app.add_handler(CallbackQueryHandler(boutons, pattern=r"^(q|prenom|live|agenda|demarrer|souci)(:|$)"))
    app.add_error_handler(error_handler)

    register_sondage_handlers(app)

    # En dernier : capte tout texte hors commande (codes du soir, questionnaire, messages libres)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_libre))

    print("start...")
    app.run_polling(poll_interval=1, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()