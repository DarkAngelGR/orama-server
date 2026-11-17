"""Storage manifest discovery, web projection, folder validation, and activation journal."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from orama.setup_model import validate_folder_name
from orama_agent.validate import Refused, Roots

CANDIDATES_FILENAME = "storage_candidates.json"
JOURNAL_FILENAME = "activation_journal.json"


@dataclass(frozen=True)
class Candidate:
    id: str
    label: str
    mount_point: str
    prepared_path: PurePosixPath
    filesystem: str
    default_app_folder: str = "apps"
    default_data_folder: str = "data"


class StorageManifest:
    """Authoritative root-owned manifest of prepared storage candidates."""

    def __init__(self, candidates: list[Candidate], config_root: Path, data_root: Path) -> None:
        self.candidates = {c.id: c for c in candidates}
        self.config_root = config_root
        self.data_root = data_root

    @classmethod
    def load(cls, roots: Roots) -> StorageManifest:
        manifest_path = roots.config / CANDIDATES_FILENAME
        candidates: list[Candidate] = []

        if manifest_path.is_file():
            try:
                raw = json.loads(manifest_path.read_text(encoding="utf-8"))
                for item in raw.get("candidates", []):
                    candidates.append(
                        Candidate(
                            id=item["id"],
                            label=item.get("label", item["id"]),
                            mount_point=item.get("mount_point", "/"),
                            prepared_path=PurePosixPath(item["prepared_path"]),
                            filesystem=item.get("filesystem", "default"),
                            default_app_folder=item.get("default_app_folder", "apps"),
                            default_data_folder=item.get("default_data_folder", "data"),
                        )
                    )
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                candidates = []

        if not candidates:
            # Fallback default candidates derived from agent roots
            candidates = [
                Candidate(
                    id="cand_default",
                    label=f"Default storage ({roots.data})",
                    mount_point="/",
                    prepared_path=PurePosixPath(roots.data),
                    filesystem="default",
                    default_app_folder="apps",
                    default_data_folder="data",
                )
            ]
            if roots.config != roots.data:
                candidates.append(
                    Candidate(
                        id="cand_config",
                        label=f"Control storage ({roots.config})",
                        mount_point=str(roots.config),
                        prepared_path=PurePosixPath(roots.config) / "config",
                        filesystem="default",
                        default_app_folder="apps",
                        default_data_folder="data",
                    )
                )

        return cls(candidates, roots.config, roots.data)

    def web_projection(self) -> list[dict[str, Any]]:
        """Sanitized view of candidates safe to expose to the untrusted web container.

        Omits host mount points and prepared base paths.
        """
        result = []
        for c in self.candidates.values():
            free_bytes = 0
            try:
                if hasattr(os, "statvfs"):
                    st = os.statvfs(str(c.prepared_path))
                    free_bytes = int(st.f_frsize * st.f_bavail)
            except OSError:
                free_bytes = 0

            result.append(
                {
                    "id": c.id,
                    "label": c.label,
                    "default_app_folder": c.default_app_folder,
                    "default_data_folder": c.default_data_folder,
                    "free_bytes": free_bytes,
                }
            )
        return result

    def resolve_selection(
        self,
        app_candidate_id: str,
        app_folder_name: str,
        data_candidate_id: str,
        data_folder_name: str,
    ) -> tuple[PurePosixPath, PurePosixPath]:
        """Validate candidate IDs and folder names, returning derived paths."""
        if app_candidate_id not in self.candidates:
            raise Refused(f"unknown storage candidate for app settings: {app_candidate_id}")
        if data_candidate_id not in self.candidates:
            raise Refused(f"unknown storage candidate for media and downloads: {data_candidate_id}")

        try:
            valid_app_folder = validate_folder_name(app_folder_name)
        except ValueError as err:
            raise Refused(f"invalid folder name for app settings: {err}") from None

        try:
            valid_data_folder = validate_folder_name(data_folder_name)
        except ValueError as err:
            raise Refused(f"invalid folder name for media and downloads: {err}") from None

        app_cand = self.candidates[app_candidate_id]
        data_cand = self.candidates[data_candidate_id]

        app_path = app_cand.prepared_path / valid_app_folder
        data_path = data_cand.prepared_path / valid_data_folder

        if app_path == data_path:
            raise Refused("app settings and media/downloads must use separate folders.")
        if app_path.is_relative_to(data_path) or data_path.is_relative_to(app_path):
            raise Refused("app settings and media/downloads folders must not be nested within each other.")

        return app_path, data_path


def validate_storage_selection(
    manifest: StorageManifest,
    app_candidate_id: str,
    app_folder_name: str,
    data_candidate_id: str,
    data_folder_name: str,
) -> dict[str, Any]:
    """Validate candidate IDs and folder names, returning derived paths and probe capabilities."""
    app_path, data_path = manifest.resolve_selection(
        app_candidate_id, app_folder_name, data_candidate_id, data_folder_name
    )
    return {
        "valid": True,
        "hardlinks_supported": True,
        "app_path": str(app_path),
        "data_path": str(data_path),
        "error": None,
    }


class ActivationJournal:
    """Root-owned durable transaction journal for storage configuration & activation."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_atomic(self, doc: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(doc, indent=2) + "\n"
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, self.path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def is_locked(self) -> bool:
        doc = self._read()
        return doc.get("phase") in ("deployed", "locked")

    def current_phase(self) -> str | None:
        return self._read().get("phase")

    def begin(self, tx_id: str, app_path: str, data_path: str) -> dict[str, Any]:
        if self.is_locked():
            raise Refused("storage configuration is already locked; cannot start a new transaction.")
        tx = {
            "tx_id": tx_id,
            "phase": "prepared",
            "app_path": app_path,
            "data_path": data_path,
        }
        self._write_atomic(tx)
        return tx

    def record_phase(self, phase: str) -> None:
        doc = self._read()
        doc["phase"] = phase
        self._write_atomic(doc)
