# Installation

Ce document decrit `install.sh`, le choix du modele d'inference et le depannage
des cas courants. Le reste du projet est decrit dans le [README](../README.md).

## Principe

`install.sh` doit etre la premiere commande apres un `git clone`. Il est
idempotent : relance sur une installation existante, il ne refait que ce qui
manque. Il ne suppose aucun chemin absolu, donc il fonctionne depuis n'importe
quel dossier.

Il enchaine sept etapes :

1. Verification des prerequis, et installation de ce qui manque (paquets
   systeme, Ollama, Docker sur Linux natif) apres confirmation.
2. Choix du modele d'inference (menu, avec preselection selon la machine).
3. Recapitulatif et confirmation (sauf `--yes`).
4. Ecriture de `.env` et report du choix dans `gateway/config.yaml`.
5. `scripts/bootstrap.sh` : environnements Python, cles JWT, telechargement du modele.
6. `scripts/setup_db.sh` puis `scripts/up.sh` : PostgreSQL, gateway, serveurs MCP, orchestrateur.
7. `scripts/openwebui.sh up|setup` puis verification de bout en bout.

## Dependances installees automatiquement

Sur une machine vierge, `install.sh` ne s'arrete pas : il liste ce qui manque,
demande confirmation, puis installe. Ce qui est couvert :

| Element | Moyen | Remarque |
| --- | --- | --- |
| `curl`, `jq` | gestionnaire de paquets (`apt`, `dnf`, `pacman`) | `pacman` sur Arch |
| Python 3.11+ et `venv` | paquets `python3`, `python3-venv`, `python3-pip` | necessaire aux environnements du projet |
| Ollama | script officiel `ollama.com/install.sh` | installe le binaire et le service |
| Docker | script officiel `get.docker.com` | **Linux natif seulement** |

Deux cas particuliers :

- **Sous WSL**, Docker n'est jamais installe par le script : il vient de Docker
  Desktop, cote Windows. Sans Docker, la pile demarre quand meme (mode cle
  maitresse seule, sans PostgreSQL).
- **Docker fraichement installe sur Linux natif** : l'utilisateur est ajoute au
  groupe `docker`, mais la session en cours ne l'a pas encore. Reconnectez-vous,
  ou lancez `newgrp docker`, puis `./scripts/setup_db.sh`.

Un mot de passe `sudo` peut etre demande. Pour n'installer aucune dependance,
utiliser `--no-auto-install` : le script s'arrete alors en listant ce qui
manque.

Le labo utilise deux alias (`planner` et `writer`) qui pointent vers le meme
modele physique. Le choix se fait donc une seule fois.

| Modele | Taille | Contexte (planner/writer) | Pour qui |
| --- | --- | --- | --- |
| `qwen3.5:4b` | 3.3 Go | 8192 / 16384 | Defaut. CPU recent, 16 Go de RAM |
| `qwen3.5:2b` | 2.7 Go | 4096 / 8192 | PC sans GPU, 8 Go de RAM |
| `qwen3.5:0.8b` | 1.2 Go | 4096 / 4096 | Machine tres faible, tests de plomberie |
| `qwen3.5:9b` | 6.6 Go | 8192 / 16384 | GPU, ou CPU avec beaucoup de patience |

Il n'existe pas de `qwen3.5:3b` dans la bibliotheque Ollama : les tailles
disponibles sont 0.8b, 2b, 4b, 9b, 27b, 35b et 122b.

Le contexte (`num_ctx`) compte autant que le nombre de parametres : un 2b avec
16k de contexte consomme presque autant de memoire qu'un 4b avec 8k. Si la
machine rame ou si Ollama est tue par le noyau (memoire insuffisante), reduire
d'abord `num_ctx`.

Changer de modele plus tard, sans reinstalle :

```bash
python3 scripts/set_model.py --model qwen3.5:2b --planner-ctx 4096 --writer-ctx 8192
ollama pull qwen3.5:2b
./scripts/down.sh && ./scripts/up.sh
```

`scripts/set_model.py --show` affiche l'etat courant.

## Options

| Option | Effet |
| --- | --- |
| `--model <nom>` | Impose le modele, saute le menu |
| `--planner-ctx <n>` | Fenetre de contexte de l'alias planner |
| `--writer-ctx <n>` | Fenetre de contexte de l'alias writer |
| `--yes` | Aucune question, defauts partout |
| `--no-docker` | Saute PostgreSQL (LiteLLM en mode cle maitresse seule) |
| `--skip-openwebui` | Installe la pile sans l'interface |
| `--dry-run` | Affiche le plan sans rien modifier |
| `--help` | Aide |

## Prerequis par systeme

