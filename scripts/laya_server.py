#!/usr/bin/env python3
"""Sidecar HTTP pour le modele de decision Laya (checkpoint multilingue).

Pourquoi un sidecar separe :
  - Laya n'est pas un modele de langage : il ne se sert pas via
    `/v1/chat/completions` et LiteLLM ne proxy pas `/v1/systemone` ;
  - il a ses propres dependances (torch, transformers, package `laya`) et ne
    doit pas alourdir l'image de l'orchestrateur ;
  - il reste optionnel : si ce service est arrete, l'orchestrateur retombe sur
    le planificateur qwen (mode `auto`).

API exposee (volontairement alignee sur Ollama `/v1/systemone`) :

  GET  /health          -> {"status": "ok", "model": "..."}
  POST /v1/systemone    -> {"model": ..., "answers": {...}, "usage": {...}}

Exemple de corps :
  {
    "model": "convaiinnovations/laya-multilingual",
    "state": "Resume-moi ce document : ...",
    "questions": {
      "tool": {
        "type": "choice",
        "instructions": "Quel outil ?",
        "criteria": {"o1": "Resumer un texte", "o2": "Chercher un fichier"}
      }
    }
  }

Lancement :
  ./.venv-laya/bin/python scripts/laya_server.py --host 0.0.0.0 --port 9104
"""

from __future__ import annotations

import argparse
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# transformers sonde TensorFlow a l'import ; quand TF est present, son runtime
# peut bloquer la construction du modele. Le contournement documente par Laya
# est de desactiver cette sonde.
os.environ.setdefault("USE_TF", "0")

import laya  # noqa: E402  (import apres le reglage de l'environnement)

MAX_BODY_BYTES = 2_000_000

_agent: Any = None
_agent_lock = threading.Lock()
_model_name = "convaiinnovations/laya-multilingual"
_max_len = 8192


class LayaHandler(BaseHTTPRequestHandler):
    """Handler minimal : sante + decision."""

    server_version = "laya-sidecar/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        # Journal court : une ligne par requete, sans le corps (confidentialite).
        print(f"[laya] {self.address_string()} {fmt % args}", flush=True)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (nom impose par http.server)
        if self.path == "/health":
            self._send_json(200, {"status": "ok", "model": _model_name})
            return
        self._send_json(404, {"error": "route inconnue"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/systemone":
            self._send_json(404, {"error": "route inconnue"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send_json(400, {"error": "Content-Length invalide"})
            return
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send_json(400, {"error": "corps absent ou trop volumineux"})
            return

        try:
            payload = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "JSON invalide"})
            return

        if not isinstance(payload, dict):
            self._send_json(400, {"error": "le corps doit etre un objet JSON"})
            return

        state = payload.get("state")
        questions = payload.get("questions")
        if state is None:
            self._send_json(400, {"error": "champ 'state' obligatoire"})
            return
        if not isinstance(questions, dict) or not questions:
            self._send_json(400, {"error": "champ 'questions' obligatoire"})
            return

        try:
            # Laya n'est pas concu pour un appel concurrent sur le meme agent :
            # on serialise les predictions. Le cout est de quelques centaines de
            # millisecondes par appel, largement suffisant pour ce labo.
            with _agent_lock:
                result = _agent.predict(state, questions, max_len=_max_len)
        except Exception as exc:  # noqa: BLE001 - on renvoie une erreur lisible
            print(f"[laya] erreur de prediction : {exc}", flush=True)
            self._send_json(500, {"error": f"prediction Laya en echec : {exc}"})
            return

        # `laya` rend un dictionnaire {model, answers, usage}. On garde cette
        # forme ; si une version rend un objet, on degrade proprement.
        if isinstance(result, dict):
            body = result
        else:
            body = {"answers": getattr(result, "answers", result)}

        body.setdefault("model", _model_name)
        self._send_json(200, body)


def main() -> None:
    global _agent, _model_name, _max_len

    parser = argparse.ArgumentParser(description="Sidecar HTTP du modele Laya")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9104)
    parser.add_argument(
        "--model",
        default="convaiinnovations/laya-multilingual",
        help="checkpoint Laya (le checkpoint anglais s'effondre hors anglais)",
    )
    parser.add_argument(
        "--max-len",
        type=int,
        default=8192,
        help="longueur maximale en tokens (le checkpoint multilingue lit jusqu'a 8192)",
    )
    args = parser.parse_args()

    _model_name = args.model
    _max_len = args.max_len

    print(f"[laya] chargement de {_model_name} ...", flush=True)
    _agent = laya.load(_model_name)
    print(f"[laya] modele pret, ecoute sur http://{args.host}:{args.port}", flush=True)

    server = ThreadingHTTPServer((args.host, args.port), LayaHandler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
