"""
Bot Trading Pour Tous — CÔTÉ CLIENT.
Tout ce que vit l'utilisateur : arrivée dans le canal, questionnaire, confirmation de place,
boutons, messages libres et codes du soir.

Aucune date fixe n'est montrée à l'utilisateur : on parle toujours de « mercredi prochain » et
« jeudi prochain », pour pouvoir réutiliser le même lien à chaque campagne.
Les liens du live ne sont JAMAIS envoyés en privé : ils sont publiés le jour même dans le canal Telegram.

Pour les commandes admin et les rappels planifiés : voir admin.py
Pour le lancement du bot : voir main.py  (python main.py)
Pour le nettoyage du chat à la fin de l'inscription : voir nettoyage.py
"""
import asyncio
import functools
import html
import json
import os
import random
import re
import unicodedata
from datetime import date, datetime, timedelta

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

CANAL_TELEGRAM = "https://t.me/+Gib-RJjjQqgyMWNk"   # le lien du live est publié ici, le jour même

# ── DATES DU WEBINAIRE (usage interne uniquement, jamais affichées aux utilisateurs) ──
# Elles se changent avec la commande admin /dates, sans toucher au code ni redémarrer.
# Elles sont enregistrées dans FICHIER_DATES ; les valeurs ci-dessous ne servent que si ce fichier n'existe pas.
# Elles servent à savoir qui est inscrit à CETTE session, aux rappels planifiés et aux stats (admin.py).
HEURE_LIVE = 21                         # heure du Bénin
DUREE_LIVE = 2                          # heures
FICHIER_DATES = "dates_webinaire.json"
JOURS = {
    1: {"date": "2026-11-04", "nom": "4 novembre", "semaine": "mercredi", "relatif": "mercredi prochain"},
    2: {"date": "2026-11-05", "nom": "5 novembre", "semaine": "jeudi", "relatif": "jeudi prochain"},
}

VIDEO_BIENVENUE = "video/welcomes.MP4"
VIDEO_RELANCE = "video/welcomes.MP4"                    # utilisé par admin.py
# VIDEO_BULLE = "bulle.mp4"                       # vidéo ronde (voir demarrer_questionnaire)

# Stickers envoyés à des moments clés. Choisis peu d'endroits mais cohérents.
# Pour récupérer un file_id : envoie le sticker à @RawDataBot et copie sticker.file_id.
# Une valeur vide "" désactive simplement l'envoi à cet endroit (aucune erreur).
STICKERS = {
    "bienvenue": "",            # à l'arrivée dans le canal, juste après la vidéo d'accueil
    "debut_questionnaire": "",  # sticker « en cours » (sablier, chargement...) sous « Confirmation en cours »
    "presence": "",             # juste avant la question 5 : un sticker qui illustre le direct (caméra, ON AIR...)
    "confirmation": "",         # juste après le message « Bravo, ton inscription est confirmée »
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


SEUILS = {}             # jour -> début du live : un clic/code à partir de là = présent (admin.py)

MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
        "août", "septembre", "octobre", "novembre", "décembre"]
JOURS_SEMAINE = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def nom_date(d: date) -> str:
    """date(2026, 11, 4) -> '4 novembre' ; le 1er du mois -> '1er octobre'."""
    return f"{'1er' if d.day == 1 else d.day} {MOIS[d.month - 1]}"


def appliquer_dates(mercredi: date, sauver=False):
    """Met à jour les deux soirs (mercredi + jeudi) partout dans le bot, en mémoire.
    JOURS et SEUILS sont modifiés sur place : admin.py voit le changement immédiatement."""
    for j, d in ((1, mercredi), (2, mercredi + timedelta(days=1))):
        JOURS[j]["date"] = d.isoformat()
        JOURS[j]["nom"] = nom_date(d)
    SEUILS.clear()
    SEUILS.update({j: debut_live(j).strftime(FORMAT) for j in JOURS})
    if sauver:
        with open(FICHIER_DATES, "w", encoding="utf-8") as f:
            json.dump({"mercredi": mercredi.isoformat()}, f)


def charger_dates():
    mercredi = date.fromisoformat(JOURS[1]["date"])
    try:
        with open(FICHIER_DATES, encoding="utf-8") as f:
            mercredi = date.fromisoformat(json.load(f)["mercredi"])
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"{FICHIER_DATES} illisible ({e}) : dates par défaut utilisées")
    appliquer_dates(mercredi)


def webinaire() -> str:
    """Identifiant de la session en cours (= date du mercredi) : sert à savoir qui est déjà inscrit."""
    return JOURS[1]["date"]


charger_dates()


def jour_courant():
    aujourdhui = db.now_benin().strftime("%Y-%m-%d")
    return next((j for j, v in JOURS.items() if v["date"] == aujourdhui), None)


def dates_texte(jours) -> str:
    """Toujours sans date fixe : « mercredi prochain et jeudi prochain »."""
    return " et ".join(JOURS[j]["relatif"] for j in jours)


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


def kb_canal(texte="📢 Rejoindre le canal Telegram"):
    """Bouton-lien vers le canal (là où le lien du live est publié)."""
    return InlineKeyboardMarkup([[InlineKeyboardButton(texte, url=CANAL_TELEGRAM)]])


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
# CONFIRMATION : « je valide ta place » → (2-4 s) → chat nettoyé → message de confirmation
# Plus de dates fixes, plus de liens du live, plus de bouton calendrier.
# ════════════════════════════════════════════════════════════════════════════

