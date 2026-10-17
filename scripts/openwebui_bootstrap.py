"""Prepare Open WebUI pour le labo : premier compte admin, puis declaration des
serveurs d'outils (les trois serveurs MCP publies par la passerelle mcpo).

Pourquoi un script : ces deux operations se font normalement a la souris dans
l'interface d'administration. Les faire ici rend l'installation reproductible, et
surtout evite de recopier a la main des URL et une cle.

Le mot de passe est genere ici, ecrit dans var/ (jamais versionne), et n'est
jamais affiche : meme principe que scripts/setup_db.sh pour la base.

Usage :
  python scripts/openwebui_bootstrap.py --base http://127.0.0.1:3000 \
      --ip-wsl 172.31.13.28 --mcpo-key <cle> --credentials var/openwebui-admin.txt
"""

from __future__ import annotations

import argparse
import json
import secrets
import urllib.error
import urllib.request
from pathlib import Path

# Les trois serveurs MCP du labo, tels qu'exposes par la passerelle mcpo.
# (nom du serveur, libelle affiche dans Open WebUI)
SERVEURS = [
    ("summarizer", "MCP - resume de texte"),
    ("codedoc", "MCP - code en documentation"),
    ("codedb", "MCP - code indexe"),
]


def appel(url: str, methode: str = "GET", corps: dict | None = None,
          jeton: str | None = None, timeout: float = 30.0) -> tuple[int, dict]:
    """Appel HTTP minimal (stdlib), qui ne leve pas sur les codes d'erreur."""
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
            return erreur.code, {"detail": contenu[:200]}


def lire_identifiants(chemin: Path) -> tuple[str, str, str] | None:
    """Relit email / mot de passe d'une installation precedente."""
    if not chemin.is_file():
        return None
    valeurs = {}
    for ligne in chemin.read_text(encoding="utf-8").splitlines():
        if "=" in ligne:
            cle, _, valeur = ligne.partition("=")
            valeurs[cle.strip()] = valeur.strip()
    if {"email", "password"} <= valeurs.keys():
        return valeurs.get("nom", "admin"), valeurs["email"], valeurs["password"]
    return None


def ecrire_identifiants(chemin: Path, nom: str, email: str, mot_de_passe: str) -> None:
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(
        "# Compte administrateur d'Open WebUI (genere par l'installation).\n"
        "# Ce fichier n'est pas versionne : var/ est ignore par git.\n"
        f"nom={nom}\nemail={email}\npassword={mot_de_passe}\n",
        encoding="utf-8",
    )
    chemin.chmod(0o600)


def session(base: str, chemin_identifiants: Path, email: str, nom: str) -> str:
    """Cree le premier compte (il devient administrateur) ou se reconnecte."""
    connus = lire_identifiants(chemin_identifiants)
    if connus:
        _, email, mot_de_passe = connus
        statut, corps = appel(f"{base}/api/v1/auths/signin", "POST",
                              {"email": email, "password": mot_de_passe})
        if statut == 200 and corps.get("token"):
            print(f"  compte existant retrouve ({email})")
            return corps["token"]
        print(f"  [warn] reconnexion refusee ({statut}) : nouvelle creation")

    mot_de_passe = secrets.token_urlsafe(18)
    statut, corps = appel(f"{base}/api/v1/auths/signup", "POST",
                          {"name": nom, "email": email, "password": mot_de_passe})
    if statut in (200, 201) and corps.get("token"):
        ecrire_identifiants(chemin_identifiants, nom, email, mot_de_passe)
        print(f"  compte administrateur cree -> {chemin_identifiants}")
        return corps["token"]

    raise SystemExit(f"  ERREUR : creation du compte refusee ({statut}) : {corps}")


def declarer_serveurs(base: str, jeton: str, ip_wsl: str, port: int, cle: str) -> int:
    """Declare les serveurs d'outils. Idempotent : la liste envoyee remplace
    celle du labo, en conservant d'eventuelles autres entrees."""
    statut, actuel = appel(f"{base}/api/v1/configs/tool_servers", "GET", jeton=jeton)
    existantes = actuel.get("TOOL_SERVER_CONNECTIONS", []) if statut == 200 else []
    nos_urls = {f"http://{ip_wsl}:{port}/{nom}" for nom, _ in SERVEURS}
    autres = [c for c in existantes if c.get("url") not in nos_urls]

    nos_serveurs = [
        {
            "url": f"http://{ip_wsl}:{port}/{nom}",
            "path": "openapi.json",
            "type": "openapi",
            "auth_type": "bearer",
            "key": cle,
            "config": {"enable": True},
            "info": {"id": nom, "name": libelle},
        }
        for nom, libelle in SERVEURS
    ]

    statut, corps = appel(f"{base}/api/v1/configs/tool_servers", "POST",
                          {"TOOL_SERVER_CONNECTIONS": autres + nos_serveurs}, jeton=jeton)
    if statut not in (200, 201):
        raise SystemExit(f"  ERREUR : declaration des serveurs refusee ({statut}) : {corps}")
    return len(nos_serveurs)


def main() -> None:
    analyseur = argparse.ArgumentParser(description="Prepare Open WebUI (compte + outils)")
    analyseur.add_argument("--base", default="http://127.0.0.1:3000")
    analyseur.add_argument("--ip-wsl", required=True)
    analyseur.add_argument("--mcpo-port", type=int, default=9300)
    analyseur.add_argument("--mcpo-key", required=True)
    analyseur.add_argument("--credentials", default="var/openwebui-admin.txt")
    analyseur.add_argument("--email", default="admin@labo.local")
    analyseur.add_argument("--nom", default="admin")
    arguments = analyseur.parse_args()

    print("== Compte administrateur")
    jeton = session(arguments.base, Path(arguments.credentials), arguments.email, arguments.nom)

    print("== Serveurs d'outils")
    nombre = declarer_serveurs(arguments.base, jeton, arguments.ip_wsl,
                               arguments.mcpo_port, arguments.mcpo_key)

    statut, corps = appel(f"{arguments.base}/api/v1/configs/tool_servers", "GET", jeton=jeton)
    if statut == 200:
        for connexion in corps.get("TOOL_SERVER_CONNECTIONS", []):
            print(f"  [ok]   {connexion.get('info', {}).get('name', '?'):32s} {connexion.get('url')}")
    print(f"  {nombre} serveur(s) d'outils declares")


if __name__ == "__main__":
    main()
