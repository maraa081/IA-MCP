"""Branche Open WebUI sur l'orchestrateur (et retirre l'ancien chemin direct).

Ce que fait le script, dans l'ordre :
  1. se connecte a Open WebUI avec le compte admin (var/openwebui-admin.txt) ;
  2. declare l'orchestrateur comme fournisseur OpenAI, avec un JWT longue duree
     comme cle. C'est ce jeton que l'orchestrateur verifie a chaque requete ;
  3. retire les serveurs d'outils (mcpo) : ils permettaient d'appeler les outils
     sans JWT, sans GPO et sans audit. Les outils passent desormais par
     l'orchestrateur, donc par le pipeline complet ;
  4. verifie que les modeles de l'orchestrateur sont bien visibles.

Le JWT n'est jamais affiche : il est ecrit dans var/openwebui-token.txt (non
versionne, chmod 600) et dans la base d'Open WebUI.

Usage :
  python scripts/openwebui_connect.py --base http://127.0.0.1:3000 \
      --orchestrateur http://orchestrator-lab:8090/v1 --token <jwt>
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path


def appel(url: str, methode: str = "GET", corps: dict | None = None,
          jeton: str | None = None, timeout: float = 30.0) -> tuple[int, dict]:
    donnees = json.dumps(corps).encode() if corps is not None else None
    requete = urllib.request.Request(url, data=donnees, method=methode)
    requete.add_header("Content-Type", "application/json")
    if jeton:
        requete.add_header("Authorization", f"Bearer {jeton}")
    try:
        with urllib.request.urlopen(requete, timeout=timeout) as reponse:
            contenu = reponse.read().decode() or "{}"
            return reponse.status, json.loads(contenu)
    except urllib.error.HTTPError as erreur:
        contenu = erreur.read().decode() or "{}"
        try:
            return erreur.code, json.loads(contenu)
        except json.JSONDecodeError:
            return erreur.code, {"detail": contenu[:300]}


def identifiants(chemin: Path) -> tuple[str, str]:
    valeurs = {}
    for ligne in chemin.read_text(encoding="utf-8").splitlines():
        if "=" in ligne:
            cle, _, valeur = ligne.partition("=")
            valeurs[cle.strip()] = valeur.strip()
    if not {"email", "password"} <= valeurs.keys():
        raise SystemExit(f"  ERREUR : identifiants illisibles dans {chemin}")
    return valeurs["email"], valeurs["password"]


def main() -> None:
    analyseur = argparse.ArgumentParser(description="Branche Open WebUI sur l'orchestrateur")
    analyseur.add_argument("--base", default="http://127.0.0.1:3000")
    analyseur.add_argument("--orchestrateur", required=True,
                           help="URL OpenAI vue depuis le conteneur Open WebUI")
    analyseur.add_argument("--token", required=True, help="JWT de service (non affiche)")
    analyseur.add_argument("--credentials", default="var/openwebui-admin.txt")
    arguments = analyseur.parse_args()

    email, mot_de_passe = identifiants(Path(arguments.credentials))
    statut, corps = appel(f"{arguments.base}/api/v1/auths/signin", "POST",
                          {"email": email, "password": mot_de_passe})
    if statut != 200 or not corps.get("token"):
        raise SystemExit(f"  ERREUR : connexion admin refusee ({statut})")
    session = corps["token"]
    print("  compte admin : connecte")

    # --- 1. L'orchestrateur devient le fournisseur OpenAI unique -------------
    statut, actuel = appel(f"{arguments.base}/openai/config", jeton=session)
    if statut != 200:
        raise SystemExit(f"  ERREUR : lecture de la config OpenAI refusee ({statut}) : {actuel}")
    configs = actuel.get("OPENAI_API_CONFIGS") or {}
    statut, corps = appel(
        f"{arguments.base}/openai/config/update",
        "POST",
        {
            "ENABLE_OPENAI_API": True,
            "OPENAI_API_BASE_URLS": [arguments.orchestrateur],
            "OPENAI_API_KEYS": [arguments.token],
            "OPENAI_API_CONFIGS": configs,
        },
        jeton=session,
    )
    if statut != 200:
        raise SystemExit(f"  ERREUR : mise a jour de la config OpenAI refusee ({statut}) : {corps}")
    print(f"  fournisseur OpenAI : {arguments.orchestrateur} (cle = JWT verifie par l'orchestrateur)")

    # --- 2. Retrait des serveurs d'outils (ancien chemin sans JWT) ----------
    statut, actuel = appel(f"{arguments.base}/api/v1/configs/tool_servers", jeton=session)
    if statut == 200 and actuel.get("TOOL_SERVER_CONNECTIONS"):
        statut, corps = appel(f"{arguments.base}/api/v1/configs/tool_servers", "POST",
                              {"TOOL_SERVER_CONNECTIONS": []}, jeton=session)
        if statut not in (200, 201):
            raise SystemExit(f"  ERREUR : retrait des serveurs d'outils refuse ({statut})")
        print("  serveurs d'outils retires (les outils passent par l'orchestrateur)")
    else:
        print("  serveurs d'outils : deja absents")

    # --- 3. Verification : les modeles de l'orchestrateur sont visibles ------
    statut, modeles = appel(f"{arguments.base}/api/models", jeton=session)
    if statut != 200:
        raise SystemExit(f"  [ko] lecture des modeles refusee ({statut})")
    identifiants_modeles = [m.get("id") for m in modeles.get("data", [])]
    attendus = {"Assistant"}
    presents = attendus & set(identifiants_modeles)
    print(f"  modeles visibles : {', '.join(sorted(identifiants_modeles)) or '(aucun)'}")
    if not presents:
        raise SystemExit(
            "  [ko] aucun modele de l'orchestrateur visible : verifier son etat "
            "et le reseau Docker (./scripts/orchestrator.sh status)"
        )
    print(f"  [ok] {len(presents)}/{len(attendus)} modeles de l'orchestrateur visibles")


if __name__ == "__main__":
    main()
