import json
from pathlib import Path

import pytest

from orama.agent_client import AgentClient
from orama_agent.runner import FakeRunner
from orama_agent.server import Agent
from orama_agent.storage import CANDIDATES_FILENAME, JOURNAL_FILENAME, ActivationJournal
from orama_agent.validate import Refused, Roots


@pytest.fixture
def roots(tmp_path):
    cfg = tmp_path / "cfg"
    data = tmp_path / "data"
    cfg.mkdir()
    data.mkdir()
    return Roots(data=Path(data), config=Path(cfg), puid=1000, pgid=1001)


@pytest.fixture
def agent(roots):
    runner = FakeRunner()
    return Agent(roots, runner, chown=lambda *a, **k: None)


def test_storage_candidates_default(agent, roots):
    result = agent.handle("storage-candidates", {})
    assert isinstance(result, list)
    assert len(result) >= 1
    default_cand = next(c for c in result if c["id"] == "cand_default")
    assert default_cand["default_app_folder"] == "apps"
    assert default_cand["default_data_folder"] == "data"
    assert "mount_point" not in default_cand
    assert "prepared_path" not in default_cand


def test_storage_candidates_from_manifest(agent, roots):
    manifest_file = roots.config / CANDIDATES_FILENAME
    manifest_data = {
        "version": 1,
        "candidates": [
            {
                "id": "cand_ssd",
                "label": "Fast SSD (/mnt/fast)",
                "mount_point": "/mnt/fast",
                "prepared_path": "/mnt/fast/orama-apps",
                "filesystem": "btrfs",
                "default_app_folder": "apps",
                "default_data_folder": "data",
            },
            {
                "id": "cand_hdd",
                "label": "Mass HDD (/mnt/storage)",
                "mount_point": "/mnt/storage",
                "prepared_path": "/mnt/storage/orama-data",
                "filesystem": "zfs",
                "default_app_folder": "apps",
                "default_data_folder": "data",
            },
        ],
    }
    manifest_file.write_text(json.dumps(manifest_data), encoding="utf-8")

    result = agent.handle("storage-candidates", {})
    assert len(result) == 2
    assert result[0]["id"] == "cand_ssd"
    assert result[0]["label"] == "Fast SSD (/mnt/fast)"
    assert "mount_point" not in result[0]
    assert "prepared_path" not in result[0]
    assert result[1]["id"] == "cand_hdd"


def test_storage_candidates_rejects_arguments(agent):
    with pytest.raises(Refused, match="no arguments"):
        agent.handle("storage-candidates", {"extra": "arg"})


def test_storage_validate_success(agent, roots):
    manifest_file = roots.config / CANDIDATES_FILENAME
    manifest_data = {
        "version": 1,
        "candidates": [
            {
                "id": "cand_apps",
                "label": "Apps Pool",
                "mount_point": "/mnt/apps",
                "prepared_path": "/mnt/apps/orama",
                "filesystem": "ext4",
            },
            {
                "id": "cand_data",
                "label": "Media Pool",
                "mount_point": "/mnt/data",
                "prepared_path": "/mnt/data/orama",
                "filesystem": "zfs",
            },
        ],
    }
    manifest_file.write_text(json.dumps(manifest_data), encoding="utf-8")

    result = agent.handle(
        "storage-validate",
        {
            "app_candidate_id": "cand_apps",
            "app_folder_name": "my-apps",
            "data_candidate_id": "cand_data",
            "data_folder_name": "my-media",
        },
    )
    assert result["valid"] is True
    assert result["hardlinks_supported"] is True
    assert result["app_path"] == "/mnt/apps/orama/my-apps"
    assert result["data_path"] == "/mnt/data/orama/my-media"


def test_storage_validate_refuses_invalid_candidates(agent, roots):
    with pytest.raises(Refused, match="unknown storage candidate"):
        agent.handle(
            "storage-validate",
            {
                "app_candidate_id": "cand_unknown",
                "app_folder_name": "apps",
                "data_candidate_id": "cand_default",
                "data_folder_name": "data",
            },
        )


def test_storage_validate_refuses_traversal_and_bad_characters(agent, roots):
    for bad_name in ("../bad", "bad/folder", "name:with:colons", "has spaces", ".hidden", ""):
        with pytest.raises(Refused, match="invalid folder name"):
            agent.handle(
                "storage-validate",
                {
                    "app_candidate_id": "cand_default",
                    "app_folder_name": bad_name,
                    "data_candidate_id": "cand_default",
                    "data_folder_name": "data",
                },
            )


def test_storage_validate_refuses_identical_or_nested(agent, roots):
    with pytest.raises(Refused, match="separate folders"):
        agent.handle(
            "storage-validate",
            {
                "app_candidate_id": "cand_default",
                "app_folder_name": "same_dir",
                "data_candidate_id": "cand_default",
                "data_folder_name": "same_dir",
            },
        )


def test_storage_lock_and_subsequent_validate(agent, roots):
    lock_result = agent.handle("storage-lock", {})
    assert lock_result["locked"] is True

    journal = ActivationJournal(roots.config / JOURNAL_FILENAME)
    assert journal.is_locked() is True

    with pytest.raises(Refused, match="Storage configuration is locked"):
        agent.handle(
            "storage-validate",
            {
                "app_candidate_id": "cand_default",
                "app_folder_name": "apps",
                "data_candidate_id": "cand_default",
                "data_folder_name": "data",
            },
        )


def test_agent_client_storage_methods(monkeypatch):
    client = AgentClient(socket_path="/fake/agent.sock")
    calls = []

    def fake_call(cmd, args, timeout=None):
        calls.append((cmd, args))
        if cmd == "storage-candidates":
            return [{"id": "cand_01", "label": "Storage 1"}]
        if cmd == "storage-validate":
            return {"valid": True, "hardlinks_supported": True}
        if cmd == "storage-lock":
            return {"locked": True}
        raise AssertionError(f"unexpected cmd {cmd}")

    monkeypatch.setattr(client, "_call", fake_call)

    cands = client.get_storage_candidates()
    assert cands == [{"id": "cand_01", "label": "Storage 1"}]
    assert calls[-1] == ("storage-candidates", {})

    val = client.validate_storage("cand_01", "apps", "cand_01", "data")
    assert val["valid"] is True
    assert calls[-1] == (
        "storage-validate",
        {
            "app_candidate_id": "cand_01",
            "app_folder_name": "apps",
            "data_candidate_id": "cand_01",
            "data_folder_name": "data",
        },
    )

    locked = client.lock_storage()
    assert locked is True
    assert calls[-1] == ("storage-lock", {})
