# Diagramme de sequence : une demande de bout en bout

Scenario : `demo` (role `analyst`) envoie "resume moi ce document".
C'est le diagramme de reference du projet : tout le reste decoule de ce trajet.

![01-sequence-demande-1](01-sequence-demande-1.png)

```mermaid
sequenceDiagram
    autonumber
    actor U as Utilisateur (demo / analyst)
    participant API as Orchestrateur<br/>(FastAPI :8090)
    participant JWT as Securite JWT<br/>(RS256)
    participant GPO as Politique<br/>(policies.yaml)
    participant PLAN as Planificateur<br/>(llm/planner.py)
    participant GW as Gateway LiteLLM<br/>:4000
    participant LLM as Ollama<br/>qwen3.5:4b
    participant MCP as Serveur MCP<br/>summarizer :9101
    participant AUD as Journal d'audit<br/>audit.jsonl

    Note over U,API: 1. Authentification
    U->>API: POST /auth/token {username, password}
    API->>API: comparaison a temps constant
    API->>JWT: issue_access_token(sub, roles)
    JWT-->>API: jeton signe RS256 + jti
    API-->>U: access_token (duree 900 s)

    Note over U,API: 2. Demande metier
    U->>API: POST /v1/chat + Bearer <jeton>
    API->>JWT: decode(jeton)
    JWT->>JWT: signature, exp, iss, aud, claims obligatoires
    JWT->>JWT: jti present dans la liste de revocation ?
    JWT-->>API: AuthenticatedUser{sub, roles, jti}

    API->>GPO: check_prompt(message)
    GPO-->>API: taille conforme

    API->>GPO: grants_for(["analyst"])
    GPO-->>API: {summarize_text, code_to_technical_doc}

    API->>API: registre MCP filtre sur ces droits
    Note right of API: le modele ne verra QUE ces outils

    Note over API,LLM: 3. Choix des outils
    API->>PLAN: plan(message, outils autorises)
    PLAN->>GW: POST /v1/chat/completions (tools=[...])
    GW->>LLM: requete de chat (alias "planner")
    LLM-->>GW: tool_calls: summarize_text(text=...)
    GW-->>PLAN: reponse normalisee
    PLAN-->>API: Plan{appels:[summarize_text]}

    Note over API,MCP: 4. Autorisation puis execution
    API->>GPO: check_tool_call(tool, arguments, appels deja faits)
    GPO->>GPO: droit du role, quota, taille, valeurs, debit
    GPO-->>API: Decision(allowed=true)

    API->>MCP: JSON-RPC tools/call summarize_text
    MCP->>GW: POST /v1/chat/completions (alias "writer")
    GW->>LLM: prompt de resume
    LLM-->>GW: texte du resume
    GW-->>MCP: reponse
    MCP-->>API: resultat structure {summary, ...}

    Note over API,LLM: 5. Synthese
    API->>GW: composition de la reponse finale
    GW->>LLM: prompt de synthese
    LLM-->>GW: reponse redigee
    GW-->>API: texte final

    Note over API,AUD: 6. Tracabilite
    API->>AUD: {sub, roles, outils proposes, executes, refus, duree}
    API-->>U: {answer, tools_used, tool_trace, latency_ms}

    Note over API,MCP: Cas d'un appel refuse
    API->>GPO: check_tool_call(outil non accorde)
    GPO-->>API: Decision(allowed=false, raison)
    API->>AUD: denials=[...]
    Note right of API: aucun appel MCP n'est emis
```

## La boucle de planification

Depuis l'ajout du serveur de code en base, l'orchestrateur ne se contente plus
d'un seul tour : il faut d'abord trouver le fichier, puis le lire pour le
resumer. Le modele est donc rappele avec les resultats du tour precedent.

![01-sequence-demande-2](01-sequence-demande-2.png)

```mermaid
sequenceDiagram
    autonumber
    participant API as Orchestrateur
    participant PLAN as Planificateur
    participant MCP as Serveurs MCP
    participant GW as Gateway + Ollama

    API->>PLAN: tour 1 : conversation = [demande]
    PLAN->>GW: plan(conversation, outils autorises)
    GW-->>PLAN: appelle chercher_code("politique de droits")
    PLAN-->>API: Plan{appels:[chercher_code]}

    API->>API: autorisation (GPO) puis execution MCP
    API->>MCP: tools/call chercher_code
    MCP-->>API: 3 fichiers, avec leur reference

    API->>PLAN: tour 2 : conversation + resultats de l'outil
    PLAN->>GW: plan(conversation enrichie)
    GW-->>PLAN: appelle resumer_code(reference="policy/engine.py")
    PLAN-->>API: Plan{appels:[resumer_code]}

    API->>MCP: tools/call resumer_code
    MCP-->>API: resume du fichier lu en base

    API->>API: borne atteinte (2 tours) :
    API-->>API: reponse composee a partir des resultats
```

Trois points a retenir :

- **la boucle est bornee** (`max_tool_rounds`, 2 par defaut). Sans borne, une
erreur de modelisation ferait tourner l'orchestrateur indefiniment ;
- **chaque tour repasse par la GPO**. Un droit retire au tour 1 le reste au tour
2 : l'autorisation n'est pas mise en cache ;
- **les refus sont reinjectes dans la conversation**. Le modele voit qu'un outil
a ete refuse, avec la raison, et peut se corriger au tour suivant. C'est aussi
ce qui permet de comprendre, dans les journaux, pourquoi il a change d'avis.

Le journal d'audit porte le champ `rounds` : une requete a trois tours signale
souvent une tache mal cadree ou un outil qui ne repond pas comme attendu.

## Ce que le diagramme met en evidence

**Trois appels au modele pour une seule demande.** Un pour choisir l'outil, un
dans l'outil pour produire le resultat, un pour rediger la reponse finale. C'est
le cout reel d'une architecture d'orchestration : chaque etape ajoute de la
latence. D'ou les deux leviers de reglage du projet :
`SYNTHESIS_ENABLED=false` supprime le troisieme appel, et l'attaque
d'entrainement du modele determine la qualite du premier.

**Le meme chemin pour tous les appels au modele.** L'orchestrateur et le serveur
MCP passent tous les deux par la gateway : une seule adresse, une seule cle, un
seul point de journalisation.

**La decision d'autorisation est prise deux fois.** Une fois avant de montrer les
outils au modele (etape 4 du diagramme, "le modele ne verra que ces outils"), une
fois avant d'executer chaque appel. Les deux verrous sont volontairement
redondants.

**Un appel refuse n'atteint jamais le serveur MCP.** Le refus se produit dans
l'orchestrateur, avant toute communication reseau vers l'outil. C'est ce qui
permet de garantir qu'un outil interdit n'est jamais execute, quelle que soit la
formulation du prompt.
