"""Unit tests for orama_agent storage manifest, validation helper, and activation journal."""

import json
from pathlib import PurePosixPath

import pytest

from orama_agent.storage import (
    ActivationJournal,
    Refused,
    StorageManifest,
    validate_storage_selection,
)
from orama_agent.validate import Roots


def test_storage_manifest_loads_and_projects_sanitized(tmp_path):
    manifest_file = tmp_path / "storage_candidates.json"
    doc = {
        "version": 1,
        "config_root": "/opt/orama",
        "data_root": "/data",
        "candidates": [
            {
                "id": "cand_default",
                "label": "Default storage (/data)",
                "mount_point": "/",
                "prepared_path": "/data",
                "filesystem": "ext4",
                "default_app_folder": "apps",
                "default_data_folder": "data",
            },
            {
                "id": "cand_vol2",
                "label": "Volume 2 (/volume2)",
                "mount_point": "/volume2",
                "prepared_path": "/volume2/orama-data",
                "filesystem": "btrfs",
                "default_app_folder": "apps",
                "default_data_folder": "data",
            },
        ],
    }
    manifest_file.write_text(json.dumps(doc))

    roots = Roots(data=tmp_path / "d", config=tmp_path, puid=1000, pgid=1000)
    manifest = StorageManifest.load(roots)

    projection = manifest.web_projection()
    assert len(projection) == 2
    # Projection must expose opaque IDs and labels, but not internal prepared_path or mount_point
    p0 = projection[0]
    assert p0["id"] == "cand_default"
    assert p0["label"] == "Default storage (/data)"
    assert p0["default_app_folder"] == "apps"
    assert p0["default_data_folder"] == "data"
    assert "prepared_path" not in p0
    assert "mount_point" not in p0


def test_storage_manifest_defaults_when_file_missing(tmp_path):
    roots = Roots(data=tmp_path / "data", config=tmp_path / "cfg", puid=1000, pgid=1000)
    manifest = StorageManifest.load(roots)

    projection = manifest.web_projection()
    assert len(projection) >= 1
    ids = [c["id"] for c in projection]
    assert "cand_default" in ids


def test_storage_manifest_resolves_paths_with_validation(tmp_path):
    manifest_file = tmp_path / "storage_candidates.json"
    doc = {
        "version": 1,
        "config_root": "/opt/orama",
        "data_root": "/data",
        "candidates": [
            {
                "id": "cand_default",
                "label": "Default (/data)",
                "mount_point": "/",
                "prepared_path": "/data",
                "filesystem": "ext4",
                "default_app_folder": "apps",
                "default_data_folder": "data",
            }
        ],
    }
    manifest_file.write_text(json.dumps(doc))

    roots = Roots(data=tmp_path / "d", config=tmp_path, puid=1000, pgid=1000)
    manifest = StorageManifest.load(roots)

    app_path, data_path = manifest.resolve_selection(
        app_candidate_id="cand_default",
        app_folder_name="my_apps",
        data_candidate_id="cand_default",
        data_folder_name="my_media",
    )
    assert app_path == PurePosixPath("/data/my_apps")
    assert data_path == PurePosixPath("/data/my_media")


def test_storage_manifest_refuses_unknown_candidate(tmp_path):
    roots = Roots(data=tmp_path / "d", config=tmp_path, puid=1000, pgid=1000)
    manifest = StorageManifest.load(roots)

    with pytest.raises(Refused, match="unknown storage candidate"):
        manifest.resolve_selection(
            app_candidate_id="cand_nonexistent",
            app_folder_name="apps",
            data_candidate_id="cand_default",
            data_folder_name="data",
        )


def test_storage_manifest_refuses_colliding_folders(tmp_path):
    roots = Roots(data=tmp_path / "d", config=tmp_path, puid=1000, pgid=1000)
    manifest = StorageManifest.load(roots)

    with pytest.raises(Refused, match="separate folders"):
        manifest.resolve_selection(
            app_candidate_id="cand_default",
            app_folder_name="same_folder",
            data_candidate_id="cand_default",
            data_folder_name="same_folder",
        )


def test_validate_storage_selection_helper_checks_names(tmp_path):
    roots = Roots(data=tmp_path / "d", config=tmp_path, puid=1000, pgid=1000)
    manifest = StorageManifest.load(roots)

    with pytest.raises(Refused, match="folder name"):
        validate_storage_selection(
            manifest=manifest,
            app_candidate_id="cand_default",
            app_folder_name="bad/slash",
            data_candidate_id="cand_default",
            data_folder_name="data",
        )


def test_activation_journal_phases_and_locking(tmp_path):
    journal = ActivationJournal(tmp_path / "activation_journal.json")
    assert not journal.is_locked()

    tx = journal.begin(
        tx_id="tx_01",
        app_path="/data/apps",
        data_path="/data/media",
    )
    assert tx["phase"] == "prepared"
    assert not journal.is_locked()

    journal.record_phase("activating")
    assert journal.current_phase() == "activating"
    assert not journal.is_locked()

    journal.record_phase("deployed")
    assert journal.is_locked()

    # Once deployed/locked, starting a new configuration transaction is refused
    with pytest.raises(Refused, match="already locked"):
        journal.begin("tx_02", "/new/apps", "/new/data")
