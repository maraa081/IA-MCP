# La passerelle d'outils (mcpo)

## Role

Open WebUI parle **OpenAPI**, les serveurs MCP parlent **MCP**. Cette passerelle
fait le pont : elle se connecte aux serveurs MCP en `streamable-http` et publie
chacun de leurs outils comme une route REST, avec un document OpenAPI.

```
Open WebUI  ->  mcpo :9300  ->  serveurs MCP 9101 / 9102 / 9103
```

Elle ne detient **aucune cle de modele** : ce sont les serveurs MCP qui parlent a
la gateway LiteLLM, avec leur propre cle (`MCPO_GATEWAY_API_KEY`), restreinte au
seul modele de redaction. Le moindre privilege reste donc applique.

## Configuration

`bridge/mcpo.config.json` declare les trois serveurs distants :

```json
{
  "mcpServers": {
    "summarizer": { "type": "streamable-http", "url": "http://127.0.0.1:9101/mcp" }
  }
}
```

Ajouter un outil se fait ici : une entree de plus, puis `./scripts/bridge.sh up`.

## Commandes

```bash
./scripts/bridge.sh up       # demarre la passerelle et affiche les URL a declarer
./scripts/bridge.sh down     # l'arrete
./scripts/bridge.sh status   # etat + URL des documents OpenAPI
```

`status` affiche les trois adresses a coller dans Open WebUI (Administration,
Outils, Ajouter une connexion), et rappelle ou lire la cle d'acces :

```bash
grep '^MCPO_API_KEY=' .env
```

## Ce qui est publie

| Serveur | Route OpenAPI | Outils |
| --- | --- | --- |
| `summarizer` | `/summarizer/openapi.json` | `summarize_text` |
| `codedoc` | `/codedoc/openapi.json` | `code_to_technical_doc` |
| `codedb` | `/codedb/openapi.json` | `code_disponible`, `chercher_code`, `resumer_code`, `documenter_code` |

Verification rapide, sans navigateur :

```bash
curl -s http://127.0.0.1:9300/codedb/openapi.json | python -m json.tool | head
```

## Piege a connaitre

`mcpo` 0.0.20 exige `mcp<2`, alors que les serveurs du labo utilisent le SDK
2.3.0. Le venv dedie `.venv-bridge` fige donc `mcp==1.30.0`. Les deux versions
cohabitent parce qu'elles vivent dans deux environnements separes : c'est le
remède, pas un contournement.

```bash
python -m venv .venv-bridge
./.venv-bridge/bin/pip install mcpo 'mcp==1.30.0'
```

Le client 1.30.0 parle sans probleme aux serveurs 2.3.0 (le protocole MCP est
negocie entre les deux).
