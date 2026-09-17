import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request


APP_FILES = (
    "app/server.py",
    "app/business.py",
    "app/service.py",
    "app/index.html",
    "app/manifest.webmanifest",
    "app/app-icon.svg",
    "app/app-icon-180.png",
    "app/app-icon-192.png",
    "app/app-icon-512.png",
    "app/vendor/lucide.min.js",
)
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
APP_ROOT = Path("/opt/wuyou-dispatch")


def validate_revision(revision):
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("revision must be a full lowercase Git SHA")


def checksums(contents):
    return "".join(
        f"{hashlib.sha256(contents[name]).hexdigest()}  {name}\n"
        for name in (*APP_FILES, "RELEASE")
    ).encode("ascii")


def build_release(source, revision, archive):
    validate_revision(revision)
    contents = {}
    for name in APP_FILES:
        path = source / name
        if path.is_symlink() or not path.resolve().is_relative_to(source.resolve()):
            raise ValueError(f"source must be a regular project file: {name}")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError(f"source exceeds size limit: {name}")
        contents[name] = path.read_bytes()
    contents["RELEASE"] = (revision + "\n").encode("ascii")
    contents["SHA256SUMS"] = checksums(contents)
    with archive.open("xb") as output, tarfile.open(fileobj=output, mode="w:gz") as package:
        for name, content in contents.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mode = 0o644
            package.addfile(member, io.BytesIO(content))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    with Path(str(archive) + ".sha256").open("x", encoding="ascii") as output:
        output.write(f"{digest}  {archive.name}\n")
    verify_release(archive, revision)


def verify_release(archive, revision):
    validate_revision(revision)
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("archive exceeds size limit")
    expected = f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n"
    if Path(str(archive) + ".sha256").read_text(encoding="ascii") != expected:
        raise ValueError("archive checksum mismatch")
    contents = {}
    required = {*APP_FILES, "RELEASE", "SHA256SUMS"}
    with tarfile.open(archive, "r:gz") as package:
        for member in package:
            if (
                member.name not in required
                or member.name in contents
                or not member.isfile()
                or not 0 <= member.size <= MAX_FILE_BYTES
            ):
                raise ValueError(f"invalid archive member: {member.name}")
            contents[member.name] = package.extractfile(member).read()
    if contents.keys() != required:
        raise ValueError("missing archive member")
    if contents["RELEASE"] != (revision + "\n").encode("ascii"):
        raise ValueError("release revision mismatch")
    if contents["SHA256SUMS"] != checksums(contents):
        raise ValueError("application checksum mismatch")
    return contents


def ops_digest(unit):
    return hashlib.sha256(Path(__file__).read_bytes() + unit.read_bytes()).hexdigest()


def systemctl(action):
    subprocess.run(
        ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", action, "wuyou-dispatch.service"],
        check=True, timeout=90,
    )


def wait_healthy(revision):
    last_error = "service did not answer"
    for _ in range(20):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8848/api/health", timeout=3) as response:
                health = json.loads(response.read(8192))
            if health.get("ok") and health.get("revision") == revision:
                return
            last_error = "health or revision mismatch"
        except (OSError, ValueError) as error:
            last_error = str(error)
        time.sleep(1)
    raise RuntimeError(f"health check failed for {revision}: {last_error}")


def set_current(target):
    with tempfile.TemporaryDirectory(dir=APP_ROOT, prefix=".switch-") as directory:
        link = Path(directory) / "current"
        link.symlink_to(target)
        link.replace(APP_ROOT / "current")


