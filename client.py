"""
Bot Trading Pour Tous — CÔTÉ CLIENT.
Tout ce que vit l'utilisateur : arrivée dans le canal, questionnaire, confirmation de place,
calendrier, boutons, messages libres et codes du soir.

Pour les commandes admin et les rappels planifiés : voir admin.py
Pour le lancement du bot : voir main.py  (python main.py)
Pour le nettoyage du chat à la fin de l'inscription : voir nettoyage.py
"""
import asyncio
import functools
import html
import os
import random
import re
import unicodedata
from datetime import datetime, timedelta
from urllib.parse import urlencode

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from database import database as db
from nettoyage import PARCOURS, nettoyer, noter

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
# Pour récupérer un file_id : envoie le sticker à @RawDataBot et copie sticker.file_id.
# Une valeur vide "" désactive simplement l'envoi à cet endroit (aucune erreur).
STICKERS = {
    "bienvenue": "",            # à l'arrivée dans le canal, juste après la vidéo d'accueil
    "debut_questionnaire": "",  # sticker « en cours » (sablier, chargement...) sous « Confirmation en cours »
    "presence": "",             # juste avant la question 5 : un sticker qui illustre le direct (caméra, ON AIR...)
    "confirmation": "",         # juste après « Félicitations, ta place est confirmée »
}

FREINS = ["Le manque de temps", "La peur de perdre de l'argent",
          "Je ne sais pas par où commencer", "Autre"]

TOLERANCE = timedelta(minutes=20)       # utilisé par admin.py : au-delà, un rappel en retard n'est plus envoyé
PAUSE_ENVOI = 0.1                       # secondes entre deux envois (limite Telegram)
A_COMPLETER = "À COMPLÉTER"             # un rappel contenant ce mot n'est jamais envoyé (admin.py)
DATE_LANCEMENT_RELANCES = "2026-09-24 00:00:00"   # les relances automatiques ne concernent QUE les personnes
                                                    # créées à partir de cette date (pas les anciens réinvités)
HEURE_RAPPORT = 21                      # heure du Bénin à laquelle le bilan quotidien (+ erreurs du jour) est envoyé
DELAI_VALIDATION_MIN = 2                # secondes minimum entre « je valide ta place » et les félicitations
DELAI_VALIDATION_MAX = 4                # secondes maximum (durée tirée au hasard entre les deux, à chaque fois)

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
    """Envoie un message texte et retourne l'objet Message (pour le mémoriser avec noter())."""
    return await bot.send_message(chat_id=uid, text=texte, parse_mode="HTML", reply_markup=markup)


def noter_id(uid, message_id):
    """Comme noter(), mais à partir d'un simple numéro de message (ex : le texte tapé par la personne)."""
    if message_id:
        PARCOURS.setdefault(uid, []).append(message_id)


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
# CONFIRMATION : « je valide ta place » → (5 s) → chat nettoyé → félicitations + liens des 2 soirs + calendrier
# ════════════════════════════════════════════════════════════════════════════

def _utc(jour):
    """Début et fin du live en UTC (Bénin = UTC+1)."""
    debut = debut_live(jour) - timedelta(hours=1)
    return debut, debut + timedelta(hours=DUREE_LIVE)


def lien_google_agenda(jour) -> str:
    """Lien qui ouvre directement Google Agenda avec l'événement pré-rempli (Android et iPhone)."""
    debut, fin = _utc(jour)
    return "https://calendar.google.com/calendar/render?" + urlencode({
        "action": "TEMPLATE",
        "text": "Formation gratuite Trading Pour Tous",
        "dates": f"{debut:%Y%m%dT%H%M%SZ}/{fin:%Y%m%dT%H%M%SZ}",
        "details": f"Formation en direct (heure du Bénin).\n\nLien du direct : {JOURS[jour]['live']}",
        "location": "En direct en ligne",
    }, safe="/")


