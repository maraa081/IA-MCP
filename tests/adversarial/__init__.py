"""Tests adversariaux : ce qu'un contenu hostile peut obtenir du systeme.

Les tests de ce dossier remplacent la gateway et les serveurs MCP par des
doublures. Ils verifient trois choses pour chaque scenario :

  1. ce qui est EXECUTE (aucun outil interdit ne doit passer) ;
  2. ce qui est AUDITE (le refus et sa regle doivent etre tracables) ;
  3. ce que recoit l'UTILISATEUR (un message clair, sans fuite de secret).

Un test marque `xfail(strict=True)` documente un risque residuel connu : il
echoue volontairement, et le rapport genere par `conftest.py` le classe
« residuel ». Un test qui passe est classe « bloque ».
"""
