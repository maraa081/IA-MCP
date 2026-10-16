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
cle maitresse : les cles de service sont stockees dans une base
(`database_url`). Le `.env` du labo utilise donc la cle maitresse comme cle
interne, et la gateway n'ecoute que sur `127.0.0.1`.

C'est acceptable pour un labo sur une seule machine, et inacceptable des que
plusieurs services ou plusieurs personnes partagent la gateway.

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
