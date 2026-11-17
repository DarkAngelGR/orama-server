"""The Hermes page remembers a workspace key without keeping a bad one."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_workspace_key_is_remembered_and_a_bad_one_is_not():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to run the browser storage check")
    script = r"""
import { loadWorkspaceKey, saveWorkspaceKey } from './src/hermes/static/core.js';
function mem() {
  const box = new Map();
  return {
    getItem: (key) => (box.has(key) ? box.get(key) : null),
    setItem: (key, value) => box.set(key, String(value)),
    removeItem: (key) => box.delete(key),
  };
}
const store = mem();
const key = 'a'.repeat(32);
if (!saveWorkspaceKey(store, key)) throw new Error('save');
if (loadWorkspaceKey(store) !== key) throw new Error('load');
if (saveWorkspaceKey(store, 'short')) throw new Error('short key stored');
if (loadWorkspaceKey(store) !== key) throw new Error('short key replaced a good one');
if (saveWorkspaceKey(store, 'bad\n' + 'b'.repeat(32))) throw new Error('newline stored');
if (!saveWorkspaceKey(store, '')) throw new Error('clear failed');
if (loadWorkspaceKey(store) !== '') throw new Error('key remained');
const broken = { getItem() { throw new Error('off'); }, setItem() { throw new Error('off'); }, removeItem() { throw new Error('off'); } };
if (loadWorkspaceKey(broken) !== '' || saveWorkspaceKey(broken, key) || saveWorkspaceKey(null, key)) throw new Error('storage errors');
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=ROOT, check=False, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
