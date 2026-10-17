"""Acces a la base de code (SQLite).

Difference essentielle avec le serveur 'codedoc' : la, le code arrive dans le
prompt. Ici, le modele manipule une REFERENCE (un identifiant ou un chemin), et
c'est la base qui fournit le contenu. Trois consequences pratiques :

  - rien a coller dans le prompt, donc aucune limite de taille cote modele ;
  - on peut indexer des milliers de fichiers sans saturer la fenetre de contexte ;
  - la base est le point de passage oblige, donc l'endroit ou poser les droits.

Le schema est volontairement minimal : une table, un index, et l'empreinte du
contenu pour ne reindexer que ce qui a change.

En production, la meme interface s'appuie sur PostgreSQL, et l'indexation se fait
par un travail planifie plutot qu'a la main (voir scripts/index_code.py).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS code_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chemin      TEXT    NOT NULL UNIQUE,   -- chemin relatif a la racine indexee
    racine      TEXT    NOT NULL,          -- racine de l'indexation
    langage     TEXT    NOT NULL DEFAULT 'inconnu',
    description TEXT    NOT NULL DEFAULT '',
    contenu     TEXT    NOT NULL,
    taille      INTEGER NOT NULL,
    empreinte   TEXT    NOT NULL,          -- sha256 du contenu : evite de reindexer
    indexe_le   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_code_langage ON code_entries(langage);
CREATE INDEX IF NOT EXISTS idx_code_chemin  ON code_entries(chemin);
"""


@dataclass(frozen=True)
class CodeEntry:
    """Une entree du catalogue, sans son contenu."""

    id: int
    chemin: str
    langage: str
    description: str
    taille: int
    empreinte: str

    def as_dict(self) -> dict:
        return {
            "reference": self.id,
            "chemin": self.chemin,
            "langage": self.langage,
            "description": self.description,
            "taille_octets": self.taille,
        }


def _ligne_vers_entree(row: sqlite3.Row) -> CodeEntry:
    return CodeEntry(
        id=row["id"],
        chemin=row["chemin"],
        langage=row["langage"],
        description=row["description"],
        taille=row["taille"],
        empreinte=row["empreinte"],
    )