async def envoyer_confirmation(bot, uid):
    """Félicitations avec les liens des DEUX soirs (quel que soit le choix) et un seul bouton calendrier
    (premier soir de la personne) qui ouvre directement le lien."""
    u = db.get_user(uid)
    jour_agenda = jours_choisis(u)[0]

    liens = "\n\n".join(
        f"👉 Jour {j} — {JOURS[j]['semaine']} {JOURS[j]['nom']} :\n{JOURS[j]['live']}" for j in JOURS)
    bouton_agenda = InlineKeyboardMarkup(
        [[InlineKeyboardButton("📅 Ajouter à mon calendrier", url=lien_google_agenda(jour_agenda))]])

    await ecrire(
        bot, uid,
        f"<b>🎉 Félicitations {h(u['prenom'])}, ta place vient d'être confirmée ✅</b>\n\n"
        "Voici les liens qui te redirigent directement vers la formation 100 % gratuite. "
        f"Clique sur le lien du jour concerné, à {HEURE_LIVE}h00 précises, heure du Bénin.\n\n"
        f"{liens}\n\n"
        "📌 <i>NB : copie et garde précieusement ces deux liens dans ton bloc-notes. "
        "Je te les renverrai en rappel, mais c'est à toi de ne pas les perdre.</i>\n\n"
        f"Rendez-vous {dates_texte(list(JOURS))}, {HEURE_LIVE}h00. Sois à l'heure.\n\n"
        "— Coach Charbel",
        bouton_agenda)
    await envoyer_sticker(bot, uid, "confirmation")


async def envoyer_lien_live(bot, uid, jour):
    """Clic sur « Rejoindre le live » (boutons des rappels) : on note le clic (jour + heure), puis on envoie le vrai lien."""
    db.enregistrer_suivi(uid, "live", jour)
    bouton = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Ouvrir le live", url=JOURS[jour]["live"])]])
    await ecrire(bot, uid, f"<b>🔴 Ton lien pour le direct du {JOURS[jour]['nom']}</b>", bouton)


# ════════════════════════════════════════════════════════════════════════════
# QUESTIONNAIRE — piloté par la base : on repart toujours de la 1re question sans réponse
# Rien n'est supprimé pendant le questionnaire (pour ne pas distraire) : tout ce qui est affiché est mémorisé
# (noter / noter_id) et effacé d'un coup à la fin, juste avant les félicitations.
# ════════════════════════════════════════════════════════════════════════════

PRENOMS_A_CONFIRMER = {}        # uid -> prénom proposé, en attente Oui/Non
FINALISATION = set()            # uid dont l'inscription est en cours de validation (anti double envoi)


def prochaine_etape(u) -> str:
    if not u.get("prenom"):     return "prenom"
    if not u.get("whatsapp"):   return "whatsapp"
    if not u.get("deja_trade"): return "trade"
    if u["deja_trade"] == "Non" and not u.get("frein"):     # « qu'est-ce qui t'a empêché » : seulement si jamais tradé
        return "frein"
    if not u.get("presence"):   return "presence"
    if u["presence"] == "Un seul soir" and u.get("veut_j1") is None:
        return "soir"
    return "confirmation"


def restantes(u) -> int:
    """Utilisé par admin.py (relances)."""
    manquantes = sum(1 for c in ("prenom", "whatsapp", "deja_trade", "presence") if not u.get(c))
    if u.get("deja_trade") == "Non" and not u.get("frein"):
        manquantes += 1
    return manquantes + (prochaine_etape(u) == "soir")


def inscrit(u) -> bool:
    return bool(u and u.get("completed") == 1 and u.get("webinaire") == WEBINAIRE)


def _titre(u, etape, emoji) -> str:
    """« Question 3 sur 5 ». Ceux qui ont déjà tradé n'ont pas la question « frein » : 4 questions au lieu de 5."""
    total = 4 if u.get("deja_trade") == "Oui" else 5
    ordre = ["prenom", "whatsapp", "trade"] + (["frein"] if total == 5 else []) + ["presence"]
    return f"<b>{emoji} Question {ordre.index(etape) + 1} sur {total}</b>"


