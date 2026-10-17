# Architecture

## Vue d'ensemble

Cinq composants, dont quatre que l'on maitrise :

| Composant | Nature | Role |
| --- | --- | --- |
| Ollama | Service externe | Execute le modele de langage |
| Gateway LiteLLM | Service que l'on configure | Point d'entree unique des modeles, alias, quotas |
| Orchestrateur | Service ecrit dans ce depot | Authentification, GPO, planification, synthese |
| Serveurs MCP | Services ecrits dans ce depot | Executent reellement les outils |
| Client | `curl`, script, interface | Envoie les demandes avec un jeton |

## Pourquoi une gateway devant Ollama ?

Techniquement, l'orchestrateur pourrait appeler Ollama directement. La gateway
est la pour quatre raisons qui se voient toutes en exploitation :

1. **Un point de changement.** Passer d'Ollama a vLLM, puis a une API externe,
   ne touche qu'un fichier de configuration. Aucun code applicatif ne connait le
   nom du moteur.
2. **Des alias logiques.** Le code demande `planner` ou `writer`, jamais
   `qwen3.5:4b`. Changer de modele est une decision d'exploitation, pas une
   modification de code.
3. **Un point de mesure.** Tous les appels au modele passent au meme endroit :
   latence, volume, cout par consommateur.
4. **Une separation des secrets.** Les applications connaissent une cle de
   gateway, jamais l'adresse ni le jeton d'un service d'inference.

Dans le meme esprit, `gateway/config.yaml` fixe des parametres par alias :
`temperature` basse pour le planificateur (on veut une decision stable),
plus haute pour le redacteur (on veut du texte fluide).

## Pourquoi MCP plutot que des fonctions locales ?

Un outil MCP est un **service reseau**, pas une fonction Python. Consequences,
toutes importantes en entreprise :

- l'outil peut etre ecrit dans un autre langage, par une autre equipe ;
- il peut vivre dans un autre reseau, avec ses propres droits d'acces ;
- il peut etre deployee, versionnee et surveillee independamment ;
- il peut etre reutilise par plusieurs agents ou plusieurs applications.

Le protocole standardise la description des outils (`name`, `description`,
`input_schema`) et leur invocation (`tools/call`). L'orchestrateur n'a donc
aucun code specifique a un outil : il decouvre tout au demarrage.

