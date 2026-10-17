# Diagramme d'activite : choix et autorisation des outils

Ce diagramme repond a la question "que se passe-t-il exactement entre le moment
ou l'utilisateur envoie son message et le moment ou un outil est appele ?", avec
les points de refus.

![03-activite-outils](03-activite-outils.png)

```mermaid
flowchart TD
    START([Message recu + jeton JWT]) --> VERIFY{Signature, expiration,<br/>audience, claims,<br/>jti revoque ?}
    VERIFY -- non --> R401[401 Unauthorized<br/>raison volontairement generique]
    VERIFY -- oui --> SIZE{Taille du message<br/>sous le plafond GPO ?}
    SIZE -- non --> R403A[403 + X-Policy-Rule:<br/>limits.max_prompt_chars]
    SIZE -- oui --> GRANTS[Calcul des droits :<br/>union des roles du jeton]

    GRANTS --> VISIBLE[Filtrage du catalogue MCP :<br/>seuls les outils accordes<br/>sont transmis au modele]

    VISIBLE --> ANY{Outils<br/>visibles ?}
    ANY -- non --> DIRECT[Le modele est appele sans outil :<br/>il repond en conversation]
    ANY -- oui --> PLAN[Le modele propose<br/>des appels d'outils]

    PLAN --> LOOP{Pour chaque appel propose}
    LOOP --> KNOWN{L'outil existe<br/>dans le registre ?}
    KNOWN -- non --> D1[Refus : outil inconnu]
    KNOWN -- oui --> GRANT{L'outil est-il accorde<br/>aux roles du porteur ?}

    GRANT -- non --> D2[Refus : non accorde<br/>deny by default]
    GRANT -- oui --> QUOTA{Quota d'appels<br/>par requete respecte ?}
    QUOTA -- non --> D3[Refus : quota atteint]
    QUOTA -- oui --> FITS{Taille des arguments<br/>sous la limite de l'outil ?}
    FITS -- non --> D4[Refus : entree trop longue]
    FITS -- oui --> VALUES{Valeurs autorisees<br/>ex : framework ?}
    VALUES -- non --> D5[Refus : valeur hors liste blanche]
    VALUES -- oui --> RATE{Limite de debit<br/>de l'utilisateur ?}
    RATE -- non --> D6[Refus : trop d'appels]
    RATE -- oui --> CALL[Appel MCP tools/call]

    CALL --> RESULT{Succes ?}
    RESULT -- non --> D7[Trace en erreur]
    RESULT -- oui --> COLLECT[Resultat collecte]

    D1 --> NEXT[Appel suivant]
    D2 --> NEXT
    D3 --> NEXT
    D4 --> NEXT
    D5 --> NEXT
    D6 --> NEXT
    D7 --> NEXT
    COLLECT --> NEXT
    NEXT --> LOOP

    LOOP -- termine --> HAVE{Un resultat<br/>disponible ?}
    HAVE -- non, mais le modele<br/>a repondu --> ANSWER1[Reponse directe du modele]
    HAVE -- non, refus seulement --> ANSWER2[Explication du refus<br/>a l'utilisateur]
    HAVE -- oui --> SYNTH[Synthese par le modele redacteur]

    ANSWER1 --> AUDIT
    ANSWER2 --> AUDIT
    SYNTH --> AUDIT
    DIRECT --> AUDIT
    AUDIT[Ecriture du journal d'audit] --> END([Reponse HTTP])

    R401 --> END
    R403A --> END
```

## Deux idees a retenir de ce diagramme

**Les refus n'arretent pas la requete.** Chaque appel refuse est enregistre dans
`denials` et la boucle continue : l'utilisateur obtient une reponse exploitable
qui explique ce qui a ete refuse et pourquoi, au lieu d'une erreur opaque. En
revanche, la requete est refusee en bloc (403) si le message lui-meme viole un
plafond global : inutile de faire travailler le modele.

**Six portes avant l'execution, et une seule apres.** Les controles sont tous
places avant l'appel reseau. Rien n'est filtre "apres coup" : un outil interdit
n'est jamais execute, meme partiellement, meme pour "verifier".

**La sortie du modele n'est jamais une entree de confiance.** Elle est traitee
comme une proposition, exactement comme le serait une entree utilisateur.

## Ou toucher pour changer un comportement

| Comportement | Fichier a modifier |
| --- | --- |
| Ajouter ou retirer un droit | `config/policies.yaml` |
| Changer un plafond de taille ou de debit | `config/policies.yaml` (section `limits` ou par outil) |
| Ajouter un outil | nouveau dossier dans `mcp_servers/`, puis URL dans `.env` |
| Changer la maniere dont le modele choisit | `orchestrator/app/llm/planner.py` |
| Changer la redaction de la reponse finale | `orchestrator/app/llm/synthesizer.py` |
| Changer ce qui est trace | `orchestrator/app/audit.py` et les appels dans `pipeline.py` |
