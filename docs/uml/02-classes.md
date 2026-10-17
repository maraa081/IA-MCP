# Diagramme de classes

Vue des objets principaux et de leurs relations. Les noms correspondent
exactement aux modules du depot, ce qui permet de passer du diagramme au code
sans chercher.

![02-classes](02-classes.png)
Source PlantUML : [02-classes.puml](02-classes.puml)



## Points de conception visibles sur le diagramme

**Les objets de decision sont immuables.** `AuthenticatedUser`, `ToolGrant`,
`Decision`, `ToolDescriptor` sont declares `frozen` (dataclasses gelees). Une
decision d'autorisation ne peut donc pas etre modifiee apres coup par du code
maladroit : il faut en produire une nouvelle.

**Le pipeline ne construit rien.** `ChatPipeline` recoit ses dependances deja
assemblees par `Services`. C'est ce qui rend le pipeline testable : dans
`orchestrator/tests/test_pipeline.py`, seuls `GatewayClient` et `McpClient` sont
remplaces, tout le reste est le code de production.

**Le modele n'apparait nulle part comme autorite.** `ToolPlanner` produit un
`Plan` (une proposition). Seul `PolicyEngine` produit des `Decision`. La
separation est physique : deux modules, deux responsabilites.

**Un serveur MCP est un processus separe.** Il n'est donc pas dans le meme
diagramme d'objets : c'est une frontiere reseau, materialisee par la fleche
`JSON-RPC / HTTP`.
