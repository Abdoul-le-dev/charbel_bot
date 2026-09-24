"""
Bot Trading Pour Tous — CÔTÉ ADMIN.
Cibles, rappels planifiés + relances automatiques, commandes admin, gestion des erreurs.

Pour le parcours utilisateur (questionnaire, confirmation, boutons...) : voir client.py
Pour le lancement du bot (main) : voir main.py   →   python main.py
"""
import asyncio
import io
import os
from collections import Counter
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import Application, ContextTypes, ConversationHandler

from database import database as db

from client import (
    A_COMPLETER, ADMIN_IDS, DUREE_LIVE, FORMAT, FREINS, HEURE_LIVE, JOURS, PAUSE_ENVOI,
    SEUILS, TOLERANCE, VIDEO_RELANCE, WEBINAIRE,
    admin_seulement, debut_live, envoyer_video, kb, kb_demarrer, lancer, normaliser,
    prevenir_admins, restantes,
)
from nettoyage import noter

# ════════════════════════════════════════════════════════════════════════════
# CIBLES (utilisées par /envoyer, /export et les rappels)
# ════════════════════════════════════════════════════════════════════════════

def _incomplets():
    confirmes = set(db.ids_confirmes(WEBINAIRE))
    return [i for i in db.ids_tous() if i not in confirmes]


def _presents(jour):
    presents = set(db.ids_presents(jour, SEUILS[jour]))
    return [i for i in db.ids_confirmes(WEBINAIRE, jour) if i in presents]


def _absents(jour):
    presents = set(db.ids_presents(jour, SEUILS[jour]))
    return [i for i in db.ids_confirmes(WEBINAIRE, jour) if i not in presents]


# clé -> (libellé, fonction qui retourne la liste des identifiants)
CIBLES = {
    "tous":         ("Tous (canal + bot)",                    db.ids_tous),
    "confirmes":    ("Confirmés",                             lambda: db.ids_confirmes(WEBINAIRE)),
    "incomplets":   ("Incomplets (pas confirmés)",            _incomplets),
    "sept":         ("Anciens inscrits de septembre",         db.ids_septembre),
    "j1":           (f"Confirmés pour le {JOURS[1]['nom']}",  lambda: db.ids_confirmes(WEBINAIRE, 1)),
    "j2":           (f"Confirmés pour le {JOURS[2]['nom']}",  lambda: db.ids_confirmes(WEBINAIRE, 2)),
    "presents_1":   (f"Présents le {JOURS[1]['nom']}",        lambda: _presents(1)),
    "absents_1":    (f"Absents le {JOURS[1]['nom']}",         lambda: _absents(1)),
    "presents_2":   (f"Présents le {JOURS[2]['nom']}",        lambda: _presents(2)),
    "absents_2":    (f"Absents le {JOURS[2]['nom']}",         lambda: _absents(2)),
    "debutants":    ("Débutants (jamais tradé)",              lambda: db.ids_confirmes(WEBINAIRE, champ="deja_trade", valeur="Non")),
    "traders":      ("Ont déjà tradé",                        lambda: db.ids_confirmes(WEBINAIRE, champ="deja_trade", valeur="Oui")),
    "un_seul_soir": ("Un seul soir",                          lambda: db.ids_confirmes(WEBINAIRE, champ="presence", valeur="Un seul soir")),
}
for _i, _frein in enumerate(FREINS):
    CIBLES[f"frein_{_i}"] = (f"Frein : {_frein}",
                             lambda f=_frein: db.ids_confirmes(WEBINAIRE, champ="frein", valeur=f))


# ════════════════════════════════════════════════════════════════════════════
# RAPPELS PLANIFIÉS (modifiables par l'admin) + RELANCES AUTOMATIQUES
# ════════════════════════════════════════════════════════════════════════════

