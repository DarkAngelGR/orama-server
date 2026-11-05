"""Executed inside Orama by the integration test; the host controls the stop/restart."""
import json
import sys
from pathlib import Path

from orama.secrets import SecretStore
from orama.setup_model import DesiredSetup
from orama.wiring.engine import run_steps
from orama.wiring.http import Http
from orama.wiring.jellyfin import jellyfin_steps

setup = DesiredSetup.load(Path(sys.argv[1]))
store = SecretStore(Path(sys.argv[1]).parent / "secrets.bin")
http = Http()
key = store.get("apikey:jellyfin")
headers = {"Authorization": f'MediaBrowser Client="Orama", Device="Orama", '
           f'DeviceId="orama", Version="1", Token="{key}"'}
# Remove only the empty test library, so the interrupted apply has unfinished work.
response = http.request("DELETE", "http://jellyfin:8096/Library/VirtualFolders"
                        "?name=TV%20Shows&refreshLibrary=false", headers=headers)
if response.status != 204:
    raise SystemExit("test library removal failed")
steps = jellyfin_steps(setup, store, http)
for step in steps:
    if step.name == "jellyfin:libraries":
        original = step.run

        def interrupted(original=original):
            print("READY", flush=True)
            if sys.stdin.readline().strip() != "continue":
                raise RuntimeError("test handshake failed")
            return original()

        step.run = interrupted
results = run_steps(steps)
print(json.dumps({r.name: r.status for r in results}), flush=True)