Deux transports sont possibles : `stdio` (le client lance le serveur en
sous-processus, pratique sur un poste de travail) et `streamable HTTP` (le
serveur est un service reseau, ce que l'on utilise ici).

## Flux de donnees

### 1. Authentification

```
Client -> POST /auth/token -> Orchestrateur -> JwtService -> jeton signe RS256
```

Le jeton contient l'identite (`sub`), les roles, un identifiant unique (`jti`)
et une expiration courte. Il est autoporteur : aucune consultation d'annuaire
n'est necessaire a chaque appel.

### 2. Decouverte des outils

```
Demarrage de l'orchestrateur -> McpClient -> chaque serveur MCP : tools/list
                             -> ToolRegistry (catalogue en memoire)
```

Le catalogue contient, pour chaque outil : son nom, sa description, son schema
d'entree et le serveur qui l'heberge. C'est ce catalogue qui alimente le
filtrage par la GPO, puis les schemas transmis au modele.

### 3. Traitement d'une demande

1. verification du jeton ;
2. controle du message (plafond global) ;
3. calcul des droits, filtrage du catalogue ;
4. planification : le modele recoit les outils autorises et propose des appels ;
5. pour chaque appel propose : autorisation, puis execution MCP ;
6. synthese de la reponse finale ;
7. ecriture du journal d'audit.

Le detail est dans [uml/01-sequence-demande.md](uml/01-sequence-demande.md) et
le parcours commente dans [parcours-requete.md](parcours-requete.md).

## Les deux bases de donnees

Le projet utilise deux bases, qui n'ont rien a voir l'une avec l'autre. Les
confondre est l'erreur la plus facile a commettre en lisant l'architecture.

| | Base de la gateway | Base de code |
| --- | --- | --- |
| Moteur | PostgreSQL (conteneur) | SQLite (fichier) |
| A quoi elle sert | cles virtuelles, quotas, historique des appels | catalogue des fichiers de code |
| Qui l'interroge | LiteLLM | le serveur MCP `codedb` |
| Alimentee par | `scripts/virtual_keys.sh` | `scripts/index_code.py` |
| En production | PostgreSQL manage | PostgreSQL, alimente par la chaine d'integration |

Autrement dit : l'une gere **qui a le droit de consommer du modele**, l'autre gere
**ce que les outils savent lire**. Deux problemes de nature differente.

## La boucle de planification

Une demande en une etape ("resume ce texte") se traite en un tour. Une demande en
deux etapes ("trouve le fichier qui gere les droits, puis resume-le") en demande
deux : le modele cherche, l'orchestrateur execute, puis le modele est rappele
avec ce qu'il a trouve.

| Reglage | Effet |
| --- | --- |
| `MAX_TOOL_ROUNDS` | nombre maximal de tours de planification (2 par defaut) |
| `max_tool_calls_per_request` (GPO) | plafond global d'appels executes, tous tours confondus |
| `SYNTHESIS_ENABLED` | synthese finale par le modele redacteur |

Sortie anticipee : si un outil a deja rendu un texte redige (un resume, une
documentation) et que la synthese est desactivee, la boucle s'arrete la. C'est ce
qui rend `--no-synthesis` vraiment plus rapide, sans degrader le resultat.

Le nombre de tours reellement utilises est journalise (champ `rounds`) et affiche
dans la trace de `scripts/prompt.sh`.

## Points d'extension

| Besoin | Ce qu'il faut faire | Fichiers concernes |
| --- | --- | --- |
| Ajouter un outil | Creer un dossier `mcp_servers/<nom>/server.py`, declarer le serveur dans `.env`, redemarrer | `mcp_servers/`, `.env` |
| Ajouter un role ou changer des droits | Editer la GPO, recharger | `config/policies.yaml` |
| Ajouter un framework de documentation | Ajouter une entree dans le registre | `mcp_servers/codedoc/frameworks.py` |
| Ajouter un modele | Ajouter un alias dans la gateway | `gateway/config.yaml` |
| Changer la strategie de choix d'outil | Regler `PLANNER_MODE` (`native`, `json`, `auto`) | `.env` |
| Changer la maniere de composer la reponse | Modifier la synthese | `orchestrator/app/llm/synthesizer.py` |
| Ajouter un controle de politique | Ajouter une regle dans le moteur | `orchestrator/app/policy/engine.py` |

## Choix de conception, et leur contrepartie

**Une session MCP par appel.** Simple, sans etat partage, facile a comprendre.
Contrepartie : une poignee de main HTTP par appel. En production, on garde des
sessions ouvertes dans un pool par serveur.

**Le refus par defaut.** Un nouvel outil est invisible tant qu'il n'est pas
accorde. Contrepartie : ajouter un outil demande deux gestes (le declarer, puis
l'accorder), et un oubli se voit immediatement en `403` explique.

**La GPO dans un fichier YAML.** Lisible, relisible en revue, modifiable sans
redeployer. Contrepartie : pas d'historique ni de validation de schema. En
production, on passe a OPA/Rego avec revue de code obligatoire.

**Deux modes de planification, avec repli automatique.** Le mode `native`
(function calling) donne un resultat structure, mais depend de la qualite du
moteur. Le mode `json` fonctionne avec des modeles plus modestes, au prix d'un
analyse tolerante. Le mode `auto` essaie le natif puis retombe sur le JSON.

**Une synthese separee de l'outil.** L'outil produit une donnee, la synthese
produit une reponse. Cela permet de desactiver la synthese
(`SYNTHESIS_ENABLED=false`) pour gagner un appel de modele quand l'outil produit
deja un texte redige, ce qui est le cas des deux outils de ce labo.

## Performance observee (inference sur CPU)

Mesures faites sur la pile du labo, modele 4B quantifie Q4, sans GPU :

| Operation | Ordre de grandeur |
| --- | --- |
| Salutation simple, reponse directe | 34 s |
| Decision du planificateur, puis appel de l'outil de resume | 38 s + 17 s |
| Demande de resume complete (sans synthese) | 44 s, dont 16 s pour l'outil |
| Resume d'un fichier lu dans la base de code (`resumer_code`) | 67 s, dont 44 s pour l'outil |
| Documentation MADR d'une fonction de 3 lignes | 94 s, dont 74 s pour l'outil |
| Documentation d'un fichier de 60 lignes (framework arc42) | plusieurs minutes |
| Meme appel de modele avec la reflexion activee | 3 a 6 fois plus lent, et resultat vide si le budget est serre |

Trois leviers, par ordre d'efficacite :

1. **Desactiver la phase de reflexion** (`OLLAMA_DISABLE_THINKING=true`, valeur par
   defaut). Sur la decision du planificateur, le facteur mesure est de 3,5 (28 s
   contre 10 s selon la variante) ; sur un appel d'outil a budget serre, c'est la
   difference entre un resultat vide et un resultat utile ;
2. **Desactiver la synthese** (`SYNTHESIS_ENABLED=false`) : supprime un appel de
   modele complet. Pour un resume ou une documentation, l'outil produit deja un
   texte utilisable, comme le montre la verification finale ;
3. **Un GPU ou un modele plus petit.** Le reste du systeme (orchestration,
   politique, MCP) ne represente que quelques dizaines de millisecondes.

Autrement dit : la latence d'une architecture d'orchestration se joue dans le
modele, pas dans le code d'orchestration. Optimiser le Python ne changerait rien.
