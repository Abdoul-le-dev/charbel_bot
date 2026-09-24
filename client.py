"""
Bot Trading Pour Tous — CÔTÉ CLIENT.
Tout ce que vit l'utilisateur : arrivée dans le canal, questionnaire, confirmation de place,
calendrier, boutons, messages libres et codes du soir.

Pour les commandes admin et les rappels planifiés : voir admin.py
Pour le lancement du bot : voir main.py  (python main.py)
Pour le nettoyage du chat à la confirmation : voir nettoyage.py
"""
import asyncio
import functools
import html
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from database import database as db
from nettoyage import nettoyer, noter

# ════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ════════════════════════════════════════════════════════════════════════════

TOKEN = os.getenv("TOKEN")          # ne JAMAIS écrire le token dans le code (main.py charge le .env avant cet import)
ADMIN_IDS = {6992809421, 6799962131}
ADMIN_USERNAME = "@Faiseur2Rois"

WEBINAIRE = "2026-09-30"                # identifiant du webinaire : sert à savoir qui est déjà inscrit
HEURE_LIVE = 20                         # heure du Bénin — ⚠️ ton texte de félicitations parlait de 21h, à vérifier
DUREE_LIVE = 2                          # heures (uniquement pour le calendrier)
JOURS = {
    1: {"date": "2026-09-30", "nom": "30 septembre", "semaine": "mercredi",
        "live": "https://youtube.com/live/jEC7hAcVlTQ?feature=share"},
    2: {"date": "2026-10-01", "nom": "1er octobre", "semaine": "jeudi",
        "live": "https://youtube.com/live/TNjFGH64FHQ?feature=share"},
}

VIDEO_BIENVENUE = "video/welcomes.MP4"
VIDEO_RELANCE = "video/welcomes.MP4"                    # à la racine (utilisé par admin.py)
# VIDEO_BULLE = "bulle.mp4"                       # vidéo ronde (voir demarrer_questionnaire)

# Stickers envoyés à des moments clés. Choisis peu d'endroits mais cohérents.
# Pour récupérer un file_id : envoie le sticker au bot (ou à @RawDataBot) et copie sticker.file_id.
# Une valeur vide "" désactive simplement l'envoi à cet endroit.
STICKERS = {
    "bienvenue": "",            # à l'arrivée dans le canal, juste après la vidéo d'accueil
    "debut_questionnaire": "",  # au lancement du questionnaire (motivation)
    "confirmation": "",         # juste après « Félicitations, ta place est confirmée »
    "live": "",                 # avec le rappel du lien du direct (2 min après le calendrier)
}

FREINS = ["Le manque de temps", "La peur de perdre de l'argent",
          "Je ne sais pas par où commencer", "Autre"]

TOLERANCE = timedelta(minutes=20)       # utilisé par admin.py : au-delà, un rappel en retard n'est plus envoyé
PAUSE_ENVOI = 0.1                       # secondes entre deux envois (limite Telegram)
A_COMPLETER = "À COMPLÉTER"             # un rappel contenant ce mot n'est jamais envoyé (admin.py)
DELAI_LIEN_LIVE = 120                   # secondes d'attente après le bloc calendrier avant le rappel du lien
DELAI_SUPPRESSION = 3                   # secondes avant de supprimer un message question/réponse déjà traité

FORMAT = "%Y-%m-%d %H:%M:%S"
h = html.escape


def debut_live(jour) -> datetime:
    """Début du live, heure du Bénin."""
    return datetime.strptime(JOURS[jour]["date"], "%Y-%m-%d") + timedelta(hours=HEURE_LIVE)


SEUILS = {j: debut_live(j).strftime(FORMAT) for j in JOURS}    # clic à partir de là = présent (admin.py)


def jour_courant():
    aujourdhui = db.now_benin().strftime("%Y-%m-%d")
    return next((j for j, v in JOURS.items() if v["date"] == aujourdhui), None)


