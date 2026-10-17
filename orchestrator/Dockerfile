# Image de l'orchestrateur, utilisee comme backend OpenAI par Open WebUI.
#
# Pourquoi un conteneur : l'orchestrateur n'a plus a etre un service visible sur
# l'hote. Il tourne sur le meme reseau Docker que Open WebUI, qui l'appelle par
# son nom (http://orchestrator-lab:8090/v1). Aucun port n'est publie : le port
# 8090 n'existe plus cote machine.
#
# Le code et les dependances sont figes dans l'image ; la configuration
# (policies.yaml, cles JWT, jeton revoque) et le journal d'audit sont montes au
# demarrage : ils restent sur l'hote et ne sont jamais cuits dans l'image.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY orchestrator/requirements.txt /app/requirements.txt
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir -r /app/requirements.txt

COPY orchestrator /app/orchestrator

# Ces repertoires sont des points de montage : rien de sensible dans l'image.
RUN mkdir -p /app/config /app/var/logs

EXPOSE 8090

CMD ["python", "-m", "uvicorn", "orchestrator.app.main:app", \
     "--host", "0.0.0.0", "--port", "8090"]
