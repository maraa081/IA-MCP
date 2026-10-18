# Orchestrateur MCP (labo)

Mini infrastructure d'orchestration d'outils par un modele de langage local, avec
authentification JWT, politique de droits (GPO) et serveurs MCP.

Objectif : comprendre et manipuler chaque brique d'une architecture que l'on peut
transposer en entreprise, sans dependre d'un service cloud.

```
Utilisateur
    |
    |  navigateur (le seul point d'entree)
    v
+--------------------------------------+
|  Open WebUI (Docker) :3000           |
|  une interface, une seule cle        |
+------------------+-------------------+
                   |  OpenAI + JWT
                   v
+-----------------------------+        +--------------------------+
|  Orchestrateur (conteneur)  |  HTTP  |  Gateway LiteLLM :4000   |
|  aucun port publie          |------->|  (alias planner/writer)  |
|                             |        +------------+-------------+
|  1. verification du JWT     |                     |
|  2. politique (GPO)         |                     v
|  3. choix des outils        |        +--------------------------+
|  4. appel des serveurs MCP  |        |  Ollama :11434           |
|  5. synthese + audit        |        |  qwen3.5:4b              |
+------+---------------+------+        +--------------------------+
       |               |
       | MCP           | MCP
       v               v
+-------------+  +--------------+  +--------------+
| summarizer  |  | codedoc      |  | codedb       |
| :9101       |  | :9102        |  | :9103        |
| summarize_  |  | code_to_     |  | 4 outils sur |
| text        |  | technical_doc|  | base SQLite  |
+-------------+  +--------------+  +--------------+
```

Le port 8090 n'est plus expose : l'orchestrateur tourne en conteneur sur le
reseau Docker, et Open WebUI est le seul chemin d'acces. Toute requete traverse
donc le JWT, la GPO et le journal d'audit.

Pour savoir quels fichiers lire, dans quel ordre : [docs/lire-le-projet.md](docs/lire-le-projet.md).

## Ce que fait ce labo

Un utilisateur s'authentifie, puis envoie une demande en langage naturel
("resume moi ce document", "documente ce code"). L'orchestrateur :

1. verifie le jeton JWT (signature RS256, expiration, audience, revocation) ;
2. calcule les droits de l'utilisateur a partir de `config/policies.yaml` ;
3. demande a un modele local de choisir les outils utiles, en ne lui montrant
   que les outils autorises ;
4. revalide chaque appel propose, puis l'execute via le protocole MCP ;
5. redige la reponse finale et ecrit une trace d'audit complete.

Le modele propose, le code decide. C'est le principe central du projet.

