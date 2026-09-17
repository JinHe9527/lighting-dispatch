import io
import hashlib
import os
from pathlib import Path
import tarfile
import tempfile
import types
import unittest
from unittest.mock import patch

from scripts.deploy.release import APP_FILES, build_release, verify_release
from scripts.deploy import release


class ReleaseTests(unittest.TestCase):
    revision = "a" * 40

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dispatch-release-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "source"
        for name in APP_FILES:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("release fixture\n", encoding="utf-8")
        (self.source / ".env").write_text("DO_NOT_PACKAGE=secret", encoding="utf-8")
        (self.source / "app/dispatch.db").write_bytes(b"private data")
        self.archive = self.root / "release.tar.gz"
        build_release(self.source, self.revision, self.archive)

    def repack(self, name, contents, kind=tarfile.REGTYPE, duplicate=False):
        target = self.root / "changed.tar.gz"
        with tarfile.open(self.archive, "r:gz") as source, tarfile.open(target, "w:gz") as archive:
            for member in source:
                if member.name != name or duplicate:
                    archive.addfile(member, source.extractfile(member))
            member = tarfile.TarInfo(name)
            member.type = kind
            member.size = len(contents) if kind == tarfile.REGTYPE else 0
            member.linkname = "/etc/passwd" if kind == tarfile.SYMTYPE else ""
            archive.addfile(member, io.BytesIO(contents))
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        Path(str(target) + ".sha256").write_text(f"{digest}  {target.name}\n", encoding="ascii")
        return target

    def test_exact_application_files_and_revision(self):
        verify_release(self.archive, self.revision)
        with tarfile.open(self.archive, "r:gz") as archive:
            self.assertEqual(set(archive.getnames()), {
                "app/server.py", "app/business.py", "app/service.py", "app/index.html",
                "app/manifest.webmanifest", "app/app-icon.svg", "app/app-icon-180.png",
                "app/app-icon-192.png", "app/app-icon-512.png",
                "app/vendor/lucide.min.js", "RELEASE", "SHA256SUMS",
            })
            self.assertEqual(archive.extractfile("RELEASE").read(), (self.revision + "\n").encode())

    def test_modified_application_is_rejected(self):
        changed = self.repack("app/server.py", b"changed code")
        with self.assertRaisesRegex(ValueError, "checksum"):
            verify_release(changed, self.revision)

    def test_corrupt_archive_is_rejected(self):
        with self.archive.open("ab") as archive:
            archive.write(b"corrupt transfer")
        with self.assertRaisesRegex(ValueError, "archive checksum"):
            verify_release(self.archive, self.revision)

    def test_wrong_revision_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "revision"):
            verify_release(self.archive, "b" * 40)

    def test_traversal_and_extra_files_are_rejected(self):
        for name in ("../outside", "/absolute", "app/.env"):
            with self.subTest(name=name):
                changed = self.repack(name, b"not allowed")
                with self.assertRaisesRegex(ValueError, "member"):
                    verify_release(changed, self.revision)

    def test_symbolic_links_and_duplicates_are_rejected(self):
        changed = self.repack("app/server.py", b"", kind=tarfile.SYMTYPE)
        with self.assertRaisesRegex(ValueError, "member"):
            verify_release(changed, self.revision)
        changed = self.repack("app/server.py", b"duplicate", duplicate=True)
        with self.assertRaisesRegex(ValueError, "member"):
            verify_release(changed, self.revision)

    def test_missing_application_file_prevents_build(self):
        (self.source / "app/server.py").rename(self.source / "app/server-missing.py")
        with self.assertRaises(FileNotFoundError):
            build_release(self.source, self.revision, self.root / "missing.tar.gz")

    def test_invalid_revision_prevents_build(self):
        with self.assertRaisesRegex(ValueError, "revision"):
            build_release(self.source, "main", self.root / "invalid.tar.gz")

    def prepare_host(self):
        host = self.root / "host"
        (host / "incoming").mkdir(parents=True)
        (host / "releases").mkdir()
        archive = host / "incoming" / f"lighting-dispatch-app-{self.revision}.tar.gz"
        build_release(self.source, self.revision, archive)
        root_patch = patch.object(release, "APP_ROOT", host)
        self.addCleanup(root_patch.stop)
        root_patch.start()
        return host

    def test_activate_and_repeat_verified_release(self):
        host = self.prepare_host()
        with patch.object(release, "systemctl") as systemctl, patch.object(release, "wait_healthy"):
            release.activate_release(self.revision)
            release.activate_release(self.revision)
        self.assertEqual((host / "current").resolve(), host / "releases" / self.revision)
        self.assertEqual((host / "current/RELEASE").read_text(), self.revision + "\n")
        self.assertEqual(systemctl.call_args_list, [unittest.mock.call("restart")] * 2)

    def test_failed_first_release_removes_only_its_link_and_stops_service(self):
        host = self.prepare_host()
        with patch.object(release, "systemctl") as systemctl, \
                patch.object(release, "wait_healthy", side_effect=RuntimeError("unhealthy")):
            with self.assertRaisesRegex(RuntimeError, "unhealthy"):
                release.activate_release(self.revision)
        self.assertFalse((host / "current").is_symlink())
        self.assertTrue((host / "releases" / self.revision).is_dir())
        self.assertEqual(systemctl.call_args_list, [unittest.mock.call("restart"), unittest.mock.call("stop")])

    def test_failed_release_restores_previous_link_and_rechecks_health(self):
        host = self.prepare_host()
        previous = host / "releases" / ("b" * 40)
        previous.mkdir()
        (host / "current").symlink_to(previous)
        with patch.object(release, "systemctl"), \
                patch.object(release, "wait_healthy", side_effect=[RuntimeError("unhealthy"), None]) as health:
            with self.assertRaisesRegex(RuntimeError, "unhealthy"):
                release.activate_release(self.revision)
        self.assertEqual((host / "current").resolve(), previous)
        self.assertEqual(health.call_args_list, [unittest.mock.call(self.revision), unittest.mock.call("b" * 40)])

    def test_unknown_ssh_commands_are_rejected_before_access(self):
        for command in ("bash", "deploy main", "deploy " + self.revision + "; id", "upload ../outside archive"):
            with self.subTest(command=command), patch.dict(os.environ, {"SSH_ORIGINAL_COMMAND": command}):
                with self.assertRaisesRegex(ValueError, "permitted"):
                    release.restricted_ssh()

    def test_oversized_upload_does_not_leave_partial_files(self):
        host = self.prepare_host()
        previous = set((host / "incoming").iterdir())
        with patch.dict(os.environ, {"SSH_ORIGINAL_COMMAND": f"upload {self.revision} archive"}), \
                patch("pwd.getpwuid", return_value=types.SimpleNamespace(pw_name="wuyou-deploy")), \
                patch.object(release, "MAX_ARCHIVE_BYTES", 3), \
                patch.object(release.sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(b"too large"))), \
                patch.object(release.signal, "signal"), patch.object(release.signal, "alarm"):
            with self.assertRaisesRegex(ValueError, "size limit"):
                release.restricted_ssh()
        self.assertEqual(set((host / "incoming").iterdir()), previous)

    def test_upload_cannot_bypass_an_active_deploy_lock(self):
        import fcntl

        host = self.prepare_host()
        with (host / "deploy.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.dict(os.environ, {"SSH_ORIGINAL_COMMAND": f"upload {self.revision} archive"}), \
                    patch("pwd.getpwuid", return_value=types.SimpleNamespace(pw_name="wuyou-deploy")), \
                    patch.object(release.signal, "signal"), patch.object(release.signal, "alarm"):
                with self.assertRaises(BlockingIOError):
                    release.restricted_ssh()
