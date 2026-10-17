"""Point d'entree de l'orchestrateur.

Lancement :
  .venv/bin/uvicorn orchestrator.app.main:app --host 127.0.0.1 --port 8080

Lecture conseillee du code, dans cet ordre :
  config.py      -> ce qui est parametrable
  container.py   -> ce qui est assemble au demarrage
  pipeline.py    -> ce qui se passe pour une requete (le coeur)
  policy/        -> ce qui decide des droits
  llm/planner.py -> comment le modele choisit ses outils
  mcp/           -> comment les outils sont appeles
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from . import __version__
from .config import Settings, get_settings
from .container import Services
from .routers import auth, chat, meta

# Page de prompt servie par l'API elle-meme : une seule page HTML, sans
# dependance ni etape de build. Elle sert a essayer l'infrastructure depuis un
# navigateur, sans installer de client.
STATIC_DIR = Path(__file__).resolve().parent / "static"

DESCRIPTION = """
Orchestrateur d'outils MCP.

Enchaine : authentification JWT, application d'une politique de droits (GPO),
choix des outils par un modele local, appel des serveurs MCP, synthese.

Ordre de lecture du code : `pipeline.py` decrit le deroulement complet.
"""


def configure_logging(level: str) -> None:
    """Journalisation simple, lisible en console et exploitable en production."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s : %(message)s",
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Fabrique l'application (fabrique = testable avec un autre jeu de reglages)."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    logger = logging.getLogger("orchestrator")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Cycle de vie : on assemble les services au demarrage, on les ferme a l'arret.

        Les services sont ranges dans app.state : les routes y accedent par la
        dependance get_services(), sans jamais rien construire elles-memes.
        """
        app.state.services = await Services.startup(settings)
        logger.info(
            "Orchestrateur pret : %s outils MCP, politique v%s",
            app.state.services.registry.count(),
            app.state.services.policy.version,
        )
        try:
            yield
        finally:
            services: Services | None = getattr(app.state, "services", None)
            if services is not None:
                await services.shutdown()
            logger.info("Orchestrateur arrete")

    app = FastAPI(
        title="Orchestrateur MCP (labo)",
        description=DESCRIPTION,
        version=__version__,
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )

    # --- En-tetes de securite ------------------------------------------------
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        """Ajoute les en-tetes de securite recommandes sur toutes les reponses."""
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        # En production, derriere HTTPS, ajouter aussi :
        # response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    # --- CORS ----------------------------------------------------------------
    if settings.environment == "lab":
        # En labo uniquement : permet d'appeler l'API depuis une page locale.
        # En production on liste explicitement les origines autorisees, et on
        # evite "*" qui, combine a des identifiants, est une faille silencieuse.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://127.0.0.1:8080", "http://localhost:8080"],
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
        )

    # --- Gestion d'erreurs ---------------------------------------------------
    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        """Filet de securite : aucune trace interne ne sort de l'API."""
        logger.exception("Erreur non geree")
        return JSONResponse(status_code=500, content={"detail": "erreur interne"})

    # --- Routes --------------------------------------------------------------
    app.include_router(auth.router)
    app.include_router(chat.router)
    app.include_router(meta.router)

    # --- Interface de test ---------------------------------------------------
    @app.get("/", include_in_schema=False)
    async def racine() -> RedirectResponse:
        """La racine renvoie vers la page de prompt."""
        return RedirectResponse(url="/ui")

    @app.get("/ui", include_in_schema=False)
    async def page_de_prompt() -> FileResponse:
        """Page de prompt (fichier statique servi par l'API)."""
        return FileResponse(STATIC_DIR / "ui.html", media_type="text/html")

    return app


app = create_app()
