# Parcours commente d'une requete reelle

Ce document suit une requete, du premier appel HTTP jusqu'a la ligne du journal
d'audit. Les valeurs affichees proviennent d'une execution reelle sur la pile
decrite dans le README, avec une inference sur CPU.

Pour reproduire : `./scripts/up.sh` puis `./scripts/demo.sh`.

## Etape 1 - Sans jeton, l'API refuse

```bash
curl -s -o /dev/null -w "%{http_code}" -X POST http://127.0.0.1:8080/v1/chat \
  -H 'Content-Type: application/json' -d '{"message":"bonjour"}'
```

```
401
```

Le refus est immediat : il ne depend d'aucune logique metier. C'est la dependance
`get_current_user` de FastAPI qui s'applique avant que la route ne soit appelee.

## Etape 2 - Obtention d'un jeton

```bash
curl -s -X POST http://127.0.0.1:8080/auth/token \
  -H 'Content-Type: application/json' \
  -d '{"username":"demo","password":"changeme-lab"}'
```

Le jeton decode contient :

```json
{
  "iss": "mcp-orchestrator-lab",
  "aud": "orchestrator-api",
  "sub": "demo",
  "roles": ["analyst"],
  "scopes": [],
  "jti": "964e9b95e1f3420f89a24b9e965d3a60",
  "iat": 1792193637,
  "nbf": 1792193637,
  "exp": 1792197237,
  "typ": "access"
}
```

Trois remarques sur ces claims :

- `roles` porte les droits, mais il ne les accorde pas : `policies.yaml` reste la
  source de verite. Un jeton qui annoncerait `roles: ["admin"]` n'obtiendrait
  rien de plus si le role `admin` n'existe pas dans la politique ;
- `jti` identifie ce jeton precis : c'est lui qui est revoque a la deconnexion ;
- la duree de vie est de 3600 s dans le labo, 900 s recommande en production.

## Etape 3 - Les droits effectifs

```bash
curl -s http://127.0.0.1:8080/auth/me -H "Authorization: Bearer $TOKEN"
```

```json
{
  "sub": "demo",
  "roles": ["analyst"],
  "tools": {
    "summarize_text": {
      "max_input_chars": 20000,
      "max_calls_per_request": 1,
      "allowed_frameworks": null,
      "rate_limit": { "requests": 30, "window_seconds": 60 }
    },
    "code_to_technical_doc": {
      "max_input_chars": 12000,
      "max_calls_per_request": 1,
      "allowed_frameworks": ["diataxis", "arc42", "c4", "madr", "sphinx"],
      "rate_limit": { "requests": 10, "window_seconds": 60 }
    }
  },
  "limits": {
    "max_prompt_chars": 40000,
    "max_tool_calls_per_request": 3,
    "allowed_models": ["planner", "writer"]
  },
  "token_expires_at": 1792197237
}
```

C'est la lecture du fichier `config/policies.yaml`, fusionnee avec les plafonds
globaux. Aucune valeur n'est inventee : tout est lisible dans le YAML.

## Etape 4 - Ce que le modele a le droit de voir

```bash
curl -s http://127.0.0.1:8080/tools -H "Authorization: Bearer $TOKEN"
```

```json
[
  { "name": "code_to_technical_doc", "server": "codedoc" },
  { "name": "summarize_text", "server": "summarizer" }
]
```

Ces deux entrees sont exactement les outils dont les schemas sont transmis au
modele a l'etape suivante. Un seul endroit dans le code construit cette liste :
`ToolRegistry.filter(grants)` dans `pipeline.py`.

Le schema transmis contient des valeurs fermees, visibles par le modele :

```json
{
  "framework": {
    "default": "diataxis",
    "enum": ["arc42", "c4", "madr", "diataxis", "sphinx"],
    "type": "string"
  },
  "audience": {
    "default": "developpeur",
    "enum": ["developpeur", "exploitant", "auditeur", "architecte"],
    "type": "string"
  }
}
```

Cette declaration n'est pas cosmetique : sans elle, le modele invente des valeurs
hors liste et l'appel echoue. C'est un des trois defauts corriges apres la
premiere execution complete (voir la section correspondante du README).