async def message_deja_inscrit(bot, uid):
    u = db.get_user(uid)
    await ecrire(bot, uid,
                 "<b>✅ Tu es déjà inscrit(e)</b>\n\n"
                 f"Rendez-vous {dates_texte(jours_choisis(u))} à {HEURE_LIVE}h00 (heure du Bénin).\n\n"
                 "Ton lien du direct et les rappels arrivent ici, en privé.")


async def demarrer_questionnaire(bot, uid):
    if uid in FINALISATION:                         # validation déjà en cours : on ne relance rien
        return
    u = db.get_user(uid)
    if inscrit(u):                                  # jamais deux fois pour la même personne
        await message_deja_inscrit(bot, uid)
        return

    if not u or u.get("webinaire") != WEBINAIRE:
        # Nouveau webinaire : on garde prénom / WhatsApp / Q3 / Q4 déjà connus,
        # mais la présence est à redonner (les anciennes réponses concernaient septembre).
        db.upsert_user(uid, webinaire=WEBINAIRE, presence=None, veut_j1=None, veut_j2=None,
                       completed=0, relance5_le=None, relance15_le=None, relance30_le=None)
    db.upsert_user(uid, en_cours=1)

    noter(uid, await ecrire(bot, uid, "<b>⏳ Confirmation de ta présence en cours…</b>"))
    noter(uid, await envoyer_sticker(bot, uid, "debut_questionnaire"))

    # ── Vidéo en bulle (désactivée) — pour l'activer : décommenter, mettre le fichier à la racine
    # (carré, 1 minute max) et définir VIDEO_BULLE en haut du fichier.
    # with open(VIDEO_BULLE, "rb") as f:
    #     noter(uid, await bot.send_video_note(chat_id=uid, video_note=f))

    await poser_question(bot, uid)


async def poser_question(bot, uid):
    """Pose la prochaine question (et la mémorise pour l'effacer à la fin).
    Quand tout est répondu : lance la validation automatique, sans nouveau clic."""
    u = db.get_user(uid)
    etape = prochaine_etape(u)

    if etape == "confirmation":
        if uid not in FINALISATION:
            FINALISATION.add(uid)
            lancer(finaliser_inscription(bot, uid))      # en tâche de fond : l'attente de 5 s ne bloque personne
        return

    if etape == "prenom":
        msg = await ecrire(bot, uid, f"{_titre(u, etape, '📝')}\n\n"
                                     "Quel est ton prénom ? 😊\n\n"
                                     "Envoie-moi simplement ton prénom ici.\n"
                                     "Exemple : Charbel")
    elif etape == "whatsapp":
        msg = await ecrire(bot, uid, f"{_titre(u, etape, '📱')}\n\nQuel est ton numéro WhatsApp ?\n\n"
                                     "Avec l'indicatif, par exemple : +229 60619292")
    elif etape == "trade":
        msg = await ecrire(bot, uid, f"{_titre(u, etape, '📈')}\n\nAs-tu déjà fait du trading ?",
                           kb(("🟢 Oui", "q:trade:Oui"), ("🔴 Non", "q:trade:Non")))
    elif etape == "frein":
        msg = await ecrire(bot, uid, f"{_titre(u, etape, '🤔')}\n\nQu'est-ce qui t'a empêché jusqu'ici de te lancer ?",
                           kb(*[(f, f"q:frein:{i}") for i, f in enumerate(FREINS)]))
    elif etape == "presence":
        noter(uid, await envoyer_sticker(bot, uid, "presence"))
        msg = await ecrire(
            bot, uid,
            "<b>🎯 Dernière question !</b>\n\n"
            f"{h(u.get('prenom') or '')}, ta présence est importante.\n\n"
            "Seras-tu là les deux soirs ? ⚠️\n\n"
            "Ça se passera en direct, il n'y aura pas de replay !",
            kb((f"🔥 Oui, je serai là les DEUX soirs à {HEURE_LIVE}h", "q:presence:deux"),
               ("Je pourrai être là un seul soir", "q:presence:un")))
    else:   # "soir"
        msg = await ecrire(bot, uid, "<b>📅 Lequel des deux soirs ?</b>",
                           kb(*[(JOURS[j]["nom"], f"q:soir:{j}") for j in JOURS]))

    noter(uid, msg)