async def envoyer_confirmation(bot, uid):
    u = db.get_user(uid)
    await ecrire(
        bot, uid,
        f"<b>☑ Bravo {h(u.get('prenom') or '')}, ton inscription est confirmée !</b>\n\n"
        "Tu fais partie de la prochaine formation gratuite Trading Pour Tous.\n\n"
        f"📅 Mercredi prochain et jeudi prochain, à {HEURE_LIVE} h (heure du Bénin), "
        f"pendant environ {DUREE_LIVE} heures chaque soir.\n\n"
        "🔔 Mets-toi un rappel sur ton téléphone dès maintenant pour ne pas oublier.\n\n"
        "Reste attentif. Ce que je vais partager avec toi pendant ces deux soirées peut changer ta vie, "
        "et celle des gens autour de toi.\n\n"
        "— Charbel YAYI\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "<b>⚠️ DERNIÈRE ÉTAPE, TRÈS IMPORTANTE</b>\n\n"
        "Toutes les informations, et le lien pour rejoindre le live, seront envoyées uniquement "
        "dans le canal Telegram.\n\n"
        "👉 Clique maintenant sur ce lien et rejoins le canal Telegram :\n\n"
        f"{CANAL_TELEGRAM}\n\n"
        "Si tu ne rejoins pas le canal, tu ne recevras pas le lien de la formation.")
    await envoyer_sticker(bot, uid, "confirmation")


async def envoyer_lien_live(bot, uid, jour):
    """Clic sur un ANCIEN bouton « Rejoindre le live » (déjà présent dans certains chats) :
    on note le clic, puis on renvoie vers le canal — plus aucun lien du live en privé."""
    db.enregistrer_suivi(uid, "live", jour)
    await ecrire(bot, uid,
                 "<b>🔴 Le lien du live est publié dans le canal Telegram, le jour même.</b>\n\n"
                 "Rejoins le canal pour ne pas le manquer 👇", kb_canal())


# ════════════════════════════════════════════════════════════════════════════
# QUESTIONNAIRE — piloté par la base : on repart toujours de la 1re question sans réponse
# Rien n'est supprimé pendant le questionnaire (pour ne pas distraire) : tout ce qui est affiché est mémorisé
# (noter / noter_id) et effacé d'un coup à la fin, juste avant la confirmation.
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
    return bool(u and u.get("completed") == 1 and u.get("webinaire") == webinaire())


def _titre(u, etape, emoji) -> str:
    """« Question 3 sur 5 ». Ceux qui ont déjà tradé n'ont pas la question « frein » : 4 questions au lieu de 5."""
    total = 4 if u.get("deja_trade") == "Oui" else 5
    ordre = ["prenom", "whatsapp", "trade"] + (["frein"] if total == 5 else []) + ["presence"]
    return f"<b>{emoji} Question {ordre.index(etape) + 1} sur {total}</b>"


async def message_deja_inscrit(bot, uid):
    u = db.get_user(uid)
    await ecrire(bot, uid,
                 "<b>✅ Tu es déjà inscrit(e)</b>\n\n"
                 f"Rendez-vous {dates_texte(jours_choisis(u))} à {HEURE_LIVE} h (heure du Bénin).\n\n"
                 "Le lien du live sera publié uniquement dans le canal Telegram, le jour même. "
                 "Si ce n'est pas encore fait, rejoins-le maintenant :\n\n"
                 f"{CANAL_TELEGRAM}")


async def demarrer_questionnaire(bot, uid):
    if uid in FINALISATION:                         # validation déjà en cours : on ne relance rien
        return
    u = db.get_user(uid)
    if inscrit(u):                                  # jamais deux fois pour la même personne
        await message_deja_inscrit(bot, uid)
        return

    if not u or u.get("webinaire") != webinaire():
        # Nouveau webinaire : on garde prénom / WhatsApp / Q3 / Q4 déjà connus,
        # mais la présence est à redonner (les anciennes réponses concernaient la session précédente).
        db.upsert_user(uid, webinaire=webinaire(), presence=None, veut_j1=None, veut_j2=None,
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
            lancer(finaliser_inscription(bot, uid))      # en tâche de fond : l'attente ne bloque personne
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
            "Seras-tu là mercredi prochain et jeudi prochain ? ⚠️\n\n"
            "Ça se passera en direct, il n'y aura pas de replay !",
            kb((f"🔥 Oui, je serai là les DEUX soirs à {HEURE_LIVE}h", "q:presence:deux"),
               ("Je pourrai être là un seul soir", "q:presence:un")))
    else:   # "soir"
        msg = await ecrire(bot, uid, "<b>📅 Lequel des deux soirs ?</b>",
                           kb(*[(JOURS[j]["relatif"].capitalize(), f"q:soir:{j}") for j in JOURS]))

    noter(uid, msg)


async def finaliser_inscription(bot, uid):
    """« Tout est prêt » → 2-4 s → inscription enregistrée → chat nettoyé → confirmation."""
    try:
        u = db.get_user(uid)
        noter(uid, await ecrire(bot, uid, f"<b>✅ Tout est prêt, {h(u.get('prenom') or '')}.</b>\n\n"
                                          "Je suis en train de valider ta place…"))
        await asyncio.sleep(random.uniform(DELAI_VALIDATION_MIN, DELAI_VALIDATION_MAX))
        db.marquer_confirme(uid, webinaire())
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
        "<b>📅 Dates</b>\n"
        f"La formation gratuite aura lieu mercredi prochain et jeudi prochain, à {HEURE_LIVE} h (heure du Bénin).\n\n"
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
                   f"N'oublie pas : {dates_texte(jours_choisis(u))} à {HEURE_LIVE} h, heure de Cotonou.\n\n"
                   f"Le lien du live sera publié dans le canal Telegram, le jour même : {CANAL_TELEGRAM}")
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