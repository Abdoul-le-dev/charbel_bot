#!/usr/bin/env python3
"""
Suppression d'un utilisateur de la base preinscriptions.db

Usage :
    python supprimer_user.py <telegram_id>
    python supprimer_user.py <telegram_id> --force     (sans confirmation)
    python supprimer_user.py --list                    (lister les derniers users)
    python supprimer_user.py --search <mot>            (chercher par prenom/whatsapp/pays)

Exemples :
    python supprimer_user.py 123456789
    python supprimer_user.py 123456789 --force
    python supprimer_user.py --search charbel
"""

import sys
import sqlite3

DB_PATH = "preinscriptions.db"

# Tables où le telegram_id peut apparaitre
TABLES = ["users", "members_log", "messages_libres"]


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def afficher_user(uid: int) -> bool:
    """Affiche les infos d'un user. Retourne True s'il existe quelque part."""
    trouve = False
    with db() as conn:
        row = conn.execute("""
            SELECT telegram_id, prenom, whatsapp, pays, deja_trade,
                   interet, presence, frein, completed, categorie, last_seen
            FROM users WHERE telegram_id = ?
        """, (uid,)).fetchone()

        if row:
            trouve = True
            print("\n--- Table users ---")
            print(f"  ID Telegram   : {row['telegram_id']}")
            print(f"  Prenom        : {row['prenom'] or '(vide)'}")
            print(f"  WhatsApp      : {row['whatsapp'] or '(vide)'}")
            print(f"  Pays          : {row['pays'] or '(vide)'}")
            print(f"  Deja trade    : {row['deja_trade'] or '(vide)'}")
            print(f"  Interet       : {row['interet'] or '(vide)'}")
            print(f"  Presence      : {row['presence'] or '(vide)'}")
            print(f"  Frein         : {row['frein'] or '(vide)'}")
            print(f"  Complete      : {'Oui' if row['completed'] else 'Non'}")
            print(f"  Categorie     : {row['categorie'] or '(vide)'}")
            print(f"  Derniere vue  : {row['last_seen'] or '(jamais)'}")
        else:
            print("\n--- Table users : aucune entree ---")

        try:
            member = conn.execute(
                "SELECT joined_at FROM members_log WHERE telegram_id = ?",
                (uid,)
            ).fetchone()
            if member:
                trouve = True
                print(f"\n--- Table members_log ---")
                print(f"  Arrive le : {member['joined_at']}")
        except Exception as e:
            print(f"  (members_log inaccessible : {e})")

        try:
            nb_msg = conn.execute(
                "SELECT COUNT(*) as n FROM messages_libres WHERE telegram_id = ?",
                (uid,)
            ).fetchone()["n"]
            if nb_msg > 0:
                trouve = True
                print(f"\n--- Table messages_libres ---")
                print(f"  {nb_msg} message(s) libre(s) stocke(s)")
        except Exception:
            pass

    return trouve


def supprimer_user(uid: int) -> dict:
    """Supprime l'user des 3 tables. Retourne un dict avec le nb de lignes supprimees."""
    resultats = {}
    with db() as conn:
        for table in TABLES:
            try:
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE telegram_id = ?", (uid,)
                )
                resultats[table] = cur.rowcount
            except sqlite3.OperationalError as e:
                resultats[table] = f"erreur ({e})"
        conn.commit()
    return resultats


def lister_recents(n: int = 20):
    with db() as conn:
        rows = conn.execute(f"""
            SELECT u.telegram_id, u.prenom, u.whatsapp, u.pays,
                   CASE WHEN u.completed = 1 THEN 'OK' ELSE '--' END AS c,
                   u.last_seen
            FROM users u
            ORDER BY u.last_seen DESC NULLS LAST
            LIMIT {n}
        """).fetchall()

    if not rows:
        print("Aucun utilisateur.")
        return

    print(f"\n{'ID Telegram':<14} {'C':<3} {'Prenom':<20} {'WhatsApp':<20} {'Pays':<15} Vu le")
    print("-" * 100)
    for r in rows:
        print(f"{r['telegram_id']:<14} {r['c']:<3} "
              f"{(r['prenom'] or '')[:19]:<20} "
              f"{(r['whatsapp'] or '')[:19]:<20} "
              f"{(r['pays'] or '')[:14]:<15} "
              f"{r['last_seen'] or ''}")


def chercher(mot: str):
    mot_like = f"%{mot}%"
    with db() as conn:
        rows = conn.execute("""
            SELECT telegram_id, prenom, whatsapp, pays, completed
            FROM users
            WHERE prenom LIKE ? OR whatsapp LIKE ? OR pays LIKE ?
            ORDER BY prenom
        """, (mot_like, mot_like, mot_like)).fetchall()

    if not rows:
        print(f"Aucun resultat pour : {mot}")
        return

    print(f"\n{len(rows)} resultat(s) pour '{mot}' :\n")
    print(f"{'ID Telegram':<14} {'C':<3} {'Prenom':<20} {'WhatsApp':<20} Pays")
    print("-" * 90)
    for r in rows:
        c = "OK" if r["completed"] else "--"
        print(f"{r['telegram_id']:<14} {c:<3} "
              f"{(r['prenom'] or '')[:19]:<20} "
              f"{(r['whatsapp'] or '')[:19]:<20} "
              f"{r['pays'] or ''}")


def main():
    args = sys.argv[1:]

    if not args or args[0] in {"-h", "--help"}:
        print(__doc__)
        sys.exit(0)

    if args[0] == "--list":
        lister_recents()
        sys.exit(0)

    if args[0] == "--search":
        if len(args) < 2:
            print("Erreur : mot-cle manquant apres --search")
            sys.exit(1)
        chercher(args[1])
        sys.exit(0)

    # Suppression
    try:
        uid = int(args[0])
    except ValueError:
        print(f"Erreur : '{args[0]}' n'est pas un ID Telegram valide (entier attendu).")
        sys.exit(1)

    force = "--force" in args or "-f" in args

    print(f"\nRecherche de l'utilisateur {uid} dans {DB_PATH}...")
    existe = afficher_user(uid)

    if not existe:
        print(f"\nAucun utilisateur avec l'ID {uid} dans la base.")
        sys.exit(0)

    if not force:
        print("\n" + "=" * 50)
        rep = input("Confirmer la suppression definitive ? (oui/non) : ").strip().lower()
        if rep not in {"oui", "o", "yes", "y"}:
            print("Annule. Aucune modification.")
            sys.exit(0)

    print("\nSuppression en cours...")
    res = supprimer_user(uid)

    print("\n--- Resultat ---")
    total = 0
    for table, n in res.items():
        print(f"  {table:<20} : {n} ligne(s) supprimee(s)")
        if isinstance(n, int):
            total += n

    print(f"\nTermine. {total} ligne(s) supprimee(s) au total.")


if __name__ == "__main__":
    main()