class CodeDatabase:
    """Base de code interrogeable.

    Chaque methode ouvre et ferme sa connexion : c'est le choix le plus simple a
    lire, et suffisant ici (les serveurs MCP sont appeles sequentiellement par
    une requete). Un service a fort trafic garderait un pool de connexions.
    """

    def __init__(self, chemin: str | Path) -> None:
        self.chemin = Path(chemin)

    # --- Ouverture -----------------------------------------------------------

    def connecter(self) -> sqlite3.Connection:
        self.chemin.parent.mkdir(parents=True, exist_ok=True)
        connexion = sqlite3.connect(self.chemin, timeout=10)
        connexion.row_factory = sqlite3.Row
        return connexion

    def initialiser(self) -> None:
        """Cree le schema s'il n'existe pas (idempotent)."""
        with self.connecter() as connexion:
            connexion.executescript(SCHEMA)

    # --- Ecriture (utilisee par l'indexation) --------------------------------

    def indexer(
        self,
        *,
        chemin: str,
        racine: str,
        contenu: str,
        langage: str,
        description: str,
        empreinte: str,
        horodatage: str,
    ) -> str:
        """Insere ou met a jour une entree. Retourne 'nouveau', 'maj' ou 'inchange'.

        L'empreinte du contenu evite de reecrire une entree identique : utile
        quand l'indexation tourne souvent.
        """
        with self.connecter() as connexion:
            existante = connexion.execute(
                "SELECT id, empreinte FROM code_entries WHERE chemin = ?", (chemin,)
            ).fetchone()

            if existante is None:
                connexion.execute(
                    """
                    INSERT INTO code_entries
                        (chemin, racine, langage, description, contenu, taille,
                         empreinte, indexe_le)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (chemin, racine, langage, description, contenu, len(contenu),
                     empreinte, horodatage),
                )
                return "nouveau"

            if existante["empreinte"] == empreinte:
                return "inchange"

            connexion.execute(
                """
                UPDATE code_entries
                   SET racine = ?, langage = ?, description = ?, contenu = ?,
                       taille = ?, empreinte = ?, indexe_le = ?
                 WHERE chemin = ?
                """,
                (racine, langage, description, contenu, len(contenu), empreinte,
                 horodatage, chemin),
            )
            return "maj"

    def vider(self) -> None:
        with self.connecter() as connexion:
            connexion.execute("DELETE FROM code_entries")

    # --- Lecture (utilisee par les outils MCP) ------------------------------

    def inventaire(self) -> dict:
        """Vue d'ensemble : combien d'entrees, par langage."""
        self.initialiser()
        with self.connecter() as connexion:
            total = connexion.execute("SELECT COUNT(*) AS n FROM code_entries").fetchone()["n"]
            par_langage = connexion.execute(
                """
                SELECT langage, COUNT(*) AS n, SUM(taille) AS octets
                  FROM code_entries GROUP BY langage ORDER BY n DESC
                """
            ).fetchall()

        return {
            "entrees": total,
            "langages": [
                {"langage": ligne["langage"], "fichiers": ligne["n"], "octets": ligne["octets"]}
                for ligne in par_langage
            ],
        }

    def lister(self, *, langage: str | None = None, limite: int = 20) -> list[CodeEntry]:
        self.initialiser()
        with self.connecter() as connexion:
            if langage:
                lignes = connexion.execute(
                    """
                    SELECT * FROM code_entries WHERE langage = ?
                     ORDER BY taille DESC LIMIT ?
                    """,
                    (langage, limite),
                ).fetchall()
            else:
                lignes = connexion.execute(
                    "SELECT * FROM code_entries ORDER BY taille DESC LIMIT ?", (limite,)
                ).fetchall()
        return [_ligne_vers_entree(ligne) for ligne in lignes]

    def chercher(
        self, *, requete: str, langage: str | None = None, limite: int = 10
    ) -> list[CodeEntry]:
        """Recherche simple sur le chemin, la description et le contenu.

        Volontairement en LIKE plutot qu'en plein texte : c'est lisible, sans
        dependance, et suffisant a cette echelle. La montee en gamme naturelle
        est un index FTS5 (SQLite) ou un moteur de recherche dedie.
        """
        self.initialiser()
        motif = f"%{requete}%"
        with self.connecter() as connexion:
            if langage:
                lignes = connexion.execute(
                    """
                    SELECT * FROM code_entries
                     WHERE langage = ?
                       AND (chemin LIKE ? OR description LIKE ? OR contenu LIKE ?)
                     ORDER BY (chemin LIKE ?) DESC, taille DESC LIMIT ?
                    """,
                    (langage, motif, motif, motif, motif, limite),
                ).fetchall()
            else:
                lignes = connexion.execute(
                    """
                    SELECT * FROM code_entries
                     WHERE chemin LIKE ? OR description LIKE ? OR contenu LIKE ?
                     ORDER BY (chemin LIKE ?) DESC, taille DESC LIMIT ?
                    """,
                    (motif, motif, motif, motif, limite),
                ).fetchall()
        return [_ligne_vers_entree(ligne) for ligne in lignes]

    def lire(self, reference: str | int) -> tuple[CodeEntry, str] | None:
        """Retrouve une entree par identifiant, par chemin exact, ou par morceau de chemin.

        Accepte plusieurs formes pour rester tolerant a la maniere dont le modele
        designe un fichier : '42', 'gateway/config.yaml', ou 'config.yaml'.
        """
        self.initialiser()
        texte = str(reference).strip()

        with self.connecter() as connexion:
            ligne = None
            if texte.isdigit():
                ligne = connexion.execute(
                    "SELECT * FROM code_entries WHERE id = ?", (int(texte),)
                ).fetchone()
            if ligne is None:
                ligne = connexion.execute(
                    "SELECT * FROM code_entries WHERE chemin = ?", (texte,)
                ).fetchone()
            if ligne is None:
                # Le plus court chemin correspondant : evite de renvoyer un
                # fichier au hasard quand plusieurs contiennent la reference.
                ligne = connexion.execute(
                    """
                    SELECT * FROM code_entries WHERE chemin LIKE ?
                     ORDER BY LENGTH(chemin) ASC LIMIT 1
                    """,
                    (f"%{texte}%",),
                ).fetchone()
            if ligne is None:
                return None

            contenu = connexion.execute(
                "SELECT contenu FROM code_entries WHERE id = ?", (ligne["id"],)
            ).fetchone()["contenu"]

        return _ligne_vers_entree(ligne), contenu