# (moment, texte) — moment = heure fixe "12:00" ou décalage en minutes par rapport au début du live.
# {prenom} est remplacé par le prénom. Le bouton « Rejoindre le live » est ajouté à chaque message.
SEQUENCE_SOIR = [
    ("12:00", f"{{prenom}}, ce soir à {HEURE_LIVE}h.\n\nTon lien est déjà là, juste en dessous."),
    (-180, "⏰ Dans 3 heures.\n\nCharge ton téléphone."),
    (-30, "⏰ Dans 30 minutes.\n\nConnecte-toi maintenant."),
    (5, "🔴 On a commencé.\n\nPlus d'une centaine de personnes sont déjà là."),
]


def _apres_live(jour, minutes) -> str:
    return (debut_live(jour) + timedelta(minutes=minutes)).strftime(FORMAT)


# (nom, cible, date d'envoi, texte, vidéo, bouton)  — bouton : "live_1" / "live_2" / "demarrer" / ""
PLANNING = [
    ("Réinvitation septembre", "sept", "2026-09-25 12:00:00",
     "{prenom},\n\nla formation gratuite revient :\nmercredi 30 septembre et jeudi 1er octobre, à "
     f"{HEURE_LIVE}h.\n\nTu avais réservé ta place en septembre. Elle t'attend toujours.\n\nConfirme-la ici.",
     "", "demarrer"),
    ("Veille J1 (vidéo)", "j1", "2026-09-29 20:00:00",
     f"Demain soir, {HEURE_LIVE}h.\n\nVoilà ce que tu vas voir.", "29.mp4", "live_1"),
    ("Fin J1 → demain", "confirmes", _apres_live(1, DUREE_LIVE * 60 + 15),
     f"Demain, je montre [{A_COMPLETER}].\n\nMême heure, même lien.", "", "live_2"),
    ("Absents J1 (replay)", "absents_1", _apres_live(1, DUREE_LIVE * 60 + 45),
     f"[{A_COMPLETER}] texte replay + relance", "", ""),
    ("Présents J1 (offre)", "presents_1", _apres_live(1, DUREE_LIVE * 60 + 45),
     f"[{A_COMPLETER}] texte de l'offre", "", ""),
    ("Absents J2 (replay)", "absents_2", _apres_live(2, DUREE_LIVE * 60 + 45),
     f"[{A_COMPLETER}] texte replay + relance", "", ""),
    ("Présents J2 (offre)", "presents_2", _apres_live(2, DUREE_LIVE * 60 + 45),
     f"[{A_COMPLETER}] texte de l'offre", "", ""),
]
for _jour, _infos in JOURS.items():
    for _moment, _texte in SEQUENCE_SOIR:
        if isinstance(_moment, str):
            _quand = datetime.strptime(f"{_infos['date']} {_moment}", "%Y-%m-%d %H:%M")
        else:
            _quand = debut_live(_jour) + timedelta(minutes=_moment)
        PLANNING.append((f"J{_jour} {_quand:%H:%M}", f"j{_jour}", _quand.strftime(FORMAT),
                         _texte, "", f"live_{_jour}"))
PLANNING.sort(key=lambda r: r[2])


def clavier_rappel(bouton):
    if bouton.startswith("live_"):
        return kb(("▶️ Rejoindre le live", f"live:{bouton[5:]}"))
    if bouton == "demarrer":
        return kb_demarrer()
    return None


async def envoyer_a(bot, uid, texte, video, markup):
    """Un message de rappel, en texte brut (le texte écrit par l'admin n'est jamais interprété)."""
    prenom = (db.get_user(uid) or {}).get("prenom") or "Hello"
    texte = texte.replace("{prenom}", prenom)
    if video:
        await envoyer_video(bot, uid, video, texte, markup)
    else:
        await bot.send_message(chat_id=uid, text=texte, reply_markup=markup)


