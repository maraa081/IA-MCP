# La gateway LiteLLM

## Role

La gateway est le **point d'entree unique vers les modeles**. Ni l'orchestrateur
ni les serveurs MCP ne parlent directement a Ollama : tous passent par ici.

```
Applications  ->  Gateway LiteLLM :4000  ->  Ollama :11434
```

## Alias de modeles

Le code applicatif ne connait que des alias logiques, definis dans
`gateway/config.yaml` :

| Alias | Modele reel | Reglages | Usage |
| --- | --- | --- | --- |
| `planner` | `ollama_chat/qwen3.5:4b` | `temperature: 0.1`, `num_ctx: 8192` | Choix des outils : on veut une decision stable |
| `writer` | `ollama_chat/qwen3.5:4b` | `temperature: 0.4`, `num_ctx: 16384` | Redaction : on veut du texte fluide |

Changer de moteur, de taille de contexte ou de modele se fait donc **ici
uniquement**. Exemple pour passer sur un modele plus gros pour la redaction :

```yaml
  - model_name: writer
    litellm_params:
      model: ollama_chat/qwen3.5:9b
      api_base: http://127.0.0.1:11434
      temperature: 0.4
```

## Authentification, deux niveaux

| Niveau | Cle | Usage |
| --- | --- | --- |
| Cle maitresse | `GATEWAY_MASTER_KEY` | Administration de la gateway |
| Cles de service | `GATEWAY_API_KEY` | Appels des applications |

**Limite du labo, a connaitre.** Sans base de donnees, LiteLLM n'accepte que la
cle maitresse : toute autre cle produit l'erreur `no_db_connection`, parce qu'il
cherche cette cle dans une table qu'il n'a pas. Le `.env` du labo utilise donc la
cle maitresse comme cle interne, et la gateway n'ecoute que sur `127.0.0.1`.

C'est acceptable pour un labo sur une seule machine, et inacceptable des que
plusieurs services ou plusieurs personnes partagent la gateway.

## Appeler la gateway directement

Utile pour tester un modele sans passer par l'orchestrateur.

**Recuperer la cle maitresse.** Elle vit dans le `.env` du projet, qui n'est jamais
versionne (verifie : aucun secret dans l'historique du depot).

```bash
cd /home/maxim/.openclaw/workspace/mcp-orchestrator-lab
source .env          # place GATEWAY_MASTER_KEY dans l'environnement
```

Ou pour l'afficher ponctuellement : `grep '^GATEWAY_MASTER_KEY=' .env`

**Appeler un modele :**

```bash
curl -s http://127.0.0.1:4000/v1/chat/completions \
  --oauth2-bearer "$GATEWAY_MASTER_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"planner","messages":[{"role":"user","content":"dis OK"}],"max_tokens":20,"think":false}' \
  | jq -r '.choices[0].message.content'
```

Deux variantes a connaitre :

- `"model": "writer"` pour la redaction, `planner` pour un choix rapide ;
- `"think": false` desactive la phase de reflexion. Sans ce champ, un modele de
  raisonnement peut consommer tout son budget de tokens en reflexion et renvoyer
  un contenu **vide**, sans erreur.

**Lister les alias declares :**

```bash
curl -s http://127.0.0.1:4000/v1/models \
  --oauth2-bearer "$GATEWAY_MASTER_KEY" | jq -r '.data[].id'
```

**Depuis un client compatible OpenAI :**

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:4000/v1",
    api_key=os.environ["GATEWAY_MASTER_KEY"],
)
reponse = client.chat.completions.create(
    model="writer",
    messages=[{"role": "user", "content": "Bonjour"}],
)
print(reponse.choices[0].message.content)
```

**Limite a garder en tete :** la cle maitresse donne tous les droits
d'administration de la gateway. C'est acceptable pour un labo sur une machine
unique, et inacceptable des que plusieurs services ou plusieurs personnes la
partagent. Pour avoir une cle par service, avec quotas et modeles autorises par
cle, il faut une base de donnees. Ne jamais ecrire cette cle dans un fichier
versionne, ni la coller dans une conversation.

## Passer a une vraie gestion de cles (production)

Deux modifications suffisent.

1. Ajouter une base dans `gateway/config.yaml` :

```yaml
general_settings:
  master_key: os.environ/GATEWAY_MASTER_KEY
  database_url: os.environ/LITELLM_DATABASE_URL
```

avec dans `.env` :

```
LITELLM_DATABASE_URL=postgresql://utilisateur:motdepasse@127.0.0.1:5432/litellm
```

2. Creer une cle par service, avec ses limites :

```bash
curl -X POST http://127.0.0.1:4000/key/generate \
  -H "Authorization: Bearer $GATEWAY_MASTER_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
        "key_alias": "orchestrateur-prod",
        "models": ["planner", "writer"],
        "max_budget": 20,
        "budget_duration": "30d",
        "rpm_limit": 120
      }'
```

Chaque service recoit alors sa propre cle, avec ses quotas et la liste des
modeles qu'il peut utiliser. Une cle compromise se revoque sans toucher aux
autres services.

LiteLLM gere aussi les migrations de la base au demarrage, et expose une
interface d'administration pour suivre la consommation par cle.

## Verification rapide

```bash
# La gateway repond ?
curl -s http://127.0.0.1:4000/health/liveliness

# Quels alias sont declares ?
curl -s http://127.0.0.1:4000/v1/models -H "Authorization: Bearer $GATEWAY_API_KEY" | jq '.data[].id'

# Un appel de bout en bout
curl -s http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"planner","messages":[{"role":"user","content":"dis OK"}],"max_tokens":20}' | jq -r '.choices[0].message.content'
```

## Parametre propre a Ollama

L'orchestrateur ajoute `think: false` aux requetes adressees au planificateur :
la phase de reflexion du modele n'apporte rien pour choisir entre deux outils, et
elle triple le temps de reponse. Si un moteur ne connait pas ce parametre, le
client retire le champ et reessaie une fois (`_post_with_fallback` dans
`orchestrator/app/llm/gateway_client.py`). La gateway est configuree avec
`drop_params: true`, qui produit le meme effet de son cote.