**Windows (WSL2).** Lancer `install.cmd` a la racine du clone (double-clic ou
`install.cmd` depuis le dossier). Il convertit le chemin et execute `install.sh`
dans la distribution WSL par defaut. Si WSL2 ou une distribution Linux manque, il
propose de lancer `wsl --install` (droits administrateur et redemarrage).

Les dependances Linux (Python, Ollama, jq) sont installees par `install.sh` dans
WSL, apres confirmation. Seul Docker Desktop reste a installer cote Windows.

**Linux.** `python3` et `python3-venv`, `curl`, `docker` (ou Podman avec le
demon compatible), Ollama via `curl -fsSL https://ollama.com/install.sh | sh`.

**macOS.** Ollama et Docker Desktop, Python 3.11+ via Homebrew. Les chemins de
`common.sh` sont relatifs, rien a adapter.

## Ordre de demarrage (piege principal)

LiteLLM est un processus hote qui applique ses migrations Prisma au demarrage.
Si PostgreSQL n'est pas joignable a cet instant, il quitte avec :

```
Prisma error: P1001: Can't reach database server at 127.0.0.1:5433
```

C'est exactement ce qui arrive quand Docker Desktop n'est pas lance : l'erreur
remonte ensuite dans Open WebUI sous la forme
`[gateway indisponible] gateway injoignable : All connection attempts failed`.

Regle : **Docker Desktop d'abord, puis `./scripts/up.sh`.**

## Depannage

La premiere chose a lancer en cas de doute :

```bash
./scripts/doctor.sh          # lecture seule, aucune modification
./scripts/doctor.sh --fix    # repare ce qui peut l'etre, puis re-verifie
./scripts/doctor.sh --full   # ajoute un appel reel au modele
```

Il parcourt la pile dans l'ordre des dependances (Docker, PostgreSQL, gateway,
Ollama, MCP, orchestrateur, Open WebUI) et affiche une ligne par couche. Une
couche en echec n'arrete pas le diagnostic : on obtient la photo complete.

| Symptome | Cause probable | Action |
| --- | --- | --- |
| `gateway injoignable` dans Open WebUI | gateway morte (souvent P1001) | `./scripts/doctor.sh --fix` |
| `docker: command not found` | Docker Desktop ferme | demarrer Docker Desktop |
| Ollama ne repond pas | service arrete | `ollama serve` ou relancer l'application |
| Reponse tres lente | modele trop gros pour le CPU | choisir 2b ou 0.8b |
| Ollama tue sans message | memoire insuffisante | baisser `num_ctx` puis retester |
| Port 3000 ou 4000 deja pris | autre service | changer `OPENWEBUI_PORT` dans `.env` |
| Modeles vides dans Open WebUI | JWT expire | `./scripts/openwebui.sh connect` |
| `install.cmd` dit que WSL2 est absent | WSL non installe, ou distribution par defaut absente | PowerShell admin : `wsl --install`, redemarrer |
| `install.cmd` : chemin WSL vide | clone dans un chemin UNC (`\\wsl$\...`) | cloner sur `C:\`, ou lancer `bash install.sh` depuis WSL |

## Delais d'attente

Les scripts attendent volontairement longtemps avant de declarer une panne :

| Etape | Delai | Pourquoi |
| --- | --- | --- |
| Gateway au demarrage | 180 s | migrations Prisma et chaine d'outils au premier lancement |
| Ollama au demarrage | 90 s | chargement du modele en memoire |
| Appel de modele | 600 s | premiere inference CPU d'un modele 4b |

Pendant l'attente, une ligne `... gateway : toujours en cours (15s)` montre que le
script avance. Un message `ne repond pas apres 60s` signifiait souvent un delai
trop court, pas une panne : c'est corrige.

Les delais sont reglables :

```bash
./scripts/doctor.sh --gateway-timeout 300 --model-timeout 900
DELAI_MODELE=900 ./install.sh --yes
```

## Ou sont les secrets

Rien de secret n'est versionne. Tout est genere a l'installation :

- `.env` : cles de la gateway, chaine PostgreSQL, secret de session.
- `config/keys/` : paire de cles JWT de l'orchestrateur.
- `var/openwebui-admin.txt` : compte administrateur de l'interface.
- `var/openwebui-token.txt` : JWT de service utilise par Open WebUI.

Aucune de ces valeurs n'est affichee pendant l'installation.

## Arret et desinstallation

```bash
./scripts/down.sh              # arret, Ollama conserve
./scripts/down.sh --all        # arret complet, Ollama inclus
```

Pour repartir de zero : arreter la pile, puis
`docker rm -f litellm-postgres open-webui orchestrator-lab`,
`docker volume rm litellm-pgdata open-webui-data`, et supprimer `.env`,
`config/keys/` et `var/`. Les environnements Python (`.venv*`) peuvent etre
supprimes sans risque : ils sont recrees par `install.sh`.
