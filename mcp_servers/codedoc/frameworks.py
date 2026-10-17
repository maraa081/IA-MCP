"""Registre des frameworks de documentation technique.

Idee directrice : on ne demande pas au modele d'"inventer une doc". On lui
impose une structure issue d'un framework etabli et reconnu, section par
section. Resultat : une documentation homogene d'un fichier a l'autre, et un
rendu verifiable (il manque une section = c'est visible).

Pour ajouter un framework : ajouter une entree dans FRAMEWORKS. Aucune autre
modification n'est necessaire (ni dans le serveur, ni dans la GPO : penser
seulement a l'autoriser dans config/policies.yaml si on veut la restreindre).

Ce module porte aussi la verification de conformite (find_missing_sections),
utilisee par les deux serveurs MCP qui produisent de la documentation : celui a
partir d'un code fourni, et celui a partir d'un code lu en base.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class DocFramework:
    """Description declarative d'un framework de documentation."""

    key: str
    label: str
    origin: str
    best_for: str
    sections: list[str] = field(default_factory=list)
    output_format: str = "markdown"
    extra_instructions: str = ""

    def render_structure(self) -> str:
        """Rend la liste des sections attendues, numerotee."""
        return "\n".join(
            f"{index}. {section}" for index, section in enumerate(self.sections, start=1)
        )


FRAMEWORKS: dict[str, DocFramework] = {

    "diataxis": DocFramework(
        key="diataxis",
        label="Diataxis",
        origin="Daniele Procida - diataxis.fr",
        best_for="Documentation destinee aux utilisateurs d'un composant",
        sections=[
            "Tutoriel : parcours pas a pas pour un debutant, avec un resultat visible a la fin",
            "Guide pratique (how-to) : reponse a un objectif precis, orientee action",
            "Reference : description exhaustive et neutre de l'interface publique",
            "Explication : pourquoi ce code est concu ainsi, compromis et alternatives",
        ],
        extra_instructions=(
            "Les quatre sections repondent a des besoins differents : ne les melange "
            "pas. La reference decrit ce qui EST, l'explication dit POURQUOI."
        ),
    ),

    "arc42": DocFramework(
        key="arc42",
        label="arc42",
        origin="Gernot Starke et al. - arc42.org",
        best_for="Documentation d'architecture systeme",
        sections=[
            "1. Introduction et objectifs",
            "2. Contraintes",
            "3. Contexte et perimetre",
            "4. Strategie de solution",
            "5. Blocs de construction (vue composants)",
            "6. Vue d'execution (scenarios dynamiques)",
            "7. Vue de deploiement",
            "8. Concepts transverses",
            "9. Decisions d'architecture",
            "10. Exigences de qualite",
            "11. Risques et dettes techniques",
            "12. Glossaire",
        ],
        extra_instructions=(
            "Si une section ne s'applique pas au code fourni, ecris explicitement "
            "'Sans objet pour ce composant' plutot que de l'omettre : c'est une "
            "exigence du framework arc42."
        ),
    ),

    "c4": DocFramework(
        key="c4",
        label="C4 model",
        origin="Simon Brown - c4model.com",
        best_for="Documentation d'architecture par niveaux de zoom",
        sections=[
            "Niveau 1 - Contexte : le systeme et ses acteurs externes",
            "Niveau 2 - Conteneurs : les processus et magasins de donnees",
            "Niveau 3 - Composants : les modules internes du composant etudie",
            "Niveau 4 - Code : seulement si le code livre le justifie (classes, fonctions cles)",
            "Diagrammes : un diagramme Mermaid par niveau documente",
        ],
        extra_instructions=(
            "Les diagrammes sont obligatoires et doivent etre ecrits en Mermaid "
            "(flowchart pour les niveaux 1 a 3). Chaque element du diagramme "
            "apparait ensuite dans la legende : nom, responsabilite, technologie."
        ),
    ),

    "madr": DocFramework(
        key="madr",
        label="MADR (Markdown Any Decision Records)",
        origin="adr.github.io - format MADR",
        best_for="Tracer les decisions techniques structurantes du code",
        sections=[
            "Titre et metadonnees (statut, date, decideurs)",
            "Contexte et enonce du probleme",
            "Options envisagees",
            "Decision",
            "Consequences (positives, negatives, risques)",
            "Conformite et notes de mise en oeuvre",
        ],
        extra_instructions=(
            "Le statut doit etre l'un de : proposed, accepted, deprecated, superseded. "
            "Les options non retenues doivent inclure la raison de leur rejet."
        ),
    ),

    "sphinx": DocFramework(
        key="sphinx",
        label="Sphinx / autodoc",
        origin="Sphinx (reStructuredText) - google/numpy docstring style",
        best_for="Documentation integree au code, generee par un outil",
        sections=[
            "En-tete de module (docstring de module : role, contenus)",
            "Liste des symboles publics",
            "Pour chaque fonction ou classe : signature, parametres, valeurs de retour, exceptions",
            "Exemples d'utilisation verifiables (doctest)",
            "Notes d'implementation",
        ],
        output_format="restructuredtext",
        extra_instructions=(
            "Produis du reStructuredText valide pour Sphinx : directive .. code-block:: "
            "pour le code, :param x:, :returns:, :raises:. Pas de titres Markdown (#) : "
            "utilise les soulignements rST (=== et ---)."
        ),
    ),
}


def get_framework(key: str) -> DocFramework | None:
    """Retourne le framework demande, ou None s'il n'existe pas."""
    return FRAMEWORKS.get((key or "").strip().lower())


def available_keys() -> list[str]:
    return sorted(FRAMEWORKS.keys())


# --- Verification de conformite ----------------------------------------------

# Mots trop courants pour servir de signature de section.
STOPWORDS = {
    "pour", "dans", "avec", "cette", "leurs", "plus", "sans", "elle", "sont",
    "voir", "tout", "tous", "une", "des", "les", "par", "sur", "que", "qui",
    "niveau", "seulement", "avant", "apres", "entre",
}


def strip_accents(value: str) -> str:
    return value.translate(str.maketrans("àâäéèêëîïôöùûüç", "aaaeeeeiioouuuc"))


def find_missing_sections(sections: list[str], documentation: str) -> list[str]:
    """Retourne les sections du framework absentes du document produit.

    Le modele peut tres bien oublier une section : on ne le croit pas sur parole,
    on verifie dans le texte. C'est le role du code, pas du modele, de garantir
    la conformite au framework.

    La comparaison porte sur les mots significatifs du libelle de section, ce qui
    tolere les variations de formulation, la numerotation et les accents.
    """
    texte = strip_accents(documentation.lower())
    manquantes: list[str] = []

    for section in sections:
        mots = [
            mot
            for mot in re.findall(r"[a-z0-9]{4,}", strip_accents(section.lower()))
            if mot not in STOPWORDS
        ]
        if mots and not any(mot in texte for mot in mots):
            manquantes.append(section)

    return manquantes