def dates_texte(jours) -> str:
    return "le " + " et le ".join(JOURS[j]["nom"] for j in jours)


def jours_choisis(u) -> list[int]:
    return [j for j in JOURS if u and u.get(f"veut_j{j}")] or list(JOURS)


def normaliser(texte) -> str:
    """'  Nasdaq ! ' -> 'NASDAQ' (sans accents, sans ponctuation)."""
    texte = unicodedata.normalize("NFKD", texte)
    texte = "".join(c for c in texte if not unicodedata.combining(c))
    return re.sub(r"[^A-Z0-9]", "", texte.upper())


# ════════════════════════════════════════════════════════════════════════════
# OUTILS D'ENVOI
# ════════════════════════════════════════════════════════════════════════════

TACHES = set()          # garde une référence aux tâches de fond


def lancer(coroutine):
    tache = asyncio.create_task(coroutine)
    TACHES.add(tache)
    tache.add_done_callback(TACHES.discard)


def kb(*boutons):
    """Un bouton par ligne. Chaque bouton = (texte, callback_data)."""
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d)] for t, d in boutons])


def kb_demarrer(texte="✅ Je confirme ma place"):
    return kb((texte, "demarrer"))


async def ecrire(bot, uid, texte, markup=None):
    """Envoie un message texte et retourne l'objet Message (pour mémoriser son id, cf. supprimer() / noter())."""
    return await bot.send_message(chat_id=uid, text=texte, parse_mode="HTML", reply_markup=markup)


async def envoyer_sticker(bot, uid, cle):
    """Envoie le sticker configuré pour ce moment du parcours (STICKERS).
    Retourne le message envoyé, ou None si non configuré / en échec."""
    file_id = STICKERS.get(cle)
    if not file_id:
        return None
    try:
        return await bot.send_sticker(chat_id=uid, sticker=file_id)
    except Exception as e:
        print(f"Sticker « {cle} » uid={uid} : {e}")
        return None


async def _supprimer_differe(bot, chat_id, message_id, delai):
    await asyncio.sleep(delai)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass  # déjà supprimé, trop vieux, ou droits insuffisants : sans conséquence


def supprimer(bot, chat_id, message_id, delai=DELAI_SUPPRESSION):
    """Programme la suppression d'un message après un court délai (non bloquant, pour plus de clarté)."""
    if not message_id:
        return
    lancer(_supprimer_differe(bot, chat_id, message_id, delai))


async def envoyer_video(bot, uid, chemin, caption=None, markup=None, nom=None, parse_mode=None):
    """Envoie une vidéo et retourne le message ; le fichier n'est téléversé qu'une fois (file_id gardé en base)."""
    nom = nom or chemin
    file_id = db.get_file_id(nom)
    if file_id:
        return await bot.send_video(chat_id=uid, video=file_id, caption=caption,
                                    parse_mode=parse_mode, reply_markup=markup)
    with open(chemin, "rb") as f:
        msg = await bot.send_video(chat_id=uid, video=f, caption=caption,
                                   parse_mode=parse_mode, reply_markup=markup)
    db.save_file_id(nom, msg.video.file_id)
    return msg


async def prevenir_admins(bot, texte):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(chat_id=admin_id, text=texte)
        except Exception as e:
            print(f"Notif admin {admin_id} : {e}")


def admin_seulement(fonction):
    @functools.wraps(fonction)
    async def enveloppe(update, ctx, *args, **kwargs):
        if update.effective_user.id not in ADMIN_IDS:
            await update.message.reply_text("⛔ Commande réservée à l'administrateur.")
            return ConversationHandler.END
        return await fonction(update, ctx, *args, **kwargs)
    return enveloppe


# ════════════════════════════════════════════════════════════════════════════
# NETTOYAGE DU PRÉNOM (logique d'origine, inchangée)
# ════════════════════════════════════════════════════════════════════════════

