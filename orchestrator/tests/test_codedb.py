"""Tests de la base de code du serveur MCP 'codedb'.

Ce que l'on verifie ici n'est pas l'appel au modele, mais la couche qui decide
QUEL code est servi : c'est elle qui porte la logique de reference (identifiant,
chemin exact, morceau de chemin) et l'indexation incrementale.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

from mcp_servers.codedb.database import CodeDatabase

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.index_code import extraire_description  # noqa: E402


@pytest.fixture()
def base(tmp_path: Path) -> CodeDatabase:
    database = CodeDatabase(tmp_path / "codedb.sqlite")
    database.initialiser()
    return database


def indexer(database: CodeDatabase, chemin: str, contenu: str, langage: str = "python") -> str:
    return database.indexer(
        chemin=chemin,
        racine="/projet",
        contenu=contenu,
        langage=langage,
        description=extraire_description(contenu, langage),
        empreinte=hashlib.sha256(contenu.encode("utf-8")).hexdigest(),
        horodatage="2026-10-17T00:00:00+00:00",
    )


# --- Indexation incrementale -------------------------------------------------

def test_indexation_nouvelle_puis_inchangee_puis_mise_a_jour(base: CodeDatabase) -> None:
    assert indexer(base, "a.py", "print(1)") == "nouveau"
    assert indexer(base, "a.py", "print(1)") == "inchange"     # meme empreinte
    assert indexer(base, "a.py", "print(2)") == "maj"          # contenu different


def test_inventaire_compte_par_langage(base: CodeDatabase) -> None:
    indexer(base, "a.py", "print(1)")
    indexer(base, "b.py", "print(2)")
    indexer(base, "c.sql", "select 1;", langage="sql")

    inventaire = base.inventaire()

    assert inventaire["entrees"] == 3
    langages = {ligne["langage"]: ligne["fichiers"] for ligne in inventaire["langages"]}
    assert langages == {"python": 2, "sql": 1}


def test_vider_remet_a_zero(base: CodeDatabase) -> None:
    indexer(base, "a.py", "print(1)")
    base.vider()
    assert base.inventaire()["entrees"] == 0


# --- Lecture par reference ---------------------------------------------------

def test_lire_par_identifiant(base: CodeDatabase) -> None:
    indexer(base, "a.py", "print(1)")
    entree, contenu = base.lire(1)
    assert entree.chemin == "a.py"
    assert contenu == "print(1)"


def test_lire_par_chemin_exact(base: CodeDatabase) -> None:
    indexer(base, "orchestrator/app/policy/engine.py", "class PolicyEngine: pass")
    entree, _ = base.lire("orchestrator/app/policy/engine.py")
    assert entree.chemin == "orchestrator/app/policy/engine.py"


def test_lire_par_morceau_de_chemin_prend_le_plus_court(base: CodeDatabase) -> None:
    """'engine.py' doit designer le fichier au chemin le plus court, pas un au hasard."""
    indexer(base, "orchestrator/app/policy/engine.py", "contenu long")
    indexer(base, "engine.py", "contenu court")

    entree, contenu = base.lire("engine.py")

    assert entree.chemin == "engine.py"
    assert contenu == "contenu court"


def test_reference_inconnue_retourne_none(base: CodeDatabase) -> None:
    indexer(base, "a.py", "print(1)")
    assert base.lire("fichier-qui-nexiste-pas.py") is None


# --- Recherche ---------------------------------------------------------------

def test_chercher_sur_le_chemin_la_description_et_le_contenu(base: CodeDatabase) -> None:
    indexer(base, "policy/engine.py", '"""GPO : droits sur les outils."""\nclass PolicyEngine: pass')
    indexer(base, "mailer.py", '"""Envoi de courriels."""\ndef envoyer(): pass')

    par_chemin = base.chercher(requete="policy")
    assert [entree.chemin for entree in par_chemin] == ["policy/engine.py"]

    par_description = base.chercher(requete="courriels")
    assert [entree.chemin for entree in par_description] == ["mailer.py"]

    par_contenu = base.chercher(requete="PolicyEngine")
    assert [entree.chemin for entree in par_contenu] == ["policy/engine.py"]


def test_chercher_filtre_par_langage(base: CodeDatabase) -> None:
    indexer(base, "a.py", "motcle magique")
    indexer(base, "b.sql", "motcle magique", langage="sql")

    resultats = base.chercher(requete="magique", langage="sql")

    assert [entree.chemin for entree in resultats] == ["b.sql"]


def test_chercher_sans_resultat(base: CodeDatabase) -> None:
    indexer(base, "a.py", "print(1)")
    assert base.chercher(requete="introuvable-xyz") == []


def test_lister_respecte_la_limite(base: CodeDatabase) -> None:
    for index in range(5):
        indexer(base, f"f{index}.py", "x" * (index + 1))
    assert len(base.lister(limite=2)) == 2


# --- Description -------------------------------------------------------------

def test_description_depuis_le_docstring_python() -> None:
    contenu = '"""Politique de droits sur les outils.\n\nSuite du docstring."""\ncode = 1\n'
    assert extraire_description(contenu, "python") == "Politique de droits sur les outils."


def test_description_depuis_un_commentaire() -> None:
    assert extraire_description("#!/bin/bash\n# Indexe les sources\nls\n", "bash") == "Indexe les sources"


def test_shebang_seul_ne_devient_pas_la_description() -> None:
    """Une ligne shebang n'est pas une description (defaut corrige)."""
    assert extraire_description("#!/bin/bash\nls\n", "bash") == "ls"


def test_description_retombe_sur_la_premiere_ligne() -> None:
    assert extraire_description("select 1;\n", "sql") == "select 1;"


def test_description_vide_sur_contenu_vide() -> None:
    assert extraire_description("", "python") == ""
