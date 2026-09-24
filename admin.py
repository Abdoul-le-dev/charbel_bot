"""
Bot Trading Pour Tous — CÔTÉ ADMIN.
Cibles, rappels planifiés + relances automatiques, commandes admin, gestion des erreurs.

Pour le parcours utilisateur (questionnaire, confirmation, boutons...) : voir client.py
Pour le lancement du bot (main) : voir main.py   →   python main.py
"""
import asyncio
import io
from collections import Counter
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import Application, ContextTypes, ConversationHandler

from database import database as db

from client import (
    A_COMPLETER, DATE_LANCEMENT_RELANCES, DUREE_LIVE, FORMAT, FREINS, HEURE_LIVE,
    HEURE_RAPPORT, JOURS, PAUSE_ENVOI, SEUILS, TOLERANCE, VIDEO_RELANCE, WEBINAIRE,
    admin_seulement, debut_live, envoyer_video, kb, kb_demarrer, lancer, normaliser,
    prevenir_admins, prochaine_etape, restantes,
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
    """5 min et 15 min : texte seul. 30 min : même texte avec la vidéo de bienvenue.
    Après la 3e relance (30 min) sans réponse, la personne n'est plus relancée automatiquement.
    Le message est mémorisé (noter) pour être effacé quand la personne confirme sa place,
    et l'envoi est enregistré (suivi) pour être compté dans le bilan quotidien."""
    n = restantes(u)
    reste = ("il ne te reste qu'un clic pour confirmer ta place" if n == 0
             else f"il te reste {n} question{'s' if n > 1 else ''} pour confirmer ta place")
    texte = f"👋 {u.get('prenom') or 'Hello'}, {reste}.\n\nOù est-ce que tu bloques ?"
    markup = kb(("➡️ Je continue", "demarrer"), ("🆘 Je suis bloqué(e)", "souci"))
    if colonne == "relance30_le":
        msg = await envoyer_video(bot, u["telegram_id"], VIDEO_RELANCE, texte, markup)
    else:
        msg = await bot.send_message(chat_id=u["telegram_id"], text=texte, reply_markup=markup)
    noter(u["telegram_id"], msg)
    db.enregistrer_suivi(u["telegram_id"], "relance", detail=colonne)


async def relancer_incomplets(bot, maintenant):
    """Relances automatiques : UNIQUEMENT les personnes créées à partir de DATE_LANCEMENT_RELANCES
    (les anciens réinvités ne reçoivent que les messages de masse / rappels planifiés).
    Chaque palier attend un vrai écart de temps depuis l'envoi réel du précédent (db.ETAPES_RELANCE) :
    ça évite que les 3 relances partent d'un coup lors d'un rattrapage après redémarrage/retard."""
    for colonne, minutes_attente, colonne_precedente in db.ETAPES_RELANCE:
        cibles = db.users_a_relancer(WEBINAIRE, colonne, minutes_attente, colonne_precedente,
                                     DATE_LANCEMENT_RELANCES)
        for u in cibles:
            db.marquer_relance(u["telegram_id"], colonne)          # d'abord : jamais deux envois
            try:
                await envoyer_relance(bot, u, colonne)
            except Exception as e:
                print(f"Relance uid={u['telegram_id']} : {e}")
            await asyncio.sleep(PAUSE_ENVOI)


async def tick(bot, maintenant=None):
    """Appelée toutes les 30 s : envoie les rappels dont l'heure est arrivée, puis les relances,
    puis vérifie s'il est l'heure d'envoyer le bilan quotidien."""
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
    await verifier_rapport_quotidien(bot, maintenant)


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
# BILAN QUOTIDIEN — envoyé une fois par jour à HEURE_RAPPORT (heure du Bénin),
# avec le détail des erreurs survenues dans la journée.
# ════════════════════════════════════════════════════════════════════════════

def _pourcentage(n, total) -> str:
    return f"{(n / total * 100):.1f}%" if total else "0.0%"


ETAPES_FUNNEL = [("prenom", "Prénom"), ("whatsapp", "WhatsApp"), ("trade", "Trading"),
                 ("frein", "Frein"), ("presence", "Présence")]


def _compter_funnel(depuis: str) -> dict:
    """Regroupe les personnes non terminées par étape où elles sont bloquées.
    'soir' et 'confirmation' (juste après la présence) sont comptées avec 'presence'."""
    buckets = {cle: 0 for cle, _ in ETAPES_FUNNEL}
    for u in db.users_non_completes(depuis):
        etape = prochaine_etape(u)
        if etape in ("soir", "confirmation"):
            etape = "presence"
        if etape in buckets:
            buckets[etape] += 1
    return buckets


async def generer_rapport_quotidien(bot, maintenant):
    aujourdhui = maintenant.strftime("%Y-%m-%d")
    debut_semaine = (maintenant - timedelta(days=6)).strftime("%Y-%m-%d")   # 7 derniers jours glissants

    approb_jour, compl_jour = db.compter_approbations(aujourdhui, aujourdhui), db.compter_completions(aujourdhui, aujourdhui)
    approb_semaine = db.compter_approbations(debut_semaine, aujourdhui)
    compl_semaine = db.compter_completions(debut_semaine, aujourdhui)
    relances_jour = db.compter_relances(aujourdhui, aujourdhui)

    funnel = _compter_funnel(DATE_LANCEMENT_RELANCES)
    total_depuis_lancement = db.compter_membres_depuis(DATE_LANCEMENT_RELANCES)
    total_bloque = sum(funnel.values())

    lignes_funnel = "\n".join(
        f"• Bloqués à l'étape {libelle} : {funnel[cle]} ({_pourcentage(funnel[cle], total_depuis_lancement)})"
        for cle, libelle in ETAPES_FUNNEL)

    texte = (
        f"📊 Bilan quotidien — {maintenant:%d/%m/%Y}\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🆕 Aujourd'hui\n"
        f"• Nouvelles demandes approuvées : {approb_jour}\n"
        f"• Nouvelles inscriptions complètes : {compl_jour}\n"
        f"• Taux de complétion du jour : {_pourcentage(compl_jour, approb_jour)}\n"
        f"• Relances envoyées : {relances_jour}\n\n"
        "📆 Cette semaine (7 derniers jours)\n"
        f"• Nouvelles demandes approuvées : {approb_semaine}\n"
        f"• Nouvelles inscriptions complètes : {compl_semaine}\n"
        f"• Taux de complétion de la semaine : {_pourcentage(compl_semaine, approb_semaine)}\n\n"
        f"🚨 Funnel — points de blocage (depuis le {DATE_LANCEMENT_RELANCES[:10]})\n"
        f"{lignes_funnel}\n"
        f"• Total bloqués : {total_bloque}")

    erreurs = db.erreurs_non_envoyees()
    if erreurs:
        lignes_erreurs = "\n".join(
            f"• {e['survenue_le'][11:]} — {e['type_erreur']} (user {e['utilisateur']}) : {e['message'][:150]}"
            for e in erreurs)
        texte += f"\n\n🐞 Erreurs du jour ({len(erreurs)})\n{lignes_erreurs}"
    else:
        texte += "\n\n✅ Aucune erreur aujourd'hui."

    await prevenir_admins(bot, texte)
    db.marquer_erreurs_envoyees()


async def verifier_rapport_quotidien(bot, maintenant):
    aujourdhui = maintenant.strftime("%Y-%m-%d")
    if maintenant.hour >= HEURE_RAPPORT and not db.rapport_deja_envoye(aujourdhui):
        db.marquer_rapport_envoye(aujourdhui)          # d'abord : jamais deux envois le même jour
        try:
            await generer_rapport_quotidien(bot, maintenant)
        except Exception as e:
            print(f"Bilan quotidien : {e}")


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
# On n'envoie plus rien aux admins immédiatement : chaque erreur est journalisée (logs serveur
# + base de données) et sera détaillée une seule fois, dans le bilan quotidien de HEURE_RAPPORT.
# ════════════════════════════════════════════════════════════════════════════

async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    qui = "inconnu"
    if isinstance(update, Update) and update.effective_user:
        qui = str(update.effective_user.id)

    type_erreur = type(ctx.error).__name__
    message = str(ctx.error) or "(pas de message)"

    print(f"[ERREUR] {type_erreur} — user={qui} — {message}")   # trace complète : toujours dans les logs serveur
    db.log_erreur(qui, type_erreur, message)