def _nettoyer_prenom(texte: str) -> str:
    t = texte.strip()
    t = t.replace("\u2019", "'").replace("\u2018", "'")
    t = t.lower()
    t = "".join(c for c in t if unicodedata.category(c) not in ("So", "Sm", "Sk", "Cn"))

    for ancien, nouveau in [
        ("mappelle", " "), ("cest", " "), ("mest", " "),
        ("jsuis", " "), ("chuis", " "),
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
    """Retourne (prénom, confirmation_requise)."""
    brut = texte.strip()
    if len(brut.split()) <= 2 and len(brut) <= 20:
        return brut.title(), False
    return _nettoyer_prenom(brut), True


def _valider_whatsapp(texte: str) -> str | None:
    """Accepte avec ou sans '+', espaces/tirets tolérés, 8 à 15 chiffres."""
    texte = texte.strip()
    if not re.fullmatch(r"\+?[\d\s.\-()]+", texte):
        return None
    chiffres = re.sub(r"\D", "", texte)
    if not 8 <= len(chiffres) <= 15:
        return None
    return ("+" if texte.startswith("+") else "") + chiffres


# ════════════════════════════════════════════════════════════════════════════
# CONFIRMATION : place confirmée → félicitations (liens en clair) → calendrier → (2 min) → rappel du lien
# ════════════════════════════════════════════════════════════════════════════

def _utc(jour):
    """Début et fin du live en UTC (Bénin = UTC+1)."""
    debut = debut_live(jour) - timedelta(hours=1)
    return debut, debut + timedelta(hours=DUREE_LIVE)


def lien_google_agenda(jour) -> str:
    debut, fin = _utc(jour)
    return "https://calendar.google.com/calendar/render?" + urlencode({
        "action": "TEMPLATE",
        "text": "Formation gratuite Trading Pour Tous",
        "dates": f"{debut:%Y%m%dT%H%M%SZ}/{fin:%Y%m%dT%H%M%SZ}",
        "details": f"Formation en direct (heure du Bénin).\n\nLien du direct : {JOURS[jour]['live']}",
        "location": "En direct en ligne",
    }, safe="/")


def clavier_google_agenda(jours, libelle):
    """Un bouton Google Agenda par soir choisi (un lien Google Agenda ne peut contenir qu'un seul événement)."""
    lignes = []
    for j in jours:
        texte = libelle if len(jours) == 1 else f"{libelle} : {JOURS[j]['nom']}"
        lignes.append([InlineKeyboardButton(texte, url=lien_google_agenda(j))])
    return InlineKeyboardMarkup(lignes)


def fichier_ics(jours) -> bytes:
    """Fichier calendrier pour iPhone : un événement par soir choisi, avec le lien du live dedans."""
    lignes = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Trading Pour Tous//FR", "CALSCALE:GREGORIAN"]
    for j in jours:
        debut, fin = _utc(j)
        lignes += [
            "BEGIN:VEVENT",
            f"UID:tpt-{WEBINAIRE}-j{j}@tradingpourtous",
            f"DTSTAMP:{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}",
            f"DTSTART:{debut:%Y%m%dT%H%M%SZ}",
            f"DTEND:{fin:%Y%m%dT%H%M%SZ}",
            "SUMMARY:Formation gratuite Trading Pour Tous",
            f"DESCRIPTION:Formation en direct (heure du Bénin).\\nLien du direct : {JOURS[j]['live']}",
            "LOCATION:En direct en ligne",
            f"URL:{JOURS[j]['live']}",
            "BEGIN:VALARM", "TRIGGER:-PT30M", "ACTION:DISPLAY",
            "DESCRIPTION:La formation commence dans 30 minutes", "END:VALARM",
            "END:VEVENT",
        ]
    lignes.append("END:VCALENDAR")
    return "\r\n".join(lignes).encode("utf-8")


async def envoyer_confirmation(bot, uid):
    u = db.get_user(uid)
    jours = jours_choisis(u)

    liens = "\n\n".join(
        f"👉 Jour {j} — {JOURS[j]['semaine']} {JOURS[j]['nom']} :\n{JOURS[j]['live']}"
        for j in jours)
    await ecrire(
        bot, uid,
        f"<b>🎉 Félicitations {h(u['prenom'])}, ta place vient d'être confirmée ✅</b>\n\n"
        "Voici les liens qui te redirigent directement vers la formation 100 % gratuite. "
        f"Clique sur le lien du jour concerné, à {HEURE_LIVE}h00 précises, heure du Bénin.\n\n"
        f"{liens}\n\n"
        "📌 <i>NB : copie et garde précieusement ces deux liens dans ton bloc-notes. "
        "Je te les renverrai en rappel, mais c'est à toi de ne pas les perdre.</i>\n\n"
        f"Rendez-vous {JOURS[jours[0]]['semaine']}, {HEURE_LIVE}h00. Sois à l'heure.\n\n"
        "— Coach Charbel")
    await envoyer_sticker(bot, uid, "confirmation")

    await ecrire(bot, uid, "<b>📅 Ajoute la formation à ton calendrier</b>\n\nPour ne pas oublier.",
                 kb(("🔵 Android : Google Agenda", "agenda:android"),
                    ("🍏 iPhone : fichier calendrier", "agenda:iphone")))

    lancer(envoyer_bloc_lien_direct(bot, uid, jours))


async def envoyer_bloc_lien_direct(bot, uid, jours):
    """Envoyé DELAI_LIEN_LIVE secondes après le bloc calendrier : rappel avec liens en boutons cliquables."""
    await asyncio.sleep(DELAI_LIEN_LIVE)
    await envoyer_sticker(bot, uid, "live")
    await ecrire(bot, uid,
                 "<b>🔔 Petit rappel : tes liens en un clic</b>\n\n"
                 "Garde-les précieusement.\n\n"
                 "Je te rappellerai ici même, en privé, la veille et le jour J.\n\n"
                 f"À {JOURS[jours[0]]['semaine']} soir !",
                 kb(*[(f"▶️ Live du {JOURS[j]['nom']}", f"live:{j}") for j in jours]))


async def envoyer_agenda(bot, uid, plateforme):
    """Clic sur un bouton calendrier : on note le clic, puis on envoie les liens Google Agenda (Android)
    ou le .ics + les liens alternatifs (iPhone, plus rapide que de télécharger le fichier)."""
    db.enregistrer_suivi(uid, f"agenda_{plateforme}")
    jours = jours_choisis(db.get_user(uid))

    if plateforme == "android":
        await ecrire(bot, uid, "Clique ci-dessous pour l'ajouter à ton agenda.",
                     clavier_google_agenda(jours, "🔗 Ajouter à Google Agenda"))
    else:
        await bot.send_document(chat_id=uid, document=fichier_ics(jours), filename="formation-trading-pour-tous.ics",
                                caption="🍏 Ouvre ce fichier pour l'ajouter à ton calendrier iPhone.")
        await ecrire(bot, uid, "Ou plus simple : ajoute-le directement via ce lien 👇",
                     clavier_google_agenda(jours, "🔗 Ajouter via un lien (rapide)"))


async def envoyer_lien_live(bot, uid, jour):
    """Clic sur « Rejoindre le live » : on note le clic (jour + heure), puis on envoie le vrai lien."""
    db.enregistrer_suivi(uid, "live", jour)
    bouton = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Ouvrir le live", url=JOURS[jour]["live"])]])
    await ecrire(bot, uid, f"<b>🔴 Ton lien pour le direct du {JOURS[jour]['nom']}</b>", bouton)


# ════════════════════════════════════════════════════════════════════════════
# QUESTIONNAIRE — piloté par la base : on repart toujours de la 1re question sans réponse
# Nettoyage pour plus de clarté :
#   • question + réponse disparaissent (après 3 s) une fois répondu ;
#   • à « Confirmer ma place », tout le reste du parcours est effacé (voir nettoyage.py).
# ════════════════════════════════════════════════════════════════════════════

PRENOMS_A_CONFIRMER = {}        # uid -> (prénom proposé, message_id du texte brut), en attente Oui/Non
DERNIERE_QUESTION = {}          # uid -> message_id de la dernière question envoyée


def prochaine_etape(u) -> str:
    if not u.get("prenom"):     return "prenom"
    if not u.get("whatsapp"):   return "whatsapp"
    if not u.get("deja_trade"): return "trade"
    if not u.get("frein"):      return "frein"
    if not u.get("presence"):   return "presence"
    if u["presence"] == "Un seul soir" and u.get("veut_j1") is None:
        return "soir"
    return "confirmation"


def restantes(u) -> int:
    """Utilisé par admin.py (relances)."""
    manquantes = sum(1 for c in ("prenom", "whatsapp", "deja_trade", "frein", "presence") if not u.get(c))
    return manquantes + (prochaine_etape(u) == "soir")


def inscrit(u) -> bool:
    return bool(u and u.get("completed") == 1 and u.get("webinaire") == WEBINAIRE)


async def message_deja_inscrit(bot, uid):
    u = db.get_user(uid)
    await ecrire(bot, uid,
                 "<b>✅ Tu es déjà inscrit(e)</b>\n\n"
                 f"Rendez-vous {dates_texte(jours_choisis(u))} à {HEURE_LIVE}h00 (heure du Bénin).\n\n"
                 "Ton lien du direct et les rappels arrivent ici, en privé.")


async def demarrer_questionnaire(bot, uid):
    u = db.get_user(uid)
    if inscrit(u):                                  # jamais deux fois pour la même personne
        await message_deja_inscrit(bot, uid)
        return

    if not u or u.get("webinaire") != WEBINAIRE:
        # Nouveau webinaire : on garde prénom / WhatsApp / Q3 / Q4 déjà connus,
        # mais la présence est à redonner (les anciennes réponses concernaient septembre).
        db.upsert_user(uid, webinaire=WEBINAIRE, presence=None, veut_j1=None, veut_j2=None,
                       completed=0, relance10=0, relance30=0)
    db.upsert_user(uid, en_cours=1)

    noter(uid, await ecrire(
        bot, uid,
        "<b>Confirme ta place</b>\nFormation gratuite Trading Pour Tous\n\n"
        f"📅 {JOURS[1]['nom']} et {JOURS[2]['nom']}, {HEURE_LIVE}h00 (heure du Bénin)\n\n"
        "🔴 En direct uniquement, places limitées."))
    noter(uid, await envoyer_sticker(bot, uid, "debut_questionnaire"))

    # ── Vidéo en bulle (désactivée) — pour l'activer : décommenter, mettre le fichier à la racine
    # (carré, 1 minute max) et définir VIDEO_BULLE en haut du fichier.
    # with open(VIDEO_BULLE, "rb") as f:
    #     noter(uid, await bot.send_video_note(chat_id=uid, video_note=f))

    await poser_question(bot, uid)


async def poser_question(bot, uid):
    """Pose la prochaine question et mémorise son message_id pour pouvoir le supprimer une fois répondu."""
    u = db.get_user(uid)
    etape = prochaine_etape(u)

    if etape == "prenom":
        msg = await ecrire(bot, uid, "<b>📝 Question 1 sur 5</b>\n\nQuel est ton prénom ?")
    elif etape == "whatsapp":
        msg = await ecrire(bot, uid, "<b>📱 Question 2 sur 5</b>\n\nQuel est ton numéro WhatsApp ?\n\n"
                               "<i>Avec l'indicatif, par exemple : +229 60619292</i>")
    elif etape == "trade":
        msg = await ecrire(bot, uid, "<b>📈 Question 3 sur 5</b>\n\nAs-tu déjà fait du trading ?",
                     kb(("🟢 Oui", "q:trade:Oui"), ("🔴 Non", "q:trade:Non")))
    elif etape == "frein":
        msg = await ecrire(bot, uid, "<b>🤔 Question 4 sur 5</b>\n\nQu'est-ce qui t'a empêché jusqu'ici de te lancer ?",
                     kb(*[(f" {f}", f"q:frein:{i}") for i, f in enumerate(FREINS)]))
    elif etape == "presence":
        msg = await ecrire(bot, uid, "<b>🎯 Question 5 sur 5</b>\n\nTa présence\n\n"
                               "⚠️ <i>En direct uniquement, pas de replay.</i>",
                     kb((f" Je serai là  les DEUX soirs, à {HEURE_LIVE}h", "q:presence:deux"),
                        ("  Je serai là un seul soir", "q:presence:un")))
    elif etape == "soir":
        msg = await ecrire(bot, uid, "<b>📅 Lequel des deux soirs ?</b>",
                     kb(*[(f" {JOURS[j]['nom']}", f"q:soir:{j}") for j in JOURS]))
    else:
        msg = await ecrire(bot, uid, f"<b>✅ Tout est prêt, {h(u.get('prenom') or '')}.</b>\n\n"
                               "Il ne reste qu'un clic pour verrouiller ta place.",
                     kb((" ✅ Je valide ma place", "q:confirme:1")))

    DERNIERE_QUESTION[uid] = msg.message_id


async def repondre_texte(bot, uid, texte, message_id) -> bool:
    """Texte reçu = réponse au questionnaire (prénom / WhatsApp) ? Retourne False si ce n'est pas le cas."""
    u = db.get_user(uid)
    if not u or not u.get("en_cours"):
        return False
    etape = prochaine_etape(u)

    if etape == "prenom":
        prenom, a_confirmer = _extraire_prenom(texte)
        if a_confirmer:
            if len(prenom) > 15:
                noter(uid, await ecrire(bot, uid, "⚠️ J'ai du mal à identifier ton prénom dans ce message.\n\n"
                                                  "Peux-tu m'envoyer <b>uniquement ton prénom</b> ?"))
                return True
            PRENOMS_A_CONFIRMER[uid] = (prenom, message_id)
            await ecrire(bot, uid, f"Ton prénom est bien <b>{h(prenom)}</b> ?",
                         kb((f"✅ Oui, c'est bien {prenom}", "prenom:oui"), ("✏️ Non, je corrige", "prenom:non")))
            return True
        db.upsert_user(uid, prenom=prenom)
        supprimer(bot, uid, message_id)                         # le texte brut tapé par l'utilisateur

    elif etape == "whatsapp":
        numero = _valider_whatsapp(texte)
        if not numero:
            noter(uid, await ecrire(bot, uid, "⚠️ Ce numéro ne semble pas valide.\n\n"
                                              "Envoie-le avec l'indicatif, par exemple :\n<b>+229 60619292</b>"))
            return True
        db.upsert_user(uid, whatsapp=numero)
        supprimer(bot, uid, message_id)

    else:
        return False

    supprimer(bot, uid, DERNIERE_QUESTION.pop(uid, None))        # la question elle-même
    await poser_question(bot, uid)
    return True


async def confirmer_prenom(bot, uid, reponse) -> bool:
    u = db.get_user(uid)
    if not u or prochaine_etape(u) != "prenom":
        return False
    donnee = PRENOMS_A_CONFIRMER.pop(uid, None)
    prenom, message_brut_id = donnee if donnee else (None, None)

    if reponse == "oui" and prenom:
        db.upsert_user(uid, prenom=prenom)
        supprimer(bot, uid, message_brut_id)                         # prénom brut tapé par l'utilisateur
        supprimer(bot, uid, DERNIERE_QUESTION.pop(uid, None))        # « Quel est ton prénom ? »
        await poser_question(bot, uid)
    else:
        supprimer(bot, uid, message_brut_id)
        supprimer(bot, uid, DERNIERE_QUESTION.pop(uid, None))
        msg = await ecrire(bot, uid, "Pas de souci.\n\nEnvoie-moi juste ton prénom :")
        DERNIERE_QUESTION[uid] = msg.message_id
    return True


async def repondre_bouton(bot, uid, data) -> bool:
    """data = 'trade:Oui', 'frein:2', 'presence:deux', 'soir:1', 'confirme:1'.
    N'est accepté que si ça correspond à la question en cours (anti double-clic, anciens boutons)."""
    u = db.get_user(uid)
    champ, _, valeur = data.partition(":")
    if not u or inscrit(u) or prochaine_etape(u) != ("confirmation" if champ == "confirme" else champ):
        return False

    if champ == "trade":
        db.upsert_user(uid, deja_trade=valeur)
    elif champ == "frein":
        db.upsert_user(uid, frein=FREINS[int(valeur)])
    elif champ == "presence":
        if valeur == "deux":
            db.upsert_user(uid, presence="Les deux soirs", veut_j1=1, veut_j2=1)
        else:
            db.upsert_user(uid, presence="Un seul soir")
    elif champ == "soir":
        db.upsert_user(uid, veut_j1=int(valeur == "1"), veut_j2=int(valeur == "2"))
    elif champ == "confirme":
        db.upsert_user(uid, completed=1, en_cours=0)
        await nettoyer(bot, uid)                # efface tout le parcours : vidéo, stickers, intro, relances...
        await envoyer_confirmation(bot, uid)
        return True

    await poser_question(bot, uid)
    return True


async def gerer_souci(bot, uid):
    """Bouton « Je suis bloqué(e) » : on donne le contact de l'admin et on met le questionnaire en pause
    (plus aucune relance automatique pour cette personne)."""
    db.upsert_user(uid, en_cours=0)
    noter(uid, await ecrire(bot, uid,
                            "<b>🆘 Pas de souci.</b>\n\n"
                            f"Pour t'aider rapidement, contacte directement Charbel : {ADMIN_USERNAME}\n\n"
                            "Quand tu es prêt(e), tu peux reprendre ton inscription.",
                            kb_demarrer("🔄 Reprendre mon inscription")))


# ════════════════════════════════════════════════════════════════════════════
# ARRIVÉE DANS LE CANAL ET /start
# ════════════════════════════════════════════════════════════════════════════

async def envoyer_bienvenue(bot, uid, lien_entree=None):
    db.log_member(uid, lien_entree)
    db.upsert_user(uid, categorie=db.derniere_categorie())

    legende = (
        "<b>Bienvenue dans Trading Pour Tous</b>\n\n"
        "Tu es sur le point de réserver ta place à la formation gratuite.\n\n"
        f"<b>📅 Dates</b>\n{JOURS[1]['nom']} et {JOURS[2]['nom']}, à {HEURE_LIVE}h00 (heure du Bénin)\n\n"
        "<b>🔴 Format</b>\nEn direct uniquement.")
    noter(uid, await envoyer_video(bot, uid, VIDEO_BIENVENUE, legende, nom="welcomes_222", parse_mode="HTML"))
    noter(uid, await envoyer_sticker(bot, uid, "bienvenue"))
    noter(uid, await ecrire(bot, uid, "<b>⚠️ Il reste peu de places.</b>\n\nClique ci-dessous pour confirmer la tienne.",
                            kb_demarrer()))


async def envoyer_bienvenue_securise(bot, uid, lien_entree=None):
    try:
        await envoyer_bienvenue(bot, uid, lien_entree)
    except Exception as e:
        print(f"Erreur bienvenue uid={uid} : {e}")


async def approuver_demande(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    demande = update.chat_join_request
    uid = demande.from_user.id
    try:
        await demande.approve()
    except Exception as e:
        print(f"approve() uid={uid} : {e}")
    lien = demande.invite_link          # nommer les liens A, B, C dans Telegram : le nom est gardé en base
    nom_lien = (lien.name or lien.invite_link) if lien else None
    lancer(envoyer_bienvenue_securise(ctx.bot, uid, nom_lien))


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    db.touch_last_seen(uid)
    if inscrit(db.get_user(uid)):
        await message_deja_inscrit(ctx.bot, uid)
    else:
        lancer(envoyer_bienvenue_securise(ctx.bot, uid))


async def cmd_inscription(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db.touch_last_seen(update.effective_user.id)
    await demarrer_questionnaire(ctx.bot, update.effective_user.id)


# ════════════════════════════════════════════════════════════════════════════
# BOUTONS (un seul routeur)
# ════════════════════════════════════════════════════════════════════════════

async def boutons(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid, bot = q.from_user.id, ctx.bot
    db.touch_last_seen(uid)
    action, _, reste = q.data.partition(":")

    accepte = None
    if action == "q":
        accepte = await repondre_bouton(bot, uid, reste)
    elif action == "prenom":
        accepte = await confirmer_prenom(bot, uid, reste)
    elif action == "live":
        await envoyer_lien_live(bot, uid, int(reste))
    elif action == "agenda":
        await envoyer_agenda(bot, uid, reste)
    elif action == "demarrer":
        await demarrer_questionnaire(bot, uid)
    elif action == "souci":
        await gerer_souci(bot, uid)

    if accepte:              # question déjà répondue : on la supprime (après un court délai), pour plus de clarté
        DERNIERE_QUESTION.pop(uid, None)
        supprimer(bot, q.message.chat_id, q.message.message_id)


# ════════════════════════════════════════════════════════════════════════════
# MESSAGES LIBRES ET CODES DU SOIR
# ════════════════════════════════════════════════════════════════════════════

async def traiter_code(bot, uid, texte, jour_actuel=None) -> bool:
    """Le message est-il l'un des codes du soir ? Si oui on l'enregistre (qui + quelle heure)."""
    trouves = db.trouver_code(normaliser(texte))
    if not trouves:
        return False
    jour_actuel = jour_actuel or jour_courant()
    jour, rang = next(((j, r) for j, r in trouves if j == jour_actuel), trouves[0])
    db.enregistrer_suivi(uid, "code", jour, str(rang))
    prenom = (db.get_user(uid) or {}).get("prenom") or ""
    await ecrire(bot, uid, f"✅ Code reçu, {h(prenom)}. Merci d'être en direct.")
    return True


async def message_libre(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    user, bot = update.effective_user, ctx.bot
    uid, brut = user.id, update.message.text
    db.touch_last_seen(uid)

    if await traiter_code(bot, uid, brut):                                # 1. code du soir
        return
    if await repondre_texte(bot, uid, brut, update.message.message_id):   # 2. réponse au questionnaire
        return

    db.log_message(uid, brut)                        # 3. message libre → on prévient l'admin
    texte = brut.lower().strip()
    if "present" in texte or "présent" in texte:
        u = db.get_user(uid)
        reponse = ("🙌 Je suis très content de savoir que tu seras là !\n\n"
                   f"N'oublie pas : {dates_texte(jours_choisis(u))} à {HEURE_LIVE}h, heure de Cotonou.\n\n"
                   "Je t'enverrai ton lien ici, en privé, juste avant le live.")
    elif "merci" in texte:
        reponse = "jtp !"
    elif "ok" in texte:
        reponse = "Super !"
    else:
        reponse = ("💬 Ton message a bien été reçu.\n\n"
                   f"Pour une réponse rapide, contacte directement Charbel sur Telegram : {ADMIN_USERNAME}\n\n"
                   "Il te répondra dès que possible.")
    await update.message.reply_text(reponse)

    pseudo = f"@{user.username}" if user.username else f"id:{uid}"
    await prevenir_admins(bot, f"Nouveau message reçu\n\nUtilisateur : {user.first_name or ''} ({pseudo})\n"
                               f"ID : {uid}\n\nMessage : {brut[:200]}")