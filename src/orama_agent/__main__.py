"""Run the Agent: ``python -m orama_agent``. Roots and ids come from its own environment."""

from __future__ import annotations

import logging
import os

from orama_agent.runner import ComposeRunner
from orama_agent.server import DEFAULT_SOCKET, AgentServer
from orama_agent.validate import Roots


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    roots = Roots.from_env()
    socket_path = os.environ.get("ORAMA_AGENT_SOCKET", DEFAULT_SOCKET)
    with AgentServer(roots, ComposeRunner(roots), socket_path) as server:
        logging.getLogger("orama_agent").info("listening on %s", socket_path)
        server.serve_forever()


if __name__ == "__main__":
    main()