async def finaliser_inscription(bot, uid):
    """« Tout est prêt » → 5 s → inscription enregistrée → chat nettoyé → félicitations."""
    try:
        u = db.get_user(uid)
        noter(uid, await ecrire(bot, uid, f"<b>✅ Tout est prêt, {h(u.get('prenom') or '')}.</b>\n\n"
                                          "Je suis en train de valider ta place…"))
        await asyncio.sleep(random.uniform(DELAI_VALIDATION_MIN, DELAI_VALIDATION_MAX))
        db.upsert_user(uid, completed=1, en_cours=0)
        await nettoyer(bot, uid)                # efface tout le parcours : vidéo, stickers, questions, réponses, relances
        await envoyer_confirmation(bot, uid)
    except Exception as e:
        print(f"Finalisation uid={uid} : {e}")
        await prevenir_admins(bot, f"⚠️ Finalisation de l'inscription en échec (uid={uid}) : {e}")
    finally:
        FINALISATION.discard(uid)


async def repondre_texte(bot, uid, texte, message_id) -> bool:
    """Texte reçu = réponse au questionnaire (prénom / WhatsApp) ? Retourne False si ce n'est pas le cas."""
    u = db.get_user(uid)
    if not u or not u.get("en_cours"):
        return False
    etape = prochaine_etape(u)
    if etape not in ("prenom", "whatsapp"):
        return False

    noter_id(uid, message_id)               # le texte tapé par la personne sera effacé à la fin

    if etape == "prenom":
        prenom, a_confirmer = _extraire_prenom(texte)
        if a_confirmer:
            if len(prenom) > 15:
                noter(uid, await ecrire(bot, uid, "⚠️ J'ai du mal à identifier ton prénom dans ce message.\n\n"
                                                  "Peux-tu m'envoyer <b>uniquement ton prénom</b> ?"))
                return True
            PRENOMS_A_CONFIRMER[uid] = prenom
            noter(uid, await ecrire(bot, uid, f"Ton prénom est bien <b>{h(prenom)}</b> ?",
                                    kb((f"✅ Oui, c'est bien {prenom}", "prenom:oui"),
                                       ("✏️ Non, je corrige", "prenom:non"))))
            return True
        db.upsert_user(uid, prenom=prenom)

    else:   # whatsapp
        numero = _valider_whatsapp(texte)
        if not numero:
            noter(uid, await ecrire(bot, uid, "⚠️ Ce numéro ne semble pas valide.\n\n"
                                              "Envoie-le avec l'indicatif, par exemple :\n<b>+229 60619292</b>"))
            return True
        db.upsert_user(uid, whatsapp=numero)

    await poser_question(bot, uid)
    return True


async def confirmer_prenom(bot, uid, reponse) -> bool:
    u = db.get_user(uid)
    if not u or prochaine_etape(u) != "prenom":
        return False
    prenom = PRENOMS_A_CONFIRMER.pop(uid, None)

    if reponse == "oui" and prenom:
        db.upsert_user(uid, prenom=prenom)
        await poser_question(bot, uid)
    else:
        noter(uid, await ecrire(bot, uid, "Pas de souci.\n\nEnvoie-moi juste ton prénom :"))
    return True


async def repondre_bouton(bot, uid, data) -> bool:
    """data = 'trade:Oui', 'frein:2', 'presence:deux', 'soir:1'.
    N'est accepté que si ça correspond à la question en cours (anti double-clic, anciens boutons).
    Après la dernière réponse, poser_question() lance la validation automatique."""
    u = db.get_user(uid)
    champ, _, valeur = data.partition(":")
    if not u or inscrit(u) or prochaine_etape(u) != champ:
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

    if action == "q":
        await repondre_bouton(bot, uid, reste)
    elif action == "prenom":
        await confirmer_prenom(bot, uid, reste)
    elif action == "live":
        await envoyer_lien_live(bot, uid, int(reste))
    elif action == "demarrer":
        await demarrer_questionnaire(bot, uid)
    elif action == "souci":
        await gerer_souci(bot, uid)


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