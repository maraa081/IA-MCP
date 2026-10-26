"""Harnais d'evaluation du planificateur.

Deux modes :

  --mode simule
      Le planificateur est branche sur une doublure deterministe. Aucun service
      reseau n'est necessaire : ce mode sert a tester le harnais lui-meme et a
      verifier que le jeu de cas est coherent.

  --mode reel
      Le planificateur appelle la vraie gateway LiteLLM, avec le vrai modele.
      C'est lent sur CPU : utiliser --limite, --categorie et --repetitions pour
      borner le cout.

Ce que le harnais mesure :
  - le choix d'outil (global, par categorie, par role) ;
  - les arguments (sous-ensemble attendu, champs obligatoires, valeurs d'enum) ;
  - les outils interdits proposes avant la GPO ;
  - les reponses directes a tort et les outils appeles a tort ;
  - la latence p50/p95 ;
  - la stabilite sur K repetitions.

Ce qu'il ne fait pas : il n'execute pas les serveurs MCP. Pour les demandes en
deux etapes, il simule le resultat du premier outil et interroge a nouveau le
planificateur. La boucle complete du pipeline est testee ailleurs (Phase 2).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import time
import traceback
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import yaml

# Le script est lance depuis la racine du depot (`python evals/run.py`), mais
# Python ajoute le dossier du script (evals/) a sys.path, pas la racine. On
# ajoute donc explicitement la racine pour importer `orchestrator`.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from orchestrator.app.audit import AuditLogger
from orchestrator.app.config import Settings
from orchestrator.app.llm import (
    RESPOND_DIRECTLY_TOOL,
    GatewayClient,
    GatewayReply,
    LayaClient,
    LayaUnavailable,
    ToolCallRequest,
    ToolPlanner,
    assistant_tool_call_message,
    tool_result_message,
)
from orchestrator.app.mcp import ToolDescriptor
from orchestrator.app.policy import PolicyEngine, RateLimiter

# Valeurs speciales du champ attendu.outil.
REFUS_GPO = "refus_gpo"
OUTILS_SPECIAUX = {RESPOND_DIRECTLY_TOOL, REFUS_GPO}

# Etape marquee quand le modele n'a pas produit la premiere etape d'une demande
# en deux tours : on ne peut pas evaluer la seconde.
ETAPE_NON_ATTEINTE = "(etape2_non_atteinte)"

# Arguments dont la comparaison ignore la casse : ce sont des valeurs d'enum ou
# des libelles de langage, pas des references de fichier.
ARGUMENTS_INSENSIBLES_A_LA_CASSE = {"framework", "style", "audience", "language"}


# -----------------------------------------------------------------------------
# Chargement et validation du jeu de cas
# -----------------------------------------------------------------------------

def load_dataset(path: Path) -> dict[str, Any]:
    """Charge le YAML et verifie sa forme generale."""
    if not path.exists():
        raise FileNotFoundError(f"jeu de cas introuvable : {path}")
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise ValueError("le jeu de cas doit contenir une cle 'cases' (liste)")
    return data


def validate_dataset(
    dataset: dict[str, Any], catalog: list[ToolDescriptor] | None = None
) -> None:
    """Verifie les champs obligatoires et la coherence des cas.

    `catalog` permet de verifier que les noms d'outils existent reellement. Il
    est optionnel pour que la validation reste utilisable sans charger les
    serveurs MCP (par exemple dans un test rapide).
    """
    cases = dataset["cases"]
    if not cases:
        raise ValueError("le jeu de cas est vide")

    noms_catalogue = {tool.name for tool in catalog} if catalog else None
    identifiants: set[str] = set()

    for index, case in enumerate(cases, start=1):
        if not isinstance(case, dict):
            raise ValueError(f"cas #{index} : doit être un objet YAML")
        identifiant = case.get("id")
        prefixe = f"cas {identifiant or '#' + str(index)}"

        for champ in ("id", "prompt", "role", "categorie", "attendu", "commentaire"):
            if not case.get(champ):
                raise ValueError(f"{prefixe} : champ obligatoire manquant '{champ}'")

        if identifiant in identifiants:
            raise ValueError(f"{prefixe} : identifiant en double")
        identifiants.add(identifiant)

        if case["role"] not in {"analyst", "reader"}:
            raise ValueError(f"{prefixe} : role inconnu '{case['role']}'")

        attendu = case["attendu"]
        if not isinstance(attendu, dict) or not attendu.get("outil"):
            raise ValueError(f"{prefixe} : 'attendu.outil' manquant")

        outil = attendu["outil"]
        if not isinstance(outil, str):
            raise ValueError(f"{prefixe} : 'attendu.outil' doit être une chaîne")

        arguments = attendu.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ValueError(f"{prefixe} : 'attendu.arguments' doit être un objet")

        if outil not in OUTILS_SPECIAUX and noms_catalogue is not None:
            if outil not in noms_catalogue:
                raise ValueError(f"{prefixe} : outil inconnu '{outil}'")

        if outil == REFUS_GPO:
            interdits = attendu.get("outils_interdits")
            if not isinstance(interdits, list) or not interdits:
                raise ValueError(
                    f"{prefixe} : 'outils_interdits' est obligatoire pour refus_gpo"
                )
            for nom in interdits:
                if not isinstance(nom, str):
                    raise ValueError(f"{prefixe} : outil interdit non textuel")
                if noms_catalogue is not None and nom not in noms_catalogue:
                    raise ValueError(f"{prefixe} : outil interdit inconnu '{nom}'")

        etapes = attendu.get("etapes")
        if etapes is not None:
            if not isinstance(etapes, list) or len(etapes) < 2:
                raise ValueError(
                    f"{prefixe} : 'etapes' doit contenir au moins deux outils"
                )
            if etapes[0] != outil:
                raise ValueError(
                    f"{prefixe} : 'etapes[0]' doit être égal à 'attendu.outil'"
                )
            for nom in etapes:
                if noms_catalogue is not None and nom not in noms_catalogue:
                    raise ValueError(f"{prefixe} : étape inconnue '{nom}'")
            if not isinstance(attendu.get("arguments_etape_2", {}), dict):
                raise ValueError(f"{prefixe} : 'arguments_etape_2' doit être un objet")
            if not isinstance(attendu.get("resultat_premier_outil", {}), dict):
                raise ValueError(
                    f"{prefixe} : 'resultat_premier_outil' doit être un objet"
                )

        if "deterministe" in case and not isinstance(case["deterministe"], bool):
            raise ValueError(f"{prefixe} : 'deterministe' doit être un booléen")

    # Verifications transverses : la repartition imposee par la mission.
    categories = {case["categorie"] for case in cases}
    if "codedoc_code_colle" in categories:
        frameworks = {
            case["attendu"].get("arguments", {}).get("framework")
            for case in cases
            if case["categorie"] == "codedoc_code_colle"
        }
        attendus = {"diataxis", "arc42", "c4", "madr", "sphinx"}
        if not attendus.issubset(frameworks):
            manquants = sorted(attendus - frameworks)
            raise ValueError(
                "codedoc_code_colle doit couvrir les cinq frameworks ; "
                f"manquants : {', '.join(manquants)}"
            )


def parse_categories(values: list[str] | None) -> list[str]:
    """Accepte --categorie A --categorie B et --categorie A,B."""
    if not values:
        return []
    resultat: list[str] = []
    for value in values:
        for part in value.split(","):
            part = part.strip()
            if part and part not in resultat:
                resultat.append(part)
    return resultat


def select_cases(
    dataset: dict[str, Any], categories: list[str] | None, limite: int
) -> list[dict[str, Any]]:
    """Filtre par categorie, puis borne le nombre de cas."""
    if limite < 0:
        raise ValueError("--limite doit être positif ou nul (0 = tous les cas)")

    cases = list(dataset["cases"])
    if categories:
        connues = {case["categorie"] for case in cases}
        inconnues = [categorie for categorie in categories if categorie not in connues]
        if inconnues:
            raise ValueError(
                "catégorie(s) inconnue(s) : "
                + ", ".join(inconnues)
                + " ; connues : "
                + ", ".join(sorted(connues))
            )
        retenues = set(categories)
        cases = [case for case in cases if case["categorie"] in retenues]

    if limite:
        cases = cases[:limite]
    if not cases:
        raise ValueError("aucun cas ne correspond à la sélection")
    return cases


# -----------------------------------------------------------------------------
# Catalogue d'outils : les vrais schemas, sans reseau
# -----------------------------------------------------------------------------

async def build_catalog() -> list[ToolDescriptor]:
    """Construit le catalogue a partir des serveurs MCP, en processus.

    `list_tools()` ne fait aucun appel reseau : il lit les schemas declares par
    les decorateurs. C'est exactement ce que le registre decouvrirait au
    demarrage, sans dependre d'un serveur en cours d'execution.
    """
    from mcp_servers.codedb.server import mcp as codedb
    from mcp_servers.codedoc.server import mcp as codedoc
    from mcp_servers.summarizer.server import mcp as summarizer

    catalogue: list[ToolDescriptor] = []
    for serveur, instance in (
        ("summarizer", summarizer),
        ("codedoc", codedoc),
        ("codedb", codedb),
    ):
        for tool in await instance.list_tools():
            catalogue.append(
                ToolDescriptor(
                    name=tool.name,
                    server=serveur,
                    description=tool.description or "",
                    input_schema=tool.input_schema
                    or {"type": "object", "properties": {}},
                )
            )
    # Le registre de production trie par nom : on garde le meme ordre pour que
    # les schemas presentes au modele soient comparables.
    catalogue.sort(key=lambda tool: tool.name)
    return catalogue


def visible_tools(
    catalog: list[ToolDescriptor], settings: Settings, role: str
) -> list[ToolDescriptor]:
    """Outils visibles pour un role : intersection catalogue / GPO."""
    grants = PolicyEngine(settings.policies_path, RateLimiter()).grants_for([role])
    return [tool for tool in catalog if tool.name in grants]


# -----------------------------------------------------------------------------
# Doublures
# -----------------------------------------------------------------------------

class _UnusedLaya:
    """Laya non utilisee : evite de creer un client HTTP en mode simule."""

    async def choose(self, **_: Any) -> Any:
        raise LayaUnavailable("Laya n'est pas utilisée par le harnais d'évaluation")

    async def aclose(self) -> None:
        return None


class SimulatedGateway:
    """Doublure deterministe : rend l'appel attendu par le cas.

    Elle ne cherche pas a imiter un modele. Son but est de verifier que le
    harnais, les metriques et la comparaison fonctionnent sans reseau.
    """

    def __init__(self, case: dict[str, Any]) -> None:
        self.case = case
        self.calls = 0

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        **_: Any,
    ) -> GatewayReply:
        self.calls += 1
        attendu = self.case["attendu"]
        outil = attendu["outil"]

        # Refus GPO : la doublure choisit la voie sure (reponse directe) pour
        # que le harnais exerce le chemin "refus explicite". Le chemin "refus
        # par la GPO" est teste par les tests unitaires et la Phase 2.
        if outil in (RESPOND_DIRECTLY_TOOL, REFUS_GPO):
            return GatewayReply(
                model=model,
                content="",
                tool_calls=[
                    ToolCallRequest(
                        name=RESPOND_DIRECTLY_TOOL,
                        arguments={
                            "answer": "Je ne peux pas traiter cette demande avec les "
                            "outils dont je dispose."
                        },
                        call_id="sim-direct",
                    )
                ],
            )

        etapes = attendu.get("etapes") or []
        a_un_resultat = any(message.get("role") == "tool" for message in messages)
        if len(etapes) > 1 and a_un_resultat:
            outil = etapes[1]
            arguments = dict(attendu.get("arguments_etape_2") or {})
        else:
            arguments = dict(attendu.get("arguments") or {})

        # La doublure doit produire des arguments valides : sinon le harnais
        # mesurerait ses propres manques (champ obligatoire absent) au lieu du
        # modele. Les valeurs attendues du cas sont conservees ; seuls les
        # champs obligatoires absents sont completes.
        arguments = _completer_arguments_obligatoires(outil, arguments, tools)

        return GatewayReply(
            model=model,
            content="",
            tool_calls=[
                ToolCallRequest(
                    name=outil,
                    arguments=arguments,
                    call_id=f"sim-{self.calls}",
                )
            ],
        )

    async def aclose(self) -> None:
        return None


# -----------------------------------------------------------------------------
# Execution d'un cas
# -----------------------------------------------------------------------------

def _valeur_factice(cle: str) -> str:
    """Valeur de remplissage pour un argument obligatoire absent."""
    return {
        "text": "Texte simulé pour l'évaluation.",
        "code": "print('bonjour')",
        "requete": "politique",
        "reference": "orchestrator/app/policy/engine.py",
        "answer": "Réponse simulée.",
        "language": "python",
        "framework": "diataxis",
        "audience": "developpeur",
    }.get(cle, "valeur")


def _completer_arguments_obligatoires(
    outil: str, arguments: dict[str, Any], tools: list[dict[str, Any]] | None
) -> dict[str, Any]:
    """Complete les champs obligatoires du schema avec des valeurs factices.

    La doublure simule un modele qui respecte le contrat de l'outil. Sans cela,
    tous les cas simules auraient des arguments invalides (le jeu de cas ne
    verifie qu'un sous-ensemble, pas les champs obligatoires comme `text` ou
    `code`), et le harnais mesurerait sa propre doublure.
    """
    schema: dict[str, Any] | None = None
    for candidat in tools or []:
        fonction = candidat.get("function") or {}
        if fonction.get("name") == outil:
            schema = fonction.get("parameters") or {}
            break
    if not schema:
        return arguments

    completes = dict(arguments)
    proprietes = schema.get("properties") or {}
    for cle in schema.get("required") or []:
        if cle in completes and completes[cle] not in (None, ""):
            continue
        prop = proprietes.get(cle) or {}
        enum = prop.get("enum")
        if enum:
            completes[cle] = enum[0]
        elif prop.get("type") == "integer":
            completes[cle] = 3
        elif prop.get("type") == "number":
            completes[cle] = 1.0
        elif prop.get("type") == "boolean":
            completes[cle] = True
        else:
            completes[cle] = _valeur_factice(cle)
    return completes


def _absorb_plan(
    plan: Any, etapes: list[str], arguments_etapes: list[dict[str, Any]]
) -> None:
    """Ajoute les appels d'un plan aux listes d'etapes."""
    if plan.calls:
        for call in plan.calls:
            etapes.append(call.tool)
            arguments_etapes.append(dict(call.arguments or {}))
    else:
        etapes.append(RESPOND_DIRECTLY_TOOL)
        arguments_etapes.append({})


def _check_arguments(
    etapes: list[str],
    arguments_etapes: list[dict[str, Any]],
    schemas: dict[str, dict[str, Any]],
) -> tuple[bool, list[dict[str, Any]]]:
    """Verifie les champs obligatoires et les valeurs d'enum.

    Retourne (arguments_valides, liste_des_valeurs_hors_enum). Un outil inconnu
    (nom invente par le modele) rend les arguments invalides : il n'y a pas de
    schema pour les valider.
    """
    valides = True
    hors_enum: list[dict[str, Any]] = []

    for outil, arguments in zip(etapes, arguments_etapes):
        if outil in (RESPOND_DIRECTLY_TOOL, ETAPE_NON_ATTEINTE):
            continue
        schema = schemas.get(outil)
        if schema is None:
            valides = False
            continue

        for cle in schema.get("required") or []:
            if cle not in arguments or arguments[cle] in (None, ""):
                valides = False

        proprietes = schema.get("properties") or {}
        for cle, valeur in arguments.items():
            enum = (proprietes.get(cle) or {}).get("enum")
            if enum is not None and valeur not in enum:
                valides = False
                hors_enum.append({"outil": outil, "argument": cle, "valeur": valeur})

    return valides, hors_enum


def _value_matches(cle: str, attendu: Any, obtenu: Any) -> bool:
    """Compare une valeur attendue a une valeur proposee par le modele."""
    if isinstance(attendu, bool):
        return obtenu is attendu
    if isinstance(attendu, int):
        try:
            return int(obtenu) == attendu
        except (TypeError, ValueError):
            return False
    if isinstance(attendu, str):
        if cle in ARGUMENTS_INSENSIBLES_A_LA_CASSE:
            return str(obtenu).strip().lower() == attendu.strip().lower()
        return str(obtenu).strip() == attendu.strip()
    return obtenu == attendu


def _expected_arguments_match(
    attendu: dict[str, Any], obtenu: dict[str, Any]
) -> bool:
    """Verifie un sous-ensemble d'arguments attendus."""
    for cle, valeur in (attendu or {}).items():
        if cle not in obtenu:
            return False
        if not _value_matches(cle, valeur, obtenu[cle]):
            return False
    return True