async def envoyer_rappel(bot, rappel):
    ids = CIBLES[rappel["cible"]][1]()
    markup = clavier_rappel(rappel["bouton"])
    ok = erreurs = 0
    for uid in ids:
        try:
            await envoyer_a(bot, uid, rappel["texte"], rappel["video"], markup)
            ok += 1
        except Exception as e:
            erreurs += 1
            print(f"Rappel #{rappel['id']} uid={uid} : {e}")
        await asyncio.sleep(PAUSE_ENVOI)
    await prevenir_admins(bot, f"✅ Rappel #{rappel['id']} « {rappel['nom']} » : "
                               f"{ok}/{len(ids)} envoyés ({erreurs} échecs : bot bloqué, etc.)")


async def envoyer_relance(bot, u, colonne):
    """+10 min : texte. +30 min : même texte avec la vidéo de bienvenue.
    Le message est mémorisé (noter) pour être effacé quand la personne confirme sa place."""
    n = restantes(u)
    reste = ("il ne te reste qu'un clic pour confirmer ta place" if n == 0
             else f"il te reste {n} question{'s' if n > 1 else ''} pour confirmer ta place")
    texte = f"👋 {u.get('prenom') or 'Hello'}, {reste}.\n\nOù est-ce que tu bloques ?"
    markup = kb(("➡️ Je continue", "demarrer"), ("🆘 Je suis bloqué(e)", "souci"))
    if colonne == "relance30":
        msg = await envoyer_video(bot, u["telegram_id"], VIDEO_RELANCE, texte, markup)
    else:
        msg = await bot.send_message(chat_id=u["telegram_id"], text=texte, reply_markup=markup)
    noter(u["telegram_id"], msg)


async def relancer_incomplets(bot, maintenant):
    for colonne, minutes in (("relance10", 10), ("relance30", 30)):
        for u in db.users_a_relancer(WEBINAIRE, colonne, maintenant - timedelta(minutes=minutes)):
            db.marquer_relance(u["telegram_id"], colonne)          # d'abord : jamais deux envois
            try:
                await envoyer_relance(bot, u, colonne)
            except Exception as e:
                print(f"Relance uid={u['telegram_id']} : {e}")
            await asyncio.sleep(PAUSE_ENVOI)


async def tick(bot, maintenant=None):
    """Appelée toutes les 30 s : envoie les rappels dont l'heure est arrivée, puis les relances."""
    maintenant = maintenant or db.now_benin()
    for r in db.rappels_a_traiter(maintenant.strftime(FORMAT)):
        retard = maintenant - datetime.strptime(r["date_envoi"], FORMAT)
        if retard > TOLERANCE:
            db.set_statut_rappel(r["id"], db.MANQUE)
            await prevenir_admins(bot, f"⚠️ Rappel #{r['id']} « {r['nom']} » NON envoyé (heure dépassée de "
                                       f"{int(retard.total_seconds() // 60)} min). Pour l'envoyer quand même : "
                                       f"/envoyer_rappel {r['id']} oui")
        elif A_COMPLETER in r["texte"].upper():
            db.set_statut_rappel(r["id"], db.MANQUE)
            await prevenir_admins(bot, f"⚠️ Rappel #{r['id']} « {r['nom']} » NON envoyé : texte pas complété. "
                                       f"Écris-le avec /modifier {r['id']} ton texte, puis /envoyer_rappel {r['id']} oui")
        else:
            db.set_statut_rappel(r["id"], db.ENVOYE)
            await envoyer_rappel(bot, r)
    await relancer_incomplets(bot, maintenant)


async def boucle_planificateur(bot):
    while True:
        try:
            await tick(bot)
        except Exception as e:
            print(f"Planificateur : {e}")
        await asyncio.sleep(30)


async def demarrer_planificateur(app: Application):
    lancer(boucle_planificateur(app.bot))


# ════════════════════════════════════════════════════════════════════════════
# COMMANDES ADMIN — rappels et codes
# ════════════════════════════════════════════════════════════════════════════