def activate_release(revision):
    archive = APP_ROOT / "incoming" / f"lighting-dispatch-app-{revision}.tar.gz"
    contents = verify_release(archive, revision)
    target = APP_ROOT / "releases" / revision
    current = APP_ROOT / "current"
    previous = current.resolve(strict=True) if current.is_symlink() else None
    if current.exists() and previous is None:
        raise ValueError("current must be a release symlink")
    if previous is not None:
        validate_revision(previous.name)
        if previous.parent != (APP_ROOT / "releases").resolve():
            raise ValueError("previous release is outside the release directory")
    if target.exists():
        if target.is_symlink() or any((target / name).read_bytes() != content for name, content in contents.items()):
            raise ValueError("existing release differs from the verified artifact")
    else:
        with tempfile.TemporaryDirectory(dir=APP_ROOT / "releases", prefix=".stage-") as directory:
            staging = Path(directory)
            for name, content in contents.items():
                path = staging / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                path.chmod(0o644)
            for path in (staging, staging / "app", staging / "app/vendor"):
                path.chmod(0o755)
            staging.rename(target)
    try:
        set_current(target)
        systemctl("restart")
        wait_healthy(revision)
    except Exception as failure:
        try:
            if previous is None:
                current.unlink(missing_ok=True)
                systemctl("stop")
            else:
                set_current(previous)
                systemctl("restart")
                wait_healthy(previous.name)
        except Exception as rollback_failure:
            raise RuntimeError(f"deployment failed ({failure}); rollback failed ({rollback_failure})") from rollback_failure
        raise RuntimeError(f"deployment failed ({failure}); previous state restored") from failure


def restricted_ssh():
    import fcntl
    import pwd

    command = os.environ.get("SSH_ORIGINAL_COMMAND", "")
    match = re.fullmatch(r"protocol ([0-9a-f]{64})|upload ([0-9a-f]{40}) (archive|checksum)|deploy ([0-9a-f]{40})", command)
    if not match:
        raise ValueError("only protocol, upload and deploy commands are permitted")
    if pwd.getpwuid(os.getuid()).pw_name != "wuyou-deploy":
        raise ValueError("SSH publishing must run as wuyou-deploy")

    def interrupted(signum, _frame):
        raise RuntimeError(f"SSH publish interrupted by signal {signum}")

    for signum in (signal.SIGALRM, signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, interrupted)
    signal.alarm(600)
    try:
        with (APP_ROOT / "deploy.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            digest, revision, kind, deployment = match.groups()
            if digest:
                actual = ops_digest(Path("/etc/systemd/system/wuyou-dispatch.service"))
                if digest != actual:
                    raise ValueError("host operations differ; update the installed scripts first")
                print(f"wuyou-deploy-protocol=1 ops-digest={actual}")
            elif revision:
                filename = f"lighting-dispatch-app-{revision}.tar.gz" + (".sha256" if kind == "checksum" else "")
                limit = 1024 if kind == "checksum" else MAX_ARCHIVE_BYTES
                received = 0
                with tempfile.TemporaryDirectory(dir=APP_ROOT / "incoming", prefix=".upload-") as directory:
                    temporary = Path(directory) / "payload"
                    with temporary.open("xb") as output:
                        while chunk := sys.stdin.buffer.read(min(1024 * 1024, limit + 1 - received)):
                            received += len(chunk)
                            if received > limit:
                                raise ValueError("upload exceeds size limit")
                            output.write(chunk)
                    temporary.replace(APP_ROOT / "incoming" / filename)
                print(f"Uploaded {kind} for {revision}: {received} bytes")
            else:
                activate_release(deployment)
                print(f"Deployed {deployment}")
    finally:
        signal.alarm(0)


def main():
    parser = argparse.ArgumentParser(description="Build or verify an application-only release.")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("source", type=Path)
    build.add_argument("revision")
    build.add_argument("archive", type=Path)
    verify = commands.add_parser("verify")
    verify.add_argument("archive", type=Path)
    verify.add_argument("revision")
    digest = commands.add_parser("digest")
    digest.add_argument("unit", type=Path)
    commands.add_parser("ssh")
    args = parser.parse_args()
    try:
        if args.command == "build":
            build_release(args.source, args.revision, args.archive)
        elif args.command == "verify":
            verify_release(args.archive, args.revision)
        elif args.command == "digest":
            print(ops_digest(args.unit))
            return
        else:
            restricted_ssh()
            return
    except (OSError, ValueError, RuntimeError, tarfile.TarError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Release rejected: {error}\n")
    print(f"Release verified: {args.revision}")


if __name__ == "__main__":
    main()