def _signature(
    etapes: list[str], arguments_etapes: list[dict[str, Any]]
) -> tuple[str, str, str]:
    """Signatures pour la stabilite : complete, outils seuls, arguments seuls."""
    complete = json.dumps(
        {"etapes": etapes, "arguments": arguments_etapes},
        sort_keys=True,
        ensure_ascii=False,
    )
    outils = json.dumps(etapes, ensure_ascii=False)
    arguments = json.dumps(arguments_etapes, sort_keys=True, ensure_ascii=False)
    return complete, outils, arguments


async def run_once(
    case: dict[str, Any],
    *,
    planner: ToolPlanner,
    settings: Settings,
    visible: list[ToolDescriptor],
    allowed_names: set[str],
    schemas: dict[str, dict[str, Any]],
    timeout: float,
) -> dict[str, Any]:
    """Execute une repetition d'un cas et retourne une trace normalisee."""
    attendu = case["attendu"]
    outil_attendu = attendu["outil"]
    etapes_attendues = attendu.get("etapes") or []
    started = time.perf_counter()

    modes: list[str] = []
    etapes: list[str] = []
    arguments_etapes: list[dict[str, Any]] = []
    erreur: str | None = None
    premier_plan: Any = None

    try:
        premier_plan = await asyncio.wait_for(
            planner.plan(
                messages=[{"role": "user", "content": case["prompt"]}],
                tools=visible,
            ),
            timeout=timeout,
        )
        modes.append(premier_plan.mode)
        if premier_plan.mode.endswith("_error"):
            erreur = premier_plan.content or "erreur de planification"
        _absorb_plan(premier_plan, etapes, arguments_etapes)

        # Demande en deux etapes : on simule le resultat du premier outil, puis
        # on redemande une planification. C'est la meme mecanique que le
        # pipeline, sans executer le serveur MCP.
        if len(etapes_attendues) > 1:
            if etapes == [etapes_attendues[0]]:
                premier_appel = premier_plan.calls[0]
                resultat = attendu.get("resultat_premier_outil") or {}
                conversation = [
                    {"role": "user", "content": case["prompt"]},
                    assistant_tool_call_message(premier_plan.calls),
                    tool_result_message(
                        premier_appel,
                        json.dumps(resultat, ensure_ascii=False),
                    ),
                ]
                second_plan = await asyncio.wait_for(
                    planner.plan(messages=conversation, tools=visible),
                    timeout=timeout,
                )
                modes.append(second_plan.mode)
                if second_plan.mode.endswith("_error"):
                    erreur = second_plan.content or "erreur de planification (tour 2)"
                _absorb_plan(second_plan, etapes, arguments_etapes)
            else:
                # Le modele n'a pas fait la premiere etape : la seconde n'est
                # pas evaluable. On le marque explicitement.
                etapes.append(ETAPE_NON_ATTEINTE)
                arguments_etapes.append({})
    except asyncio.TimeoutError:
        erreur = f"timeout après {timeout:g}s"
        if not etapes:
            etapes = [RESPOND_DIRECTLY_TOOL]
            arguments_etapes = [{}]
    except Exception as exc:  # noqa: BLE001 - on veut une trace, pas un crash
        erreur = f"{type(exc).__name__}: {exc}"
        if not etapes:
            etapes = [RESPOND_DIRECTLY_TOOL]
            arguments_etapes = [{}]

    latence_ms = int((time.perf_counter() - started) * 1000)
    outil = etapes[0] if etapes else RESPOND_DIRECTLY_TOOL
    arguments = arguments_etapes[0] if arguments_etapes else {}

    arguments_valides, enum_invalide = _check_arguments(
        etapes, arguments_etapes, schemas
    )
    outil_interdit_propose = any(
        nom not in allowed_names
        for nom in etapes
        if nom not in (RESPOND_DIRECTLY_TOOL, ETAPE_NON_ATTEINTE)
    )

    refus: str | None = None
    succes_outil = False

    if outil_attendu == REFUS_GPO:
        interdits = set(attendu.get("outils_interdits") or [])
        proposes_interdits = [nom for nom in etapes if nom in interdits]
        executes_interdits: list[str] = []

        # On applique la GPO aux appels proposes pour distinguer un refus
        # explicite (le modele repond), un refus par la GPO (le modele propose
        # l'outil interdit, la politique l'arrete) et une substitution.
        if premier_plan is not None and premier_plan.calls:
            policy = PolicyEngine(settings.policies_path, RateLimiter())
            for call in premier_plan.calls:
                if call.tool not in interdits:
                    continue
                decision = policy.check_tool_call(
                    sub="eval",
                    roles=[case["role"]],
                    tool=call.tool,
                    arguments=dict(call.arguments or {}),
                    calls_so_far=0,
                )
                if decision.allowed:
                    executes_interdits.append(call.tool)

        if executes_interdits:
            refus = "echec_critique"
            succes_outil = False
        elif proposes_interdits:
            refus = "refus_par_gpo"
            succes_outil = True
        elif outil == RESPOND_DIRECTLY_TOOL and premier_plan is not None and premier_plan.content.strip():
            refus = "refus_explicite"
            succes_outil = True
        elif outil == RESPOND_DIRECTLY_TOOL:
            refus = "reponse_vide"
            succes_outil = False
        else:
            refus = "substitution"
            succes_outil = False

        if erreur:
            refus = "erreur"

    elif outil_attendu == RESPOND_DIRECTLY_TOOL:
        succes_outil = (
            outil == RESPOND_DIRECTLY_TOOL
            and premier_plan is not None
            and bool(premier_plan.content.strip())
            and not premier_plan.calls
        )

    else:
        if etapes_attendues:
            succes_outil = etapes == etapes_attendues
        else:
            succes_outil = (
                outil == outil_attendu
                and premier_plan is not None
                and bool(premier_plan.calls)
            )

    # Les arguments ne sont evalues que si le bon outil a ete choisi : comparer
    # les arguments d'un mauvais outil n'aurait pas de sens.
    succes_arguments: bool | None
    if outil_attendu in (RESPOND_DIRECTLY_TOOL, REFUS_GPO):
        succes_arguments = None
    elif not succes_outil:
        succes_arguments = None
    else:
        ok = _expected_arguments_match(
            attendu.get("arguments") or {}, arguments_etapes[0] if arguments_etapes else {}
        )
        if etapes_attendues:
            ok = ok and _expected_arguments_match(
                attendu.get("arguments_etape_2") or {},
                arguments_etapes[1] if len(arguments_etapes) > 1 else {},
            )
        succes_arguments = ok and arguments_valides

    signature, signature_outil, signature_arguments = _signature(
        etapes, arguments_etapes
    )

    return {
        "outil": outil,
        "outils": etapes,
        "arguments": arguments,
        "arguments_etapes": arguments_etapes,
        "etapes": etapes,
        "modes": modes,
        "mode": modes[0] if modes else "none",
        "latence_ms": latence_ms,
        "succes_outil": succes_outil,
        "succes_arguments": succes_arguments,
        "arguments_valides": arguments_valides,
        "enum_invalide": enum_invalide,
        "outil_interdit_propose": outil_interdit_propose,
        "refus": refus,
        "erreur": erreur,
        "signature": signature,
        "signature_outil": signature_outil,
        "signature_arguments": signature_arguments,
    }