@admin_seulement
async def cmd_aide(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Commandes admin :\n\n"
        "/rappels — liste des messages planifiés\n"
        "/modifier <n°> <texte> — changer le texte d'un rappel ({prenom} = prénom)\n"
        "/tester_rappel <n°> — recevoir le rappel chez toi pour vérifier\n"
        "/envoyer_rappel <n°> oui — l'envoyer maintenant à sa cible\n"
        "/codes 1 NASDAQ OR BTC — définir les 3 codes du soir 1 (ou 2)\n"
        "/envoyer — diffuser un message à une cible\n"
        "/export — exporter un tableau Excel (tu choisis la cible)\n"
        "/stats — statistiques\n"
        "/nouvelle_categorie <nom>")


@admin_seulement
async def cmd_rappels(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    icones = {db.PLANIFIE: "⏳", db.ENVOYE: "✅", db.MANQUE: "⚠️"}
    lignes = []
    for r in db.get_rappels():
        date = datetime.strptime(r["date_envoi"], FORMAT).strftime("%d/%m %H:%M")
        extrait = r["texte"].replace("\n", " ")[:55]
        lignes.append(f"{icones[r['statut']]} #{r['id']} · {date} · {r['cible']}\n      {extrait}")
    await update.message.reply_text("⏳ planifié · ✅ envoyé · ⚠️ non envoyé\n\n" + "\n".join(lignes))


@admin_seulement
async def cmd_modifier(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    parties = update.message.text.split(maxsplit=2)
    if len(parties) < 3 or not parties[1].isdigit() or not db.get_rappel(int(parties[1])):
        await update.message.reply_text("Usage : /modifier <n°> <nouveau texte>\nEx : /modifier 3 Ce soir 21h !")
        return
    db.modifier_texte_rappel(int(parties[1]), parties[2])
    await update.message.reply_text(f"✅ Rappel #{parties[1]} mis à jour. Vérifie avec /tester_rappel {parties[1]}")


@admin_seulement
async def cmd_tester_rappel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rappel = ctx.args and ctx.args[0].isdigit() and db.get_rappel(int(ctx.args[0]))
    if not rappel:
        await update.message.reply_text("Usage : /tester_rappel <n°>")
        return
    await update.message.reply_text(f"Aperçu du rappel #{rappel['id']} (cible : {rappel['cible']}) :")
    await envoyer_a(ctx.bot, update.effective_user.id, rappel["texte"], rappel["video"],
                    clavier_rappel(rappel["bouton"]))


@admin_seulement
async def cmd_envoyer_rappel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rappel = ctx.args and ctx.args[0].isdigit() and db.get_rappel(int(ctx.args[0]))
    if not rappel or ctx.args[1:] != ["oui"]:
        await update.message.reply_text("Usage : /envoyer_rappel <n°> oui")
        return
    db.set_statut_rappel(rappel["id"], db.ENVOYE)
    await update.message.reply_text(f"📤 Envoi du rappel #{rappel['id']} lancé…")
    lancer(envoyer_rappel(ctx.bot, rappel))


@admin_seulement
async def cmd_codes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) == 4 and ctx.args[0] in ("1", "2"):
        mots = [normaliser(m) for m in ctx.args[1:]]
        db.definir_codes(int(ctx.args[0]), mots)
        await update.message.reply_text(f"✅ Codes du soir {ctx.args[0]} : " + " · ".join(mots))
        return
    actuels = "\n".join(f"Soir {c['jour']} — code {c['rang']} : {c['mot']}" for c in db.get_codes()) or "(aucun)"
    await update.message.reply_text(f"Codes actuels :\n{actuels}\n\nPour définir : /codes 1 NASDAQ OR BTC")


# ════════════════════════════════════════════════════════════════════════════
# COMMANDES ADMIN — /envoyer et /export (même choix de cible)
# ════════════════════════════════════════════════════════════════════════════

CHOIX_CIBLE, RECEVOIR_MESSAGE = range(2)


async def afficher_cibles(update, ctx, action, titre):
    ctx.user_data["action"] = action
    lignes = [titre, ""]
    for numero, (libelle, get_ids) in enumerate(CIBLES.values(), start=1):
        lignes.append(f"{numero} — {libelle} ({len(get_ids())})")
    lignes.append("\nRéponds avec le numéro (ou /cancel).")
    await update.message.reply_text("\n".join(lignes))
    return CHOIX_CIBLE


@admin_seulement
async def cmd_envoyer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    return await afficher_cibles(update, ctx, "envoyer", "À qui envoyer le message ?")


@admin_seulement
async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    return await afficher_cibles(update, ctx, "export", "Quelle liste exporter ?")


async def choisir_cible(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cles = list(CIBLES)
    choix = update.message.text.strip()
    if not choix.isdigit() or not 1 <= int(choix) <= len(cles):
        await update.message.reply_text(f"❌ Réponds avec un numéro entre 1 et {len(cles)}.")
        return CHOIX_CIBLE
    cle = cles[int(choix) - 1]
    ctx.user_data["cible"] = cle

    if ctx.user_data["action"] == "export":
        await exporter(update, cle)
        return ConversationHandler.END
    await update.message.reply_text(f"✅ Cible : {CIBLES[cle][0]}\n\nEnvoie maintenant ton message "
                                    "(texte, photo ou vidéo avec légende).")
    return RECEVOIR_MESSAGE


async def recevoir_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cle = ctx.user_data["cible"]
    await update.message.reply_text("📤 Diffusion lancée en arrière-plan…")
    lancer(diffuser(ctx.bot, update.effective_chat.id, update.message.message_id, cle))
    return ConversationHandler.END


async def annuler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Annulé.")
    return ConversationHandler.END


async def diffuser(bot, admin_chat, message_id, cle):
    """Copie le message de l'admin (quel que soit son format) à chaque personne de la cible."""
    libelle, get_ids = CIBLES[cle]
    ids = get_ids()
    ok = 0
    for uid in ids:
        try:
            await bot.copy_message(chat_id=uid, from_chat_id=admin_chat, message_id=message_id)
            ok += 1
        except Exception as e:
            print(f"Diffusion uid={uid} : {e}")
        await asyncio.sleep(PAUSE_ENVOI)
    await bot.send_message(chat_id=admin_chat, text=f"Diffusion terminée — {ok}/{len(ids)} envoyés à « {libelle} ».")


def construire_excel(lignes) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Export"
    entetes = list(lignes[0])
    ws.append(entetes)
    for ligne in lignes:
        ws.append([ligne[e] for e in entetes])

    cles = {"Q3 Déjà tradé", "Q4 Frein", "Q5 Présence"}          # colonnes à lire avant le live
    for cellule in ws[1]:
        couleur = "C00000" if cellule.value in cles else "1F4E79"
        cellule.fill = PatternFill("solid", fgColor=couleur)
        cellule.font = Font(bold=True, color="FFFFFF")
        cellule.alignment = Alignment(horizontal="center", wrap_text=True)
        ws.column_dimensions[cellule.column_letter].width = 22
    ws.freeze_panes = "A2"

    synthese = wb.create_sheet("Synthèse")
    for titre in ("Q3 Déjà tradé", "Q4 Frein", "Q5 Présence"):
        synthese.append([titre])
        synthese.append(["Réponse", "Nombre"])
        for reponse, n in Counter(l[titre] or "(vide)" for l in lignes).most_common():
            synthese.append([reponse, n])
        synthese.append([])
    synthese.column_dimensions["A"].width = 45

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


async def exporter(update, cle):
    libelle, get_ids = CIBLES[cle]
    ids = get_ids()
    if not ids:
        await update.message.reply_text(f"❌ Personne dans « {libelle} ».")
        return
    lignes = db.lignes_export(ids, SEUILS)
    date = db.now_benin().strftime("%d%m_%Hh%M")
    await update.message.reply_document(
        document=construire_excel(lignes), filename=f"export_{cle}_{date}.xlsx",
        caption=f"Export « {libelle} » : {len(lignes)} personnes.\n"
                "Onglet « Synthèse » : réponses Q3 / Q4 / Q5 comptées.")


# ════════════════════════════════════════════════════════════════════════════
# COMMANDES ADMIN — /stats et catégories
# ════════════════════════════════════════════════════════════════════════════

@admin_seulement
async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    def bloc(dictionnaire):
        return "\n".join(f"  • {k} : {v}" for k, v in dictionnaire.items()) or "  (aucun)"

    tous, confirmes = db.ids_tous(), db.ids_confirmes(WEBINAIRE)
    soirs = "\n".join(
        f"  • {JOURS[j]['nom']} : {len(db.ids_confirmes(WEBINAIRE, j))} prévus · "
        f"{db.nb_clics('live', j)} ont cliqué · {len(db.ids_presents(j, SEUILS[j]))} présents"
        for j in JOURS)
    await update.message.reply_text(
        "Statistiques Trading Pour Tous\n\n"
        f"👥 Personnes connues : {len(tous)}\n"
        f"✅ Confirmées : {len(confirmes)}\n"
        f"⏳ Pas encore confirmées : {len(tous) - len(confirmes)}\n\n"
        f"Liens d'entrée :\n{bloc(db.compter_liens())}\n\n"
        f"Q3 Déjà tradé :\n{bloc(db.compter_reponses(WEBINAIRE, 'deja_trade'))}\n\n"
        f"Q4 Freins :\n{bloc(db.compter_reponses(WEBINAIRE, 'frein'))}\n\n"
        f"Q5 Présence :\n{bloc(db.compter_reponses(WEBINAIRE, 'presence'))}\n\n"
        f"Soirs :\n{soirs}\n\n"
        f"📅 Ont cliqué sur le calendrier : {db.nb_clics('agenda_android') + db.nb_clics('agenda_iphone')}")


@admin_seulement
async def cmd_categorie(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    nom = " ".join(ctx.args).strip()
    if len(nom) < 3:
        liste = "\n".join(f"• {c}" for c in db.get_categories())
        await update.message.reply_text(f"Catégories :\n{liste}\n\nPour en créer une : /nouvelle_categorie <nom>")
        return
    db.ajouter_categorie(nom)
    await update.message.reply_text(f"✅ Catégorie « {nom} » créée.")


# ════════════════════════════════════════════════════════════════════════════
# GESTION DES ERREURS
# On ne renvoie plus toute la trace complète aux admins : juste l'essentiel (utilisateur + type
# d'erreur + message), écrit dans un petit fichier, envoyé, puis supprimé du disque après l'envoi.
# La trace complète reste dans les logs du serveur (print), pour un vrai debug si besoin.
# ════════════════════════════════════════════════════════════════════════════

async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    qui = "inconnu"
    if isinstance(update, Update) and update.effective_user:
        qui = str(update.effective_user.id)

    type_erreur = type(ctx.error).__name__
    message = str(ctx.error) or "(pas de message)"

    print(f"[ERREUR] {type_erreur} — user={qui} — {message}")   # trace complète : uniquement dans les logs serveur

    chemin = f"erreur_{datetime.now():%Y%m%d_%H%M%S}.txt"
    with open(chemin, "w", encoding="utf-8") as f:
        f.write(f"Utilisateur : {qui}\nType d'erreur : {type_erreur}\nMessage : {message}\n")

    try:
        for admin_id in ADMIN_IDS:
            with open(chemin, "rb") as f:
                await ctx.bot.send_document(chat_id=admin_id, document=f, filename=chemin,
                                            caption="⚠️ Erreur bot")
    finally:
        if os.path.exists(chemin):
            os.remove(chemin)