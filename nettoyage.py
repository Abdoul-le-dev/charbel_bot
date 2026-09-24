    """
Nettoyage du chat à la confirmation.

Chaque message « décoratif » du parcours (vidéo d'accueil, sticker, « Il reste peu de places »,
présentation du questionnaire, relances, messages d'erreur) est mémorisé avec noter().
Quand la personne clique sur « Confirmer ma place », nettoyer() les supprime tous :
il ne reste à l'écran que la confirmation, le calendrier et les liens.

Limite : la mémoire est vidée si le bot redémarre (les anciens messages restent alors en place, sans erreur).
"""
PARCOURS = {}       # uid -> liste des message_id à supprimer


def noter(uid, msg):
    """Mémorise un message à supprimer plus tard. Retourne le message (permet d'écrire noter(uid, await ...))."""
    if msg is not None:
        PARCOURS.setdefault(uid, []).append(msg.message_id)
    return msg


async def nettoyer(bot, uid):
    """Supprime tous les messages mémorisés pour cette personne (déjà supprimé / trop vieux = ignoré)."""
    for message_id in PARCOURS.pop(uid, []):
        try:
            await bot.delete_message(chat_id=uid, message_id=message_id)
        except Exception:
            pass