def _mode_fraction(values: list[str]) -> float | None:
    """Part de la valeur la plus frequente (stabilite)."""
    if len(values) < 2:
        return None
    return Counter(values).most_common(1)[0][1] / len(values)


async def run_case(
    case: dict[str, Any],
    *,
    planner: ToolPlanner,
    settings: Settings,
    catalog: list[ToolDescriptor],
    mode: str,
    repetitions: int,
    timeout: float,
    probe: bool,
) -> dict[str, Any]:
    """Execute toutes les repetitions d'un cas, puis la sonde reader si utile."""
    attendu = case["attendu"]
    grants = PolicyEngine(settings.policies_path, RateLimiter()).grants_for(
        [case["role"]]
    )
    visible = [tool for tool in catalog if tool.name in grants]
    allowed_names = set(grants.keys())
    schemas = {tool.name: tool.input_schema for tool in visible}

    executions = [
        await run_once(
            case,
            planner=planner,
            settings=settings,
            visible=visible,
            allowed_names=allowed_names,
            schemas=schemas,
            timeout=timeout,
        )
        for _ in range(repetitions)
    ]

    # Sonde : on montre au modele le catalogue COMPLET pour mesurer ce qu'il
    # proposerait avant la GPO. Ce n'est pas le comportement de production ;
    # c'est une mesure de tentation, reservee au mode reel.
    sonde: dict[str, Any] | None = None
    if probe and mode == "reel" and attendu["outil"] == REFUS_GPO:
        started = time.perf_counter()
        try:
            plan_sonde = await asyncio.wait_for(
                planner.plan(
                    messages=[{"role": "user", "content": case["prompt"]}],
                    tools=catalog,
                ),
                timeout=timeout,
            )
            sonde = {
                "outils": [call.tool for call in plan_sonde.calls],
                "outil_interdit_propose": any(
                    call.tool not in allowed_names
                    for call in plan_sonde.calls
                    if call.tool != RESPOND_DIRECTLY_TOOL
                ),
                "mode": plan_sonde.mode,
                "latence_ms": int((time.perf_counter() - started) * 1000),
            }
        except Exception as exc:  # noqa: BLE001
            sonde = {
                "erreur": f"{type(exc).__name__}: {exc}",
                "latence_ms": int((time.perf_counter() - started) * 1000),
            }

    stabilite = _mode_fraction([e["signature"] for e in executions])
    stabilite_outil = _mode_fraction([e["signature_outil"] for e in executions])
    stabilite_arguments = _mode_fraction(
        [e["signature_arguments"] for e in executions]
    )

    attendu_resume = {
        cle: valeur
        for cle, valeur in attendu.items()
        if cle != "resultat_premier_outil"
    }

    return {
        "id": case["id"],
        "categorie": case["categorie"],
        "role": case["role"],
        "deterministe": bool(case.get("deterministe")),
        "attendu": attendu_resume,
        "prompt_fingerprint": AuditLogger.fingerprint(case["prompt"]),
        "prompt_chars": len(case["prompt"]),
        "executions": executions,
        "stabilite": stabilite,
        "stabilite_outil": stabilite_outil,
        "stabilite_arguments": stabilite_arguments,
        "sonde": sonde,
    }


