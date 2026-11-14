import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import threading
from unittest import mock

from cbpi.controller.system_controller import SystemController
from cbpi.http_endpoints.http_system import SystemHttpEndpoints

SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER = "fedcba9876543210fedcba9876543210fedcba98"


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def test_commit_of_a_checkout():
    with tempfile.TemporaryDirectory() as root:
        git = os.path.join(root, ".git")
        write(os.path.join(git, "HEAD"), "ref: refs/heads/main\n")
        write(os.path.join(git, "refs", "heads", "main"), SHA + "\n")
        assert SystemController._source_commit(root) == "0123456"
        # the branch only in packed-refs, as after git gc
        os.remove(os.path.join(git, "refs", "heads", "main"))
        write(os.path.join(git, "packed-refs"), "# pack-refs with: peeled fully-peeled sorted\n"
                                                f"{OTHER} refs/heads/other\n{SHA} refs/heads/main\n")
        assert SystemController._source_commit(root) == "0123456"
        # a commit checked out by itself
        write(os.path.join(git, "HEAD"), OTHER + "\n")
        assert SystemController._source_commit(root) == "fedcba9"


def test_commit_of_a_worktree():
    with tempfile.TemporaryDirectory() as top:
        main = os.path.join(top, "main", ".git")
        tree = os.path.join(top, "tree")
        wt = os.path.join(main, "worktrees", "tree")
        write(os.path.join(tree, ".git"), f"gitdir: {wt}\n")
        write(os.path.join(wt, "HEAD"), "ref: refs/heads/feature\n")
        write(os.path.join(wt, "commondir"), "../..\n")
        write(os.path.join(main, "refs", "heads", "feature"), OTHER + "\n")
        assert SystemController._source_commit(tree) == "fedcba9"


def test_no_commit_without_a_checkout():
    with tempfile.TemporaryDirectory() as root:
        assert SystemController._source_commit(root) is None
        # a branch with no commits yet
        write(os.path.join(root, ".git", "HEAD"), "ref: refs/heads/main\n")
        assert SystemController._source_commit(root) is None


def test_commit_of_this_checkout_matches_git():
    root = str(SystemController.SOURCE_ROOT)
    if shutil.which("git") is None or not os.path.exists(os.path.join(root, ".git")):
        return
    head = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
    if head.returncode == 0:
        assert SystemController._source_commit(root) == head.stdout.strip()[:7]


def test_found_once_at_start_up_off_the_event_loop():
    ctrl = SystemController.__new__(SystemController)
    ctrl.commit = None
    ran = []

    def source_commit(root):
        ran.append(threading.current_thread())
        return "abc1234"

    async def start():
        with mock.patch.object(SystemController, "_source_commit", side_effect=source_commit):
            await ctrl.find_commit(None)
        return threading.current_thread()

    loop_thread = asyncio.run(start())
    assert ctrl.commit == "abc1234"
    assert len(ran) == 1 and ran[0] is not loop_thread


def test_system_state_has_the_commit_beside_the_version():
    cbpi = mock.MagicMock()
    for part in (cbpi.actor, cbpi.fermenter, cbpi.sensor, cbpi.kettle, cbpi.step, cbpi.config, cbpi.notification,
                 cbpi.resttimer, cbpi.timers):
        part.get_state.return_value = {}
    cbpi.fermenter.get_fermenter_steps.return_value = []
    cbpi.config.get.return_value = "No"
    cbpi.plugin.load_plugin_list = mock.AsyncMock(return_value=[{"Version": "0.5.1"}])
    cbpi.upload.get_brewfather_recipes = mock.AsyncMock(return_value=[])
    ep = SystemHttpEndpoints.__new__(SystemHttpEndpoints)
    ep.cbpi = cbpi
    ep.controller = mock.Mock(commit="abc1234")
    data = json.loads(asyncio.run(ep.state(None)).text)
    assert data["commit"] == "abc1234"
    assert "+" not in data["version"]
    ep.controller.commit = None
    assert json.loads(asyncio.run(ep.state(None)).text)["commit"] is None