## Etape 5 - Le planificateur choisit

Le modele est interroge avec les seuls outils autorises. En mode `native`, il
repond par un appel d'outil structure :

```
tool_calls: summarize_text(text="<document>", max_sentences=5)
```

Le mode choisi est visible dans la reponse (`planner_mode: "native"`). Si le
modele n'avait rien produit d'exploitable, `auto` aurait reessaye en mode `json`
et `planner_mode` aurait indique `json`.

## Etape 6 - Autorisation, puis execution

Avant l'appel reseau, `PolicyEngine.check_tool_call` verifie six points dans
l'ordre : droit du role, quota par requete, taille des arguments, valeurs hors
liste blanche, limite de debit. En cas de refus, aucun paquet n'est emis vers le
serveur MCP.

Quand l'appel passe, le serveur MCP est contacte :

```
Orchestrateur --(MCP tools/call)--> summarizer :9101
                                         |
                                         +--(HTTP)--> Gateway LiteLLM :4000 --> Ollama
```

Le serveur d'outil appelle a son tour le modele, avec l'alias `writer`, pour
produire le resultat. La trace de l'appel est renvoyee dans la reponse :

```json
{
  "tool": "summarize_text",
  "decision": "allowed",
  "reason": "execute",
  "duration_ms": 12034,
  "output_preview": "{\"tool\": \"summarize_text\", \"summary\": \"...\"}"
}
```

## Etape 7 - Synthese et reponse

Deux choix possibles, tous les deux visibles par configuration :

- `SYNTHESIS_ENABLED=true` (par defaut) : un troisieme appel de modele redige la
  reponse finale a partir des resultats ;
- `synthesize: false` dans la requete : le resultat de l'outil est rendu tel quel.
  Pour un resume ou une documentation, l'outil produit deja un texte utilisable,
  ce qui economise un appel complet.

## Etape 8 - Le journal d'audit

```json
{"event":"chat.completed","sub":"demo","roles":["analyst"],"tools_executed":[],"denials":[],"allowed":true,"latency_ms":62343}
{"event":"chat.completed","sub":"demo","roles":["analyst"],"tools_executed":["code_to_technical_doc"],"denials":[],"allowed":true,"latency_ms":96076}
{"event":"chat.completed","sub":"lecteur","roles":["reader"],"tools_executed":[],"denials":[],"allowed":true,"latency_ms":24230}
```

Ces trois lignes sont reelles et racontent chacune une chose differente :

- ligne 1 (`demo`, aucun outil execute, 62 s) : le premier parcours, avant
  correction, ou le modele avait redige un resume de sa propre initiative ;
- ligne 2 (`demo`, `code_to_technical_doc` execute, 96 s) : l'appel a bien eu
  lieu, mais l'outil a refuse ses arguments. Avant correction, cette erreur
  n'apparaissait pas dans le journal ;
- ligne 3 (`lecteur`, role `reader`, 24 s) : le modele a repondu de lui-meme, sans
  proposer l'outil interdit. Il n'y a donc pas eu de refus de politique a
  journaliser. Une non-decision du modele ne prouve rien : c'est pour cela que la
  GPO est verifiee en code, et testee unitairement plutot que constatee a l'oeil.

## Ce que ce parcours enseigne

**La latence se lit etage par etage.** Sur cette machine, 20 a 100 secondes pour
une demande, dont l'essentiel en inference sur CPU et non dans le code
d'orchestration. Mettre un GPU ou reduire le nombre d'appels de modele
(`SYNTHESIS_ENABLED=false`, reflexion desactivee) change tout ; optimiser le code
Python ne changerait rien.

**Un modele qui repond juste n'est pas un modele qui a bien travaille.** Le cas
le plus instructif de ce parcours est celui ou l'utilisateur obtient une bonne
reponse alors que l'outil n'a jamais ete appele. Le resultat etait satisfaisant
pour l'oeil, et l'architecture n'avait pas servi. `tools_used` est le seul
indicateur honnete.

**Un refus silencieux n'est pas une preuve.** L'absence de refus ne signifie pas
que les droits sont bien appliques : elle peut simplement signifier que le modele
n'a rien tente. La garantie vient des tests, pas de l'observation.