Pour une demande qui demande plusieurs etapes ("trouve le fichier qui gere la
politique de droits, puis resume-le"), l'orchestrateur boucle : il rappelle le
modele avec les resultats obtenus, jusqu'a deux tours. La boucle est bornee, et
chaque tour repasse par la politique de droits.

## Composants et ports

| Composant | Port | Role | Code |
| --- | --- | --- | --- |
| Ollama | 11434 | Moteur d'inference local | (externe) |
| Gateway LiteLLM | 4000 | Point d'entree unique des modeles, cles virtuelles, quotas | `gateway/config.yaml` |
| PostgreSQL (Docker) | 5433 | Cles virtuelles de la gateway, budgets, historique des appels | `scripts/setup_db.sh` |
| Serveur MCP `summarizer` | 9101 | Outil `summarize_text` | `mcp_servers/summarizer/server.py` |
| Serveur MCP `codedoc` | 9102 | Outil `code_to_technical_doc` (code fourni dans le prompt) | `mcp_servers/codedoc/server.py` |
| Serveur MCP `codedb` | 9103 | Quatre outils qui lisent le code dans une base | `mcp_servers/codedb/server.py` |
| Orchestrateur (conteneur) | interne 8090 | Authentification, GPO, planification, synthese. Aucun port publie | `orchestrator/app/` |
| Open WebUI (Docker) | 3000 | Interface unique : chat, choix du jeu d'outils par modele | `scripts/openwebui.sh` |
| Passerelle OpenAI | `/v1` | Traduit Open WebUI vers le pipeline (`/v1/models`, `/v1/chat/completions`) | `orchestrator/app/routers/openai.py` |

Un seul utilisateur de demonstration (`demo`, role `analyst`) et deux outils,
comme demande. Tout est prevu pour en ajouter sans toucher au coeur du code.

## Demarrage

Sur un clone frais, une seule commande suffit : elle verifie les prerequis,
propose un modele, installe les environnements et demarre la pile.

```bash
./install.sh
```

Le guide detaille (choix du modele selon la machine, options, depannage) est
dans [docs/installation.md](docs/installation.md). Equivalent non interactif :

```bash
./install.sh --model qwen3.5:2b --yes   # PC sans GPU, 8 Go de RAM
```

Les etapes separees restent disponibles si vous voulez faire la main :

```bash
./scripts/bootstrap.sh     # environnements Python, cles JWT, .env, modele Ollama
./scripts/up.sh            # Ollama, gateway, serveurs MCP, orchestrateur (conteneur)
./scripts/openwebui.sh up  # interface Open WebUI
./scripts/openwebui.sh connect  # branche Open WebUI sur l'orchestrateur (JWT)
./scripts/demo.sh          # parcours complet commente, de bout en bout
./scripts/down.sh          # arret (--all pour arreter aussi Ollama)
```

Ouvrir ensuite <http://localhost:3000>. L'API de l'orchestrateur n'est plus
publiee sur la machine : `./scripts/prompt.sh` et `./scripts/test-infra.sh`
continuent de fonctionner (ils rejouent leurs requetes dans le conteneur).

Comptez plusieurs dizaines de secondes par appel de modele : l'inference se fait
sur CPU.

### Depuis Open WebUI (le seul chemin)

Le navigateur ne parle plus a l'orchestrateur : il ouvre Open WebUI, qui appelle
l'orchestrateur en OpenAI + JWT.

```
http://localhost:3000         interface de chat
```

Les modeles proposes portent le choix des outils (voir plus bas). La
documentation OpenAPI de l'orchestrateur reste consultable en developpement via
`docker exec orchestrator-lab curl -s http://127.0.0.1:8090/docs`, mais elle
n'est plus un point d'entree utilisateur.

### Interface unique : Open WebUI

Open WebUI est le seul point d'entree. L'orchestrateur n'est plus un service
visible : il tourne en conteneur, sans port publie, et Open WebUI l'appelle par
son nom sur le reseau Docker `mcp-lab`.

```bash
./scripts/up.sh                 # Ollama, gateway, MCP, orchestrateur (conteneur)
./scripts/openwebui.sh up       # Open WebUI, port 3000
./scripts/openwebui.sh connect  # branche Open WebUI sur l'orchestrateur (JWT)
```

```
navigateur -> Open WebUI :3000 -> orchestrateur (conteneur, JWT)
                                      |-> gateway LiteLLM :4000 -> Ollama
                                      |-> serveurs MCP :9101 / :9102 / :9103
```

**Choisir les outils : le modele auto.** Il n'y a plus de cases a cocher ni de
modeles specialises : un seul modele, `Assistant`, recoit tous les outils
autorises par la GPO. A chaque message, le planificateur propose l'outil utile et
la GPO tranche :

| Ce que vous demandez | Outil choisi automatiquement |
| --- | --- |
| un resume de texte | `summarize_text` |
| le nombre de fichiers indexes | `code_disponible` |
| une recherche dans le code | `chercher_code` |
| l'explication d'un fichier | `resumer_code` |
| la documentation d'un fichier | `documenter_code` |
| la documentation d'un code colle | `code_to_technical_doc` |
| une salutation, une question generale | reponse directe, sans outil |

Le choix reste une **intersection** avec la GPO : le modele ne peut jamais
appeler un outil que le role du jeton n'autorise pas.

**JWT.** `./scripts/openwebui.sh connect` fabrique un jeton de service
(sub `openwebui`, role `analyst`, duree 365 jours), l'ecrit dans
`var/openwebui-token.txt` (droits 600, non versionne) et le declare comme cle du
fournisseur OpenAI. Chaque message repasse donc par la verification JWT, la GPO
et le journal d'audit. Un jeton revoque coupe immediatement l'acces
(`config/revoked_tokens.json`). Pour renouveler le jeton, relancer `connect`.

**Ce qui a change.** `mcpo` n'est plus utilise : le chemin Open WebUI -> mcpo ->
MCP contournait le pipeline. Les serveurs MCP ecoutent desormais sur `0.0.0.0`
pour que le conteneur de l'orchestrateur les joigne ; ils restent sur le reseau
local de la machine.

### Tester en tapant un prompt

Le plus simple pour voir l'infrastructure travailler :

```bash
./scripts/prompt.sh "Resume moi ce document : <votre texte>"
./scripts/prompt.sh "Documente ce code avec le framework arc42 : <votre code>"
cat document.txt | ./scripts/prompt.sh "Resume ce document"

# depuis la base de code indexee (aucun code a coller dans le prompt)
./scripts/prompt.sh --no-synthesis "Resume le fichier orchestrator/app/policy/engine.py de la base de code"
./scripts/prompt.sh "Documente le fichier mcp_servers/codedb/server.py avec le framework madr"
```

Le script signe un jeton localement, envoie le prompt et affiche non seulement la
reponse mais aussi **la trace de la decision** : mode de planification, outils
appeles, refus eventuels, duree de chaque appel, et la ligne d'audit
correspondante.

Options utiles :

| Option | Effet |
| --- | --- |
| `--no-synthesis` | supprime l'appel de redaction finale : plus rapide, l'outil rend son texte directement |
| `--role reader` | rejoue la meme demande avec un role moins permissif, pour voir la GPO a l'oeuvre |
| `--sub <nom>` | change le porteur du jeton |
| `--json` | affiche la reponse brute de l'API |

Exemple de sortie (resume d'un incident, `--no-synthesis`) :

```
utilisateur : demo   role(s) : analyst   modele : planner -> writer

 REPONSE
Le service de facturation a connu une interruption de 3 heures le 12 octobre due
<...>

 TRACE DE LA DECISION
trace_id ......... b9effffacb5b4676
mode de decision . native
outils appeles ... summarize_text
  [allowed] summarize_text (15451 ms)
```

### Essai manuel en trois commandes

L'orchestrateur n'ayant plus de port publie, les appels se font dans le conteneur
(ou via `./scripts/prompt.sh`, qui le fait deja).

```bash
# 1. obtenir un jeton
TOKEN=$(docker exec -i orchestrator-lab curl -s -X POST http://127.0.0.1:8090/auth/token \
  -H 'Content-Type: application/json' \
  -d '{"username":"demo","password":"changeme-lab"}' | jq -r .access_token)

# 2. voir ses droits
docker exec -i orchestrator-lab curl -s http://127.0.0.1:8090/auth/me \
  -H "Authorization: Bearer $TOKEN" | jq

# 3. poser une demande
docker exec -i orchestrator-lab curl -s -X POST http://127.0.0.1:8090/v1/chat \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"message":"Resume moi ce document : <collez votre texte>"}' | jq '{answer, tools_used}'
```

## Les outils MCP

### Le code fourni, ou le code lu en base ?

Deux serveurs repondent au meme besoin par des chemins opposes. C'est la
decision d'architecture la plus interessante du projet.

| | Serveur `codedoc` | Serveur `codedb` |
| --- | --- | --- |
| Source du code | fourni dans le prompt | lu dans une base SQLite |
| Taille acceptee | limitee par la fenetre de contexte | limitee par la base |
| Traçabilite | le code apparait dans le message | seule la reference apparait |
| Usage | un extrait ponctuel | un depot entier indexe |

En pratique, `codedb` est ce qu'on veut en entreprise : le modele manipule des
references (`policy/engine.py`), pas des milliers de lignes de code.

### `summarize_text` (serveur `summarizer`)

Resume un texte en `max_sentences` phrases, dans un style donne
(`neutral`, `bullets`, `executive`, `technical`).

### `code_to_technical_doc` (serveur `codedoc`)

Transforme du code source en documentation technique structuree par un framework
etabli, choisi par l'appelant :

| Framework | Origine | Usage typique |
| --- | --- | --- |
| `diataxis` | diataxis.fr | Documentation utilisateur (tutoriel, guide, reference, explication) |
| `arc42` | arc42.org | Documentation d'architecture en 12 sections |
| `c4` | c4model.com | Architecture par niveaux de zoom, avec diagrammes Mermaid |
| `madr` | adr.github.io | Trace de decision technique |
| `sphinx` | Sphinx / rST | Documentation integree au code |

Le framework est impose au modele section par section, et le serveur verifie
ensuite dans le texte produit quelles sections manquent (`missing_sections`).
Le modele ne s'auto-certifie pas.

### Les quatre outils de `codedb`

| Outil | Ce qu'il fait |
| --- | --- |
| `code_disponible` | inventaire de l'index : langages, fichiers les plus volumineux |
| `chercher_code` | recherche sur le chemin, la description et le contenu |
| `resumer_code` | explique un fichier designe par sa reference |
| `documenter_code` | produit une documentation structuree de ce fichier |

Aucun de ces outils ne recoit de code en argument : ils recoivent une reference,
et c'est la base qui fournit le contenu. L'index est construit par :

```bash
./.venv/bin/python scripts/index_code.py            # indexe le projet lui-meme
./.venv/bin/python scripts/index_code.py --racine /autre/depot
./.venv/bin/python scripts/index_code.py --liste    # simulation
```

L'indexation est incrementalе : l'empreinte sha256 du contenu evite de reecrire
les fichiers inchanges, ce qui permet de la mettre dans une tache planifiee.

> Remarque : au premier lancement, `codedoc` est plus rapide a utiliser (on colle
> un extrait). `codedb` demande d'indexer d'abord, mais c'est le seul qui tient a
> l'echelle d'un depot et le seul qui laisse une trace exploitable (la reference
> demandee, pas le contenu).

## Securite : quatre couches

1. **Authentification** : JWT RS256, claims obligatoires (`iss`, `aud`, `sub`,
   `jti`, `exp`, `nbf`, `iat`), liste blanche d'algorithmes, revocation par
   `jti`, cle publique publiee en JWKS.
2. **Autorisation** : GPO dans `config/policies.yaml`. Refus par defaut, quota
   d'appels, taille d'entree maximale, liste blanche de frameworks, limitation
   de debit par utilisateur.
3. **Isolation du modele** : le modele ne recoit que les outils autorises, et
   chaque appel est revalide avant execution. Un prompt qui demande un outil
   interdit n'obtient rien : le modele ne peut pas accorder un droit qu'il n'a
   pas.
4. **Traçabilite** : journal d'audit JSON Lines (`var/logs/audit.jsonl`) avec
   l'identite, les outils proposes, executes, refuses, la raison du refus et la
   duree. Aucun contenu de document, seulement une empreinte.

Detail complet : [docs/security.md](docs/security.md).

## Guide de lecture du code

L'ordre ci-dessous suit le trajet d'une requete. C'est l'ordre de lecture
conseille.

| Fichier | Ce qu'on y apprend |
| --- | --- |
| `orchestrator/app/config.py` | Tout ce qui est parametrable, et rien en dur |
| `orchestrator/app/container.py` | Comment les briques sont assemblees et arretees |
| `orchestrator/app/pipeline.py` | **Le coeur** : les 6 etapes d'une requete |
| `orchestrator/app/security/jwt_service.py` | Verification d'un JWT, claims, revocation |
| `orchestrator/app/policy/engine.py` | La GPO : qui a le droit a quoi, et pourquoi refuser |
| `orchestrator/app/llm/planner.py` | Comment le modele choisit ses outils (2 strategies) |
| `orchestrator/app/mcp/client.py` | Comment un outil MCP est appele |
| `orchestrator/app/mcp/registry.py` | Comment les outils sont decouverts et decrits |
| `orchestrator/app/audit.py` | Ce qu'on trace, et ce qu'on ne trace pas |
| `mcp_servers/*/server.py` | Un serveur d'outil, en 80 lignes lisibles |

## Diagrammes

| Diagramme | Image | Source (editable) |
| --- | --- | --- |
| Sequence d'une demande | [PNG](docs/uml/01-sequence-demande.png) | [PlantUML](docs/uml/01-sequence-demande.puml) |
| Boucle de planification (2 tours) | [PNG](docs/uml/01b-boucle-planification.png) | [PlantUML](docs/uml/01b-boucle-planification.puml) |
| Diagramme de classes | [PNG](docs/uml/02-classes.png) | [PlantUML](docs/uml/02-classes.puml) |
| Diagramme d'activite (choix et autorisation des outils) | [PNG](docs/uml/03-activite-outils.png) | [PlantUML](docs/uml/03-activite-outils.puml) |
| Deploiement | [PNG](docs/uml/04-deploiement.png) | [PlantUML](docs/uml/04-deploiement.puml) |
| Parcours commente d'une requete reelle | - | [Markdown](docs/parcours-requete.md) |

Les diagrammes sont ecrits en **PlantUML** (`.puml`, source texte versionnable,
plus precis que Mermaid pour l'UML) et rendus en PNG a cote. Pour regenerer
toutes les images apres une modification :

```bash
python3 scripts/render_uml.py
```

Le script utilise le serveur public plantuml.com (pas de JVM sur la machine du
labo) et n'a besoin d'aucune dependance Python.

## Tests

Deux niveaux, complementaires :

```bash
./.venv/bin/python -m pytest      # logique, en isolation (73 tests, aucun service requis)
./scripts/test-infra.sh           # la pile reelle est-elle branchee ? (30 controles, rapide)
./scripts/test-infra.sh --full    # ajoute un appel complet au modele (lent sur CPU)
```

`make test` et `make test-infra` font la meme chose.

### Ce que verifie le test d'infrastructure

| Section | Controles |
| --- | --- |
| Moteur d'inference | Ollama repond, le modele declare dans la gateway est present |
| Gateway | joignable, refuse un appel sans cle, alias `planner` charge |
| Orchestrateur et MCP | etat global, gateway vue depuis l'orchestrateur, chaque serveur MCP joignable, outils decouverts |
| Authentification | sans jeton 401, mauvais mot de passe 401, jeton emis, jeton malforme 401 |
| Politique de droits | deux outils pour `analyst`, un seul pour `reader`, aucun pour un role inexistant, message hors plafond refuse en 403 avec la regle dans `X-Policy-Rule` |
| Revocation | jeton valide 200, revocation enregistree, jeton revoque 401 |
| Audit | evenements lisibles, empreinte du message et non son contenu, outils proposes et executer journalises |
| Bout en bout (`--full`) | le modele appelle le bon outil, la reponse n'est pas vide, l'appel est autorise par la GPO |

Le script sort en code d'erreur si un controle echoue : il est utilisable dans une
chaine d'integration.

### Verification manuelle, en trois commandes

```bash
curl -s http://127.0.0.1:8090/health | jq              # la pile est-elle debout ?
curl -s -X POST http://127.0.0.1:8090/auth/token -H 'Content-Type: application/json' \
  -d '{"username":"demo","password":"changeme-lab"}' | jq -r .access_token
curl -s -X POST http://127.0.0.1:8090/v1/chat --oauth2-bearer "$TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"message":"Resume moi ce document : <votre texte>"}' | jq '{tools_used, answer}'
```

### Piege classique

Si les tests d'authentification echouent apres avoir modifie `.env` :
l'orchestrateur lit sa configuration **une seule fois, au demarrage**. Un
changement de `DEV_USERNAME` ou de `DEV_PASSWORD` demande un redemarrage
(`./scripts/down.sh && ./scripts/up.sh`). Le script `test-infra.sh` detecte ce cas
et le dit explicitement plutot que d'afficher des echecs en cascade.

### Ce que couvrent les tests unitaires

Les 73 tests couvrent : la chaine JWT (jeton expire, mal signe, mauvaise audience,
confusion d'algorithme, `alg=none`, revocation), le moteur de politiques (refus
par defaut, quotas, frameworks, debit), le pipeline complet avec des doublures
reseau (le modele ne voit que les outils autorises, un appel hors droits est
refuse a l'execution, un echec d'outil n'est pas un succes, le journal d'audit
est complet), les schemas d'outils exposes au modele, le planificateur
(echappatoire de reponse directe, choix impose), la base de code (indexation
incrementale, resolution des references, recherche) et l'interface servie
(page de prompt, redirection, absence de secret dans le HTML).

Les tests ne dependent d'aucun service en cours d'execution : les deux frontieres
reseau (gateway LiteLLM et serveurs MCP) sont remplacees par des doublures. Un
test qui passe ou echoue selon ce qui tourne sur la machine ne teste rien.

## Quatre defauts trouves en executant la demonstration

Ces quatre points n'etaient pas visibles a la lecture du code. Ils viennent d'un
premier parcours complet, et chacun a donne lieu a une correction et a un test.
C'est ce que la demonstration est censee produire.

**1. Le modele repondait lui-meme au lieu d'utiliser l'outil.** Sur "resume moi
ce document", le planificateur produisait un resume de sa propre initiative :
`tools_used` restait vide, l'appel MCP n'avait pas lieu. Consequences : aucune
trace d'outil, aucune garantie de format, et le resultat ne passait pas par le
service cense le produire.

Premiere tentative : durcir le prompt du planificateur. Insuffisant, le
comportement s'est reproduit. La cause reelle est plus subtile : "ne rien
appeler" est un choix implicite, et un petit modele ne le fait pas correctement.

Correction retenue, architecturale : rendre le choix explicite. Un outil virtuel
`respond_directly` est ajoute a la liste transmise au modele, et le choix est
impose (`tool_choice: required`). Le modele doit donc trancher : appeler un outil,
ou repondre. Mesure apres correction : sur une demande de resume il appelle
`summarize_text` (17 s), sur une salutation il repond directement sans toucher
aux outils. Cet outil virtuel n'est jamais execute : c'est un signal de decision,
traduit en reponse par le planificateur.

**2. Le modele devinait les valeurs autorisees.** Les schemas d'outils
declaraient `framework`, `style` et `audience` comme de simples chaines de
caracteres : le modele a produit `audience: "technicien et developpeur"`, hors
liste, et l'appel a echoue. Correction : ces parametres sont declares avec des
types enumeres (`Literal`), donc le schema transmet la liste des valeurs valides
et le modele les voit au lieu de les inventer. Le test
`test_le_schema_du_framework_liste_les_valeurs_autorisees` verrouille le
comportement.

**3. Un outil en erreur etait compte comme un succes.** L'outil refusait ses
arguments et repondait `{"error": "..."}`, ce que l'orchestrateur interpretait
comme un resultat valide : l'appel apparaissait dans `tools_used` et l'utilisateur
recevait une reponse confuse. Correction : toute erreur, qu'elle vienne du
protocole MCP ou du contenu de la reponse, marque l'appel comme echoue, le sort
de `tools_used`, et l'explique a l'utilisateur en distinguant un refus de droits
d'une erreur d'execution.

**4. L'outil renvoyait un resultat vide, sans erreur.** Une fois les appels
correctement routes, le resume revenait avec `summary: ""`. Mesure : sans
desactivation de la phase de reflexion, un modele de raisonnement a qui l'on donne
360 tokens de budget les consomme entierement en raisonnement interne et rend un
contenu **vide** (28 s pour rien, 1330 caracteres de reflexion non exploites).
Avec `OLLAMA_DISABLE_THINKING=true` : 336 caracteres de resume en 5 s.
Correction appliquee aux deux cotes (orchestrateur et serveurs MCP), plus une
erreur explicite des qu'un contenu vide est detecte : une panne silencieuse est
plus couteuse qu'un echec.

### Lecons a garder

- un schema d'outil est un contrat avec le modele : tout ce qui n'y est pas
declare sera devine ;
- un resultat d'outil doit etre verifie, jamais suppose bon ;
- un modele doit toujours avoir un moyen explicite de ne rien faire ;
- une reponse vide n'est pas un succes, et la cause la plus frequente est un
  budget de tokens consomme par la phase de reflexion.

## Ce qui changerait en production

| Element | En labo | En production |
| --- | --- | --- |
| Emission des jetons | `POST /auth/token` de demonstration | Keycloak / Entra ID / Okta ; l'orchestrateur ne fait que verifier |
| Revocation | fichier JSON | Redis ou base, avec TTL egal a la duree du jeton |
| Cles d'acces a la gateway | cle virtuelle par service, en base (fait) | rotation planifiee, budgets par equipe, cles revoquees automatiquement |
| Base de la gateway | PostgreSQL en conteneur sur la meme machine | PostgreSQL manage, sauvegarde, replicat |
| Base de code | SQLite, indexation a la main | PostgreSQL, indexation par la chaine d'integration a chaque fusion |
| Sessions MCP | une par appel | pool de sessions par serveur |
| Transport | HTTP sur `127.0.0.1` | TLS, mTLS entre services, reseau segmente |
| Limitation de debit | en memoire | passerelle d'API (une seule instance, resistante a l'echelle) |
| Politique | fichier YAML | OPA / Rego, publiee par le service d'identite |
| Audit | fichier local | sortie JSON vers le SIEM |
| Moteur | Ollama sur CPU | vLLM sur GPU, ou API externe ; seule la gateway change |

La colonne de gauche est volontairement simple a lire. La colonne de droite est
ce qui fait la difference entre une maquette et un service en production.

## Prerequis

- Python 3.11 ou plus (teste sur 3.12)
- Ollama avec un modele Qwen (le choix se fait a l'installation :
  `qwen3.5:4b` par defaut, `qwen3.5:2b` pour une machine modeste)
- Docker (pour PostgreSQL, la base des cles virtuelles de la gateway)
- `curl` et `jq` pour les scripts de demonstration
- Pas de GPU necessaire : tout tourne sur CPU

`./install.sh` verifie ces points avant de modifier quoi que ce soit et dit
precisement ce qui manque.

PostgreSQL est le seul service en conteneur. Si vous n'avez pas Docker, la pile
fonctionne encore : LiteLLM retombe en mode sans base, ou la seule cle valide est
la cle maitresse.