# -----------------------------------------------------------------------------
# Metriques
# -----------------------------------------------------------------------------

def _rate(numerateur: int, denominateur: int) -> float | None:
    return None if denominateur == 0 else numerateur / denominateur


def _mean_bool(values: list[bool]) -> float | None:
    if not values:
        return None
    return sum(1 for value in values if value) / len(values)


def _mean_float(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _percentile(values: list[int], ratio: float) -> int | None:
    if not values:
        return None
    ordonnees = sorted(values)
    index = min(len(ordonnees) - 1, max(0, round(ratio * (len(ordonnees) - 1))))
    return ordonnees[index]


def aggregate(case_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Agrege des resultats de cas en metriques."""
    lignes = [
        (case, execution)
        for case in case_results
        for execution in case["executions"]
    ]
    executions = [execution for _, execution in lignes]
    total = len(executions)

    hors_deterministes = [
        execution
        for case, execution in lignes
        if not case["deterministe"]
    ]

    valeurs_arguments = [
        execution["succes_arguments"]
        for execution in executions
        if execution["succes_arguments"] is not None
    ]

    avec_outil_reel = [
        execution
        for execution in executions
        if execution["etapes"]
        and execution["etapes"][0] not in (RESPOND_DIRECTLY_TOOL, ETAPE_NON_ATTEINTE)
    ]

    attendus_outil = [
        (case, execution)
        for case, execution in lignes
        if case["attendu"]["outil"] not in (RESPOND_DIRECTLY_TOOL, REFUS_GPO)
    ]
    reponses_directes_a_tort = [
        execution
        for _, execution in attendus_outil
        if execution["outil"] == RESPOND_DIRECTLY_TOOL
    ]

    attendus_sans_outil = [
        (case, execution)
        for case, execution in lignes
        if case["attendu"]["outil"] in (RESPOND_DIRECTLY_TOOL, REFUS_GPO)
    ]
    outils_appeles_a_tort = []
    for case, execution in attendus_sans_outil:
        if case["attendu"]["outil"] == RESPOND_DIRECTLY_TOOL:
            if execution["outil"] not in (RESPOND_DIRECTLY_TOOL, ETAPE_NON_ATTEINTE):
                outils_appeles_a_tort.append(execution)
        else:  # refus_gpo : seule une substitution est un outil reellement execute
            if execution.get("refus") in {"substitution", "echec_critique"}:
                outils_appeles_a_tort.append(execution)

    refus_lignes = [
        execution
        for case, execution in lignes
        if case["attendu"]["outil"] == REFUS_GPO
    ]
    refus_gpo = {
        "total": len(refus_lignes),
        "explicite": sum(1 for e in refus_lignes if e.get("refus") == "refus_explicite"),
        "par_gpo": sum(1 for e in refus_lignes if e.get("refus") == "refus_par_gpo"),
        "substitution": sum(1 for e in refus_lignes if e.get("refus") == "substitution"),
        "critique": sum(1 for e in refus_lignes if e.get("refus") == "echec_critique"),
        "reponse_vide": sum(1 for e in refus_lignes if e.get("refus") == "reponse_vide"),
        "erreur": sum(1 for e in refus_lignes if e.get("refus") == "erreur"),
    }

    latences = [execution["latence_ms"] for execution in executions]
    sondes = [case["sonde"] for case in case_results if case.get("sonde")]

    return {
        "cas": len(case_results),
        "executions": total,
        "erreurs": sum(1 for execution in executions if execution["erreur"]),
        "cas_deterministes": sum(1 for case in case_results if case["deterministe"]),
        "exactitude_outil": _mean_bool([e["succes_outil"] for e in executions]),
        "exactitude_outil_hors_deterministes": _mean_bool(
            [e["succes_outil"] for e in hors_deterministes]
        ),
        "exactitude_arguments": _mean_bool(valeurs_arguments),
        "arguments_valides": _mean_bool(
            [e["arguments_valides"] for e in avec_outil_reel]
        ),
        "enum_invalide": _rate(
            sum(1 for e in avec_outil_reel if e["enum_invalide"]),
            len(avec_outil_reel),
        ),
        "outil_interdit_propose": _mean_bool(
            [e["outil_interdit_propose"] for e in executions]
        ),
        "reponse_directe_a_tort": _rate(
            len(reponses_directes_a_tort), len(attendus_outil)
        ),
        "outil_appele_a_tort": _rate(
            len(outils_appeles_a_tort), len(attendus_sans_outil)
        ),
        "refus_gpo": refus_gpo,
        "latence_ms": {
            "n": len(latences),
            "p50": _percentile(latences, 0.50),
            "p95": _percentile(latences, 0.95),
        },
        "stabilite": _mean_float(
            [case["stabilite"] for case in case_results if case["stabilite"] is not None]
        ),
        "stabilite_outil": _mean_float(
            [
                case["stabilite_outil"]
                for case in case_results
                if case["stabilite_outil"] is not None
            ]
        ),
        "stabilite_arguments": _mean_float(
            [
                case["stabilite_arguments"]
                for case in case_results
                if case["stabilite_arguments"] is not None
            ]
        ),
        "sonde_reader": {
            "cas": len(sondes),
            "outil_interdit_propose": _rate(
                sum(1 for sonde in sondes if sonde.get("outil_interdit_propose")),
                len(sondes),
            ),
        },
    }


def aggregate_all(case_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Metriques globales, par categorie et par role."""
    categories = sorted({case["categorie"] for case in case_results})
    roles = sorted({case["role"] for case in case_results})
    return {
        "global": aggregate(case_results),
        "par_categorie": {
            categorie: aggregate(
                [case for case in case_results if case["categorie"] == categorie]
            )
            for categorie in categories
        },
        "par_role": {
            role: aggregate([case for case in case_results if case["role"] == role])
            for role in roles
        },
    }


# -----------------------------------------------------------------------------
# Execution d'une campagne
# -----------------------------------------------------------------------------

async def evaluate(
    cases: list[dict[str, Any]],
    catalog: list[ToolDescriptor],
    settings: Settings,
    *,
    mode: str,
    repetitions: int,
    timeout: float,
    probe: bool,
    planner: ToolPlanner | None = None,
    simulated_gateway_factory: Callable[[dict[str, Any]], Any] | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    """Execute les cas et retourne les resultats detailles et les metriques."""
    if mode == "reel" and planner is None:
        raise ValueError("le mode reel exige un planificateur (gateway réelle)")

    fabrique = simulated_gateway_factory or SimulatedGateway
    resultats: list[dict[str, Any]] = []

    for case in cases:
        if mode == "reel":
            planificateur = planner
        else:
            planificateur = ToolPlanner(
                fabrique(case), settings, laya=_UnusedLaya()
            )

        resultat = await run_case(
            case,
            planner=planificateur,  # type: ignore[arg-type]
            settings=settings,
            catalog=catalog,
            mode=mode,
            repetitions=repetitions,
            timeout=timeout,
            probe=probe,
        )
        resultats.append(resultat)

        if verbose:
            execution = resultat["executions"][0]
            etat = "ok" if execution["succes_outil"] else "KO"
            print(
                f"  {case['id']:<24} -> {execution['outil']:<22} "
                f"{etat:<2} {execution['latence_ms']} ms"
            )

    return {"cas": resultats, "metriques": aggregate_all(resultats)}


# -----------------------------------------------------------------------------
# Affichage
# -----------------------------------------------------------------------------

def _pct(valeur: float | None) -> str:
    return "-" if valeur is None else f"{valeur * 100:.0f}%"


def _num(valeur: float | None) -> str:
    return "-" if valeur is None else f"{valeur:.2f}"


def _latence(metriques: dict[str, Any]) -> str:
    latence = metriques.get("latence_ms") or {}
    if latence.get("p50") is None:
        return "-"
    return f"{latence['p50']}/{latence['p95']}"


def render_table(result: dict[str, Any], path: Path | None = None) -> None:
    """Affiche un tableau lisible des metriques."""
    meta = result["meta"]
    metriques = result["metriques"]

    print()
    print(f"Évaluation du planificateur — mode {meta['mode']}")
    print(
        f"Modèle : {meta['modele']} | mode planificateur : {meta['planner_mode']} | "
        f"répétitions : {meta['repetitions']} | cas : {meta['cas_selectionnes']}"
    )
    print(
        f"Dataset : {meta['dataset']} (v{meta['dataset_version']}) | "
        f"commit : {meta.get('git_commit') or '-'}"
    )

    entetes = ["Catégorie", "Cas", "Outil", "Args", "Enum", "Interdit", "p50/p95 ms", "Stab."]
    largeurs = [28, 4, 6, 5, 5, 9, 12, 5]

    def ligne(nom: str, agregat: dict[str, Any]) -> str:
        cellules = [
            nom[:28].ljust(largeurs[0]),
            str(agregat["cas"]).rjust(largeurs[1]),
            _pct(agregat["exactitude_outil"]).rjust(largeurs[2]),
            _pct(agregat["exactitude_arguments"]).rjust(largeurs[3]),
            _pct(agregat["enum_invalide"]).rjust(largeurs[4]),
            _pct(agregat["outil_interdit_propose"]).rjust(largeurs[5]),
            _latence(agregat).rjust(largeurs[6]),
            _num(agregat["stabilite"]).rjust(largeurs[7]),
        ]
        return "  ".join(cellules)

    print()
    print("  ".join(h.ljust(w) for h, w in zip(entetes, largeurs)))
    print("  ".join("-" * w for w in largeurs))
    for categorie, agregat in metriques["par_categorie"].items():
        print(ligne(categorie, agregat))
    print(ligne("GLOBAL", metriques["global"]))

    global_ = metriques["global"]
    print()
    print(
        "Cas déterministes (pré-filtre, hors mesure du modèle) : "
        f"{global_['cas_deterministes']}"
    )
    print(
        "Exactitude outil hors déterministes : "
        f"{_pct(global_['exactitude_outil_hors_deterministes'])}"
    )
    print(
        f"Réponse directe à tort : {_pct(global_['reponse_directe_a_tort'])} | "
        f"Outil appelé à tort : {_pct(global_['outil_appele_a_tort'])}"
    )
    refus = global_["refus_gpo"]
    print(
        "Refus GPO : "
        f"explicite {refus['explicite']} | par GPO {refus['par_gpo']} | "
        f"substitution {refus['substitution']} | critique {refus['critique']} | "
        f"réponse vide {refus['reponse_vide']} | erreur {refus['erreur']} "
        f"(total {refus['total']})"
    )
    print(f"Erreurs d'exécution : {global_['erreurs']} / {global_['executions']}")
    sonde = global_.get("sonde_reader") or {}
    if sonde.get("cas"):
        print(
            "Sonde reader (catalogue complet) : "
            f"{_pct(sonde['outil_interdit_propose'])} d'outil interdit proposé "
            f"sur {sonde['cas']} cas"
        )
    else:
        print("Sonde reader : non exécutée")
    if path is not None:
        print(f"Fichier : {meta.get('fichier') or path}")
    print()
    print(
        "Rappel : p95 sur quelques cas n'est pas une mesure statistique. "
        "Un petit échantillon ne permet pas de conclure à une amélioration générale."
    )
    print()


# -----------------------------------------------------------------------------
# Entree du programme
# -----------------------------------------------------------------------------

def _safe_model_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "modele"


def _relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _git_commit() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return completed.stdout.strip() or None
    except Exception:  # noqa: BLE001 - la traçabilite ne doit pas casser le run
        return None


def write_results(result: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Évalue le planificateur sur un jeu de cas.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--mode", choices=["simule", "reel"], default="simule")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=PROJECT_ROOT / "evals" / "dataset.yaml",
        help="Jeu de cas YAML",
    )
    parser.add_argument(
        "--limite",
        type=int,
        default=0,
        help="Nombre maximal de cas (0 = tous)",
    )
    parser.add_argument(
        "--categorie",
        action="append",
        default=None,
        help="Filtrer par catégorie (répétable ou séparée par des virgules)",
    )
    parser.add_argument(
        "--modele",
        default=None,
        help="Alias de modèle gateway (défaut : planner ; 'simule' en mode simulé)",
    )
    parser.add_argument(
        "--planner-mode",
        choices=["native", "json", "auto", "laya"],
        default=None,
        help="Force la stratégie de planification",
    )
    parser.add_argument(
        "--repetitions",
        type=int,
        default=1,
        help="Répétitions par cas (mesure de stabilité)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Timeout par appel de planification, en secondes",
    )
    parser.add_argument(
        "--sortie",
        type=Path,
        default=None,
        help="Fichier JSON de sortie",
    )
    parser.add_argument(
        "--sans-sonde",
        action="store_true",
        help="Désactive la sonde reader (catalogue complet) en mode réel",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Affiche le résultat JSON au lieu du tableau",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Affiche chaque cas au fil de l'exécution",
    )
    return parser.parse_args(argv)


async def _main_async(args: argparse.Namespace) -> int:
    started = time.perf_counter()

    dataset = load_dataset(args.dataset)
    catalog = await build_catalog()
    validate_dataset(dataset, catalog)

    categories = parse_categories(args.categorie)
    cases = select_cases(dataset, categories, args.limite)

    settings = Settings()
    modele = args.modele or ("simule" if args.mode == "simule" else settings.planner_model)
    settings = settings.model_copy(update={"planner_model": modele})

    planner_mode = args.planner_mode or (
        "native" if args.mode == "simule" else settings.planner_mode
    )
    if args.mode == "simule" and planner_mode == "laya":
        raise ValueError("le mode simule ne peut pas utiliser Laya")
    settings = settings.model_copy(update={"planner_mode": planner_mode})

    timeout = args.timeout
    if timeout is None:
        timeout = 10.0 if args.mode == "simule" else 300.0
    if timeout <= 0:
        raise ValueError("--timeout doit être strictement positif")
    if args.repetitions < 1:
        raise ValueError("--repetitions doit être au moins 1")

    probe = bool(args.mode == "reel" and not args.sans_sonde)
    gateway: GatewayClient | None = None
    laya: Any = None

    try:
        planner: ToolPlanner | None = None
        if args.mode == "reel":
            gateway = GatewayClient(settings)
            if not await gateway.health():
                print(
                    f"Gateway injoignable sur {settings.gateway_url}. "
                    "Démarrer la pile avec ./scripts/up.sh, puis relancer.",
                    file=sys.stderr,
                )
                return 2
            if planner_mode == "laya":
                laya = LayaClient(settings)
            else:
                laya = _UnusedLaya()
            planner = ToolPlanner(gateway, settings, laya=laya)

        resultats = await evaluate(
            cases,
            catalog,
            settings,
            mode=args.mode,
            repetitions=args.repetitions,
            timeout=timeout,
            probe=probe,
            planner=planner,
            verbose=args.verbose,
        )

        duree_s = time.perf_counter() - started
        jour = datetime.now().strftime("%Y-%m-%d")
        chemin = args.sortie or (
            PROJECT_ROOT / "var" / "evals" / f"{jour}-{_safe_model_name(modele)}.json"
        )

        # Vue des outils par role, pour documenter ce que le modele a reellement
        # pu voir (et rendre la comparaison de campagnes plus lisible).
        policy = PolicyEngine(settings.policies_path, RateLimiter())
        outils_visibles = {
            role: [
                tool.name
                for tool in catalog
                if tool.name in policy.grants_for([role])
            ]
            for role in sorted({case["role"] for case in cases})
        }

        meta = {
            "date": datetime.now().astimezone().isoformat(timespec="seconds"),
            "mode": args.mode,
            "modele": modele,
            "planner_mode": planner_mode,
            "dataset": _relative(args.dataset),
            "dataset_version": dataset.get("version"),
            "categories": categories
            or sorted({case["categorie"] for case in cases}),
            "limite": args.limite,
            "repetitions": args.repetitions,
            "timeout_s": timeout,
            "sonde_reader": probe,
            "git_commit": _git_commit(),
            "duree_s": round(duree_s, 2),
            "cas_selectionnes": len(cases),
            "catalogue": [tool.name for tool in catalog],
            "outils_visibles": outils_visibles,
            "fichier": _relative(chemin),
        }

        resultat_complet = {
            "meta": meta,
            "metriques": resultats["metriques"],
            "cas": resultats["cas"],
        }
        write_results(resultat_complet, chemin)

        if args.json:
            print(json.dumps(resultat_complet, indent=2, ensure_ascii=False))
        else:
            render_table(resultat_complet, chemin)

        global_ = resultat_complet["metriques"]["global"]
        if global_["executions"] and global_["erreurs"] == global_["executions"]:
            print(
                "Toutes les exécutions ont échoué : vérifier la gateway, le modèle "
                "et les journaux.",
                file=sys.stderr,
            )
            return 1
        return 0

    finally:
        if gateway is not None:
            await gateway.aclose()
        if laya is not None and hasattr(laya, "aclose"):
            await laya.aclose()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        print("Interrompu.", file=sys.stderr)
        return 130
    except (ValueError, FileNotFoundError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI : message clair, trace si -v
        print(f"Erreur inattendue : {exc}", file=sys.stderr)
        if args.verbose:
            traceback.print_exc()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
