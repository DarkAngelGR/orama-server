"""Run Orama's web app: ``python -m orama``.

Serves :func:`orama.web.create_app` for the state folder in ``ORAMA_STATE_DIR`` with
waitress on port 8765 (all addresses inside the container). Waitress sets the client
address from the socket peer; no forwarded-for header is trusted.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import waitress

from orama.requests.client import MediaClient
from orama.requests.store import RequestStore
from orama.requests.worker import RequestWorker
from orama.secrets import SecretStore, install_redaction
from orama.setup_model import SetupReader
from orama.web import MAX_REQUEST_BYTES, SECRETS_FILE, create_app
from orama.wiring.http import Http

HOST = "0.0.0.0"
PORT = 8765
THREADS = 8
SERVE_OPTIONS = {
    "host": HOST,
    "port": PORT,
    "threads": THREADS,
    "ident": None,  # no Server header
    # Waitress refuses a larger body itself, before buffering it or calling the app.
    "max_request_body_size": MAX_REQUEST_BYTES,
}


def main() -> None:
    state_dir = Path(os.environ["ORAMA_STATE_DIR"])
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    # Every log line passes the redaction filter: stored secret values become ***.
    # The store may be created later (by apply); the filter reads it on each record.
    secret_store = SecretStore(state_dir / SECRETS_FILE)
    install_redaction(secret_store)

    db_path = state_dir / "requests.db"
    request_store = RequestStore(db_path)
    setup_reader = SetupReader(state_dir)
    worker = RequestWorker(
        store=request_store,
        client_factory=lambda: MediaClient(setup_reader, secret_store, Http()),
    )
    worker.start()
    try:
        app = create_app(state_dir, request_worker=worker)
        waitress.serve(app, **SERVE_OPTIONS)
    finally:
        worker.stop()


if __name__ == "__main__":
    main()
