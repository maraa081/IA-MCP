# Diagramme de deploiement

Etat du labo. Tout tourne sur une seule machine, sur l'interface de bouclage
(loopback) : les services ne sont pas joignables depuis le reseau.

![04-deploiement](04-deploiement.png)

```mermaid
flowchart TB
    subgraph POSTE["Machine hote (labo)"]
        subgraph LOOPBACK["Interface de bouclage 127.0.0.1"]
            GW["Gateway LiteLLM<br/>:4000<br/>.venv-gateway"]
            ORCH["Orchestrateur FastAPI<br/>:8090<br/>.venv"]
            M1["Serveur MCP summarizer<br/>:9101/mcp<br/>.venv"]
            M2["Serveur MCP codedoc<br/>:9102/mcp<br/>.venv"]
            OLL["Ollama<br/>:11434<br/>service systeme"]
            FS[("Fichiers locaux<br/>config/policies.yaml<br/>config/keys/<br/>var/logs/audit.jsonl")]
        end
        CLI["Client de test<br/>curl / scripts/demo.sh"]
    end

    CLI -- "HTTP + JWT" --> ORCH
    ORCH -- "HTTP + cle de service" --> GW
    M1 -- "HTTP + cle de service" --> GW
    M2 -- "HTTP + cle de service" --> GW
    GW -- "API Ollama" --> OLL
    ORCH -- "MCP streamable HTTP" --> M1
    ORCH -- "MCP streamable HTTP" --> M2
    ORCH --> FS
    GW -. "modeles GGUF" .-> OLL

    subgraph FUTUR["Cible production (a titre indicatif)"]
        direction TB
        ING["Passerelle d'API<br/>TLS + limitation de debit"]
        IDP["Fournisseur d'identite<br/>Keycloak / Entra ID"]
        SVC1["Orchestrateur<br/>(plusieurs replicas)"]
        SVC2["Serveurs MCP<br/>un par domaine metier"]
        GPU["Serveur d'inference<br/>vLLM sur GPU"]
        RED[("Redis<br/>revocation + sessions")]
        PG[("PostgreSQL<br/>cles virtuelles + quotas")]
        SIEM[("SIEM<br/>journaux d'audit")]
    end

    CLI -.-> ING
    ING -.-> SVC1
    IDP -. "jetons signes" .-> ING
    SVC1 -.-> SVC2
    SVC1 -.-> PG
    SVC1 -.-> RED
    SVC2 -.-> GPU
    SVC1 -.-> SIEM
```

## Trois frontieres de reseau, trois niveaux de confiance

| Zone | Confiance | Ce qui s'y trouve | Protection |
| --- | --- | --- | --- |
| Client | Non maitrisee | Poste de l'utilisateur, automatismes | Jeton JWT, limitation de debit |
| Applications | Maitrisee | Orchestrateur, serveurs MCP | Cle de service, reseau segmente |
| Moteur | Maitrisee | Gateway, serveur d'inference | Aucun acces depuis l'exterieur |

**Ce qui est faux aujourd'hui dans le labo, et qu'il faut corriger avant toute
mise en production :**

- tout est en HTTP sans TLS, y compris les jetons : a remplacer par HTTPS, et
  mTLS entre services internes ;
- la meme cle sert a tous les composants internes : a remplacer par une cle
  virtuelle par service, avec quotas et modeles autorises par cle (base
  PostgreSQL cote LiteLLM, voir `gateway/README.md`) ;
- la politique et les jetons reposent sur des fichiers locaux : a deplacer vers
  Redis et un service d'identite ;
- la limitation de debit est en memoire, donc non partagee entre replicas : a
  deplacer dans la passerelle d'API ;
- un seul exemplaire de chaque service : a passer en plusieurs replicas, en
  verifiant que rien n'est conserve en memoire entre requetes (c'est deja le cas
  pour les sessions MCP, ouvertes puis fermees a chaque appel).

## Ordre de demarrage et de dependance

```
Ollama  ->  Gateway LiteLLM  ->  Serveurs MCP  ->  Orchestrateur
```

L'orchestrateur interroge les serveurs MCP au demarrage (`ToolRegistry.refresh`)
mais ne refuse pas de demarrer si l'un est absent : il le marque
indisponible et retire ses outils du catalogue. C'est un choix de disponibilite,
visible dans la reponse de `GET /health`.

Consequence pratique a connaitre : si vous lancez un serveur MCP **apres**
l'orchestrateur, ses outils n'apparaitront pas tant que l'orchestrateur n'aura
pas redemarre.
