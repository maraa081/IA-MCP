# Lire le projet : par ou commencer

Ce depot se lit dans l'ordre du trajet d'une requete. Chaque fichier ci-dessous
existe pour une raison precise ; les commentaires en tete disent "pourquoi", pas
seulement "quoi".

## En 15 minutes : la vue d'ensemble

1. `README.md` : architecture, ports, commandes, et les defauts trouves en
   execution (la partie la plus instructive).
2. `config/policies.yaml` : **la seule source de verite des droits**. Tout le
   reste applique ce fichier. Commencer par la, c'est comprendre le modele de
   securite en 5 minutes.
3. `docs/uml/01-sequence-demande.png` : le trajet complet, de l'authentification
   a l'audit. `03-activite-outils.png` montre les points de refus.

## En 1 heure : le coeur du code

Dans cet ordre exact :

| Fichier | Ce qu'il faut y voir |
| --- | --- |
| `orchestrator/app/pipeline.py` | Les 6 etapes d'une requete. C'est LE fichier du projet : tout le reste est un service qu'il assemble. |
| `orchestrator/app/policy/engine.py` | Comment une decision d'autorisation est prise (deny by default, quotas, tailles, valeurs, debit). |
| `orchestrator/app/llm/planner.py` | Comment le modele choisit un outil. Lire le bloc "echappatoire explicite" : c'est le defaut le plus instructif du projet. |
| `orchestrator/app/mcp/client.py` | L'appel MCP et la detection des erreurs applicatives. |
| `orchestrator/app/security/jwt_service.py` | Emission et verification RS256, claims obligatoires, JWKS. |
| `orchestrator/app/audit.py` | Ce qui est trace, et ce qui ne l'est pas (empreinte, jamais le texte). |
| `orchestrator/app/routers/openai.py` | La passerelle OpenAI : comment Open WebUI entre dans le pipeline. |
| `orchestrator/app/container.py` | L'assemblage des services au demarrage. |

## Pour comprendre le deploiement

| Fichier | Role |
| --- | --- |
| `scripts/up.sh` | Ordre de demarrage : Ollama, gateway, serveurs MCP, orchestrateur. |
| `scripts/orchestrator.sh` | Orchestrateur en conteneur, sans port publie, sur le reseau Docker. |
| `orchestrator/Dockerfile` | L'image : code + dependances ; configuration et audit restent sur l'hote. |
| `scripts/openwebui.sh` | Open WebUI, et `connect` qui le branche sur l'orchestrateur avec un JWT. |
| `scripts/openwebui_connect.py` | Ce que fait `connect`, en clair : fournisseur OpenAI, cle JWT, retrait de l'ancien chemin mcpo. |
| `gateway/config.yaml` | Les alias `planner` / `writer` et le reglage `think: false`. |

## Pour modifier un comportement

| Besoin | Fichier |
| --- | --- |
| Ajouter ou retirer un droit | `config/policies.yaml` |
| Changer un plafond ou un quota | `config/policies.yaml` |
| Ajouter un outil MCP | nouveau dossier dans `mcp_servers/`, URL dans `.env`, droits dans `policies.yaml` |
| Changer la maniere dont le modele choisit | `orchestrator/app/llm/planner.py` |
| Changer la redaction finale | `orchestrator/app/llm/synthesizer.py` |
| Changer ce qui est trace | `orchestrator/app/audit.py` et les appels dans `pipeline.py` |
| Ajouter un modele (jeu d'outils) dans Open WebUI | `orchestrator/app/routers/openai.py`, dictionnaire `MODELES` |
| Changer le transport Open WebUI | `orchestrator/app/routers/openai.py` (`stream` / SSE) |

## Pour verifier sans tout lire

| Commande | Ce qu'elle prouve |
| --- | --- |
| `./scripts/test-infra.sh` | 30 controles : JWT, GPO, refus, revocation, audit, outils. |
| `./.venv/bin/python -m pytest` | 74 tests unitaires, sans reseau. |
| `./scripts/prompt.sh "..."` | Un vrai parcours, avec la trace de decision et la ligne d'audit. |
| `var/logs/audit.jsonl` | La verite terrain : ce qui a ete propose, autorise, execute, refuse. |

## Le detail qui explique la conception

Le modele **propose**, le code **decide**. Aucun module ne demande au modele s'il
a le droit : `ToolPlanner` produit un `Plan`, `PolicyEngine` produit des
`Decision`. Cette separation est physique (deux modules), pas une convention.

Deux verrous volontairement redondants :

1. le modele ne recoit que les outils autorises (il ne peut pas proposer
   l'inconnu) ;
2. chaque appel est revalide avant execution (il peut proposer l'interdit, cela
   ne change rien).

C'est la reponse a la question "et si le modele dit n'importe quoi ?".
