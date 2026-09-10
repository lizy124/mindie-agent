#!/usr/bin/env python3
"""Run API compatibility checks inside a user-selected NPU container.

The host connection uses the local SSH key and Docker's ``exec`` interface.
Tokens are read from the local environment and sent over stdin to the remote
shell; they are never placed in command-line arguments or saved in target
metadata.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[4]
STATE_DIR = ROOT / ".vaws-local" / "vllm-api-compat"
TARGETS_FILE = STATE_DIR / "remote-targets.json"
PACKAGE_ROOT = ROOT / ".agents" / "skills" / "vllm-api-compat"
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _validate_name(value: str, label: str) -> str:
    if not value or not SAFE_NAME.fullmatch(value):
        raise ValueError(f"{label} must contain only letters, digits, '.', '_' or '-'")
    return value


def _ssh_executable() -> str:
    if os.name == "nt":
        system_ssh = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "OpenSSH" / "ssh.exe"
        if system_ssh.exists():
            return str(system_ssh)
    found = shutil.which("ssh")
    if not found:
        raise RuntimeError("OpenSSH client not found")
    return found


def _load_targets() -> dict[str, Any]:
    if not TARGETS_FILE.exists():
        return {"targets": {}}
    return json.loads(TARGETS_FILE.read_text(encoding="utf-8"))


def _save_targets(data: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temp = TARGETS_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(TARGETS_FILE)


def _identity_file(explicit: str | None) -> str:
    if explicit:
        path = Path(explicit).expanduser()
    else:
        path = Path.home() / ".ssh" / "id_ed25519"
    if not path.exists():
        raise RuntimeError(f"SSH identity file not found: {path}")
    return str(path)


def _record_from_args(args: argparse.Namespace) -> dict[str, Any]:
    host = _validate_name(args.host, "host")
    container = _validate_name(args.container, "container")
    user = _validate_name(args.user, "user")
    return {
        "name": _validate_name(args.name, "target name"),
        "host": host,
        "port": args.port,
        "user": user,
        "container": container,
        "identity_file": _identity_file(args.identity_file),
        "created_at": _now(),
    }


def _ssh_base(record: dict[str, Any]) -> list[str]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    known_hosts = STATE_DIR / "known_hosts"
    return [
        _ssh_executable(),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-i",
        record["identity_file"],
        "-p",
        str(record["port"]),
        f"{record['user']}@{record['host']}",
    ]


def _run_ssh(
    record: dict[str, Any],
    command: str,
    *,
    input_bytes: bytes | None = None,
    capture_output: bool = True,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        _ssh_base(record) + [command],
        input=input_bytes,
        capture_output=capture_output,
        timeout=timeout,
        check=False,
    )


def _docker_exec(record: dict[str, Any], command: str, *, login: bool = True) -> str:
    shell = "bash -lc" if login else "bash -c"
    return f"docker exec {shlex.quote(record['container'])} {shell} {shlex.quote(command)}"


def _probe(record: dict[str, Any]) -> dict[str, Any]:
    inspect = _run_ssh(
        record,
        "docker inspect --format '{{.Name}}|{{.Config.Image}}|{{.State.Status}}' "
        + shlex.quote(record["container"]),
    )
    if inspect.returncode != 0:
        return {
            "status": "unreachable",
            "success": False,
            "error": inspect.stderr.decode(errors="replace")[-1000:],
        }
    line = inspect.stdout.decode(errors="replace").strip().splitlines()[-1]
    parts = line.split("|", 2)
    if len(parts) != 3:
        return {"status": "invalid", "success": False, "raw": line}
    torch_probe = _run_ssh(
        record,
        _docker_exec(
            record,
            "python3 -c 'import torch, torch_npu; print(torch.npu.device_count())'",
        ),
        timeout=60,
    )
    return {
        "status": "ready" if parts[2] == "running" and torch_probe.returncode == 0 else "not_ready",
        "success": parts[2] == "running" and torch_probe.returncode == 0,
        "container": parts[0].lstrip("/"),
        "image": parts[1],
        "state": parts[2],
        "torch_npu_probe": torch_probe.stdout.decode(errors="replace").strip(),
        "torch_npu_error": torch_probe.stderr.decode(errors="replace")[-500:]
        if torch_probe.returncode
        else None,
    }


def register(args: argparse.Namespace) -> int:
    record = _record_from_args(args)
    probe = _probe(record)
    record["probe"] = probe
    data = _load_targets()
    data.setdefault("targets", {})[record["name"]] = record
    _save_targets(data)
    print(json.dumps({"status": "ok" if probe["success"] else "not_ready", "target": record}, indent=2))
    return 0 if probe["success"] else 1


def _archive_package() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in (PACKAGE_ROOT / "scripts").glob("*.py"):
            archive.add(path, arcname=str(Path(".agents/skills/vllm-api-compat/scripts") / path.name))
        for path in (PACKAGE_ROOT / "references").glob("*.yaml"):
            archive.add(path, arcname=str(Path(".agents/skills/vllm-api-compat/references") / path.name))
    return buffer.getvalue()


def _stream_remote(record: dict[str, Any], command: str, input_bytes: bytes) -> tuple[int, bytes, bytes]:
    proc = subprocess.Popen(
        _ssh_base(record) + [command],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    stdout = bytearray()
    stderr = bytearray()

    def drain(stream, target, forward: bool) -> None:
        for chunk in iter(lambda: stream.readline(), b""):
            target.extend(chunk)
            if forward:
                sys.stderr.buffer.write(chunk)
                sys.stderr.buffer.flush()

    out_thread = threading.Thread(target=drain, args=(proc.stdout, stdout, False), daemon=True)
    err_thread = threading.Thread(target=drain, args=(proc.stderr, stderr, True), daemon=True)
    out_thread.start()
    err_thread.start()
    proc.stdin.write(input_bytes)
    proc.stdin.close()
    returncode = proc.wait()
    out_thread.join()
    err_thread.join()
    return returncode, bytes(stdout), bytes(stderr)


def _safe_extract(blob: bytes, destination: Path) -> None:
    if not blob:
        return
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        root = destination.resolve()
        for member in archive.getmembers():
            target = (destination / member.name).resolve()
            if root != target and root not in target.parents:
                raise RuntimeError(f"unsafe artifact path: {member.name}")
        archive.extractall(destination)


def _last_json(stdout: bytes) -> dict[str, Any] | None:
    text = stdout.decode(errors="replace")
    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            candidates.append(value)
    if not candidates:
        return None
    # Prefer the outer summary over any nested result objects.
    return max(candidates, key=lambda value: len(value))


def run(args: argparse.Namespace) -> int:
    data = _load_targets()
    if args.target not in data.get("targets", {}):
        raise RuntimeError(f"unknown target {args.target!r}; run register first")
    record = data["targets"][args.target]
    probe = _probe(record)
    if not probe.get("success"):
        print(json.dumps({"status": "failed", "phase": "probe", "probe": probe}, indent=2))
        return 1

    stage = f"/tmp/vaws-api-compat-{uuid.uuid4().hex}"
    package = _archive_package()
    # Keep the archive stream on Docker's stdin directly. Wrapping this part
    # in ``bash -lc`` can consume or truncate stdin on some Docker/bash builds.
    mkdir_result = _run_ssh(record, _docker_exec(record, f"mkdir -p {stage}"), timeout=30)
    if mkdir_result.returncode != 0:
        raise RuntimeError(mkdir_result.stderr.decode(errors="replace")[-2000:])
    stage_cmd = (
        f"docker exec -i {shlex.quote(record['container'])} "
        f"tar -xzf - -C {shlex.quote(stage)}"
    )
    stage_rc, stage_out, stage_err = _stream_remote(record, stage_cmd, package)
    if stage_rc != 0:
        raise RuntimeError(stage_err.decode(errors="replace")[-2000:])

    python = "/usr/local/python3.11.10/bin/python3"
    daily = args.entrypoint == "daily"
    if daily:
        script = ".agents/skills/vllm-api-compat/scripts/daily_check.py"
        command_args = [
            "--model",
            args.model,
            "--timeout",
            str(args.timeout),
            "--log-path",
            f"{stage}/server-api-log",
            "--github-token-stdin",
        ]
        if args.dry_run:
            command_args.append("--dry-run")
        if args.auto_pr:
            command_args.append("--auto-pr")
        if args.skip_pr_detection:
            command_args.append("--skip-pr-detection")
        if args.parallel > 1:
            command_args.extend(["--parallel", str(args.parallel)])
            if args.parallel_devices:
                command_args.extend(["--parallel-devices", args.parallel_devices])
    else:
        script = ".agents/skills/vllm-api-compat/scripts/cli.py"
        command_args = [
            "--model",
            args.model,
            "--nodes",
            args.nodes,
            "--timeout",
            str(args.timeout),
            "--log-path",
            f"{stage}/server-api-log",
        ]
        if args.section:
            command_args.extend(["--section", args.section])
        if args.param:
            command_args.extend(["--param", args.param])
        if args.test_value is not None:
            command_args.extend(["--test-value", args.test_value])
        if args.parallel > 1:
            command_args.extend(["--parallel", str(args.parallel)])
            if args.parallel_devices:
                command_args.extend(["--parallel-devices", args.parallel_devices])
    command = (
        f"docker exec -i -e ASCEND_RT_VISIBLE_DEVICES={shlex.quote(args.devices)} "
        f"{shlex.quote(record['container'])} {shlex.quote(python)} "
        f"{shlex.quote(f'{stage}/{script}')} "
        + " ".join(shlex.quote(value) for value in command_args)
    )
    token = os.environ.get("GITHUB_TOKEN", "") if daily else ""
    run_rc, run_out, run_err = _stream_remote(
        record, command, (token + "\n").encode()
    )

    artifact_dir = STATE_DIR / "remote-runs" / args.target / datetime.now().strftime("%Y%m%dT%H%M%S")
    artifact_cmd = _docker_exec(
        record,
        f"tar -czf - -C {shlex.quote(stage)} .vaws-local server-api-log 2>/dev/null || true",
    )
    # Fetch separately so a verbose test stdout never gets mixed with the tar stream.
    fetched = _run_ssh(record, artifact_cmd, timeout=120)
    if fetched.returncode == 0:
        _safe_extract(fetched.stdout, artifact_dir)
    result = _last_json(run_out) or {
        "status": "failed",
        "phase": "remote_run",
        "error": run_out.decode(errors="replace")[-2000:],
    }
    result.update({"target": args.target, "container": record["container"], "artifact_dir": str(artifact_dir)})
    _run_ssh(record, _docker_exec(record, f"rm -rf -- {shlex.quote(stage)}"), timeout=30)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if run_rc == 0 and result.get("status") in {"ok", "interrupted"} else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    reg = sub.add_parser("register")
    reg.add_argument("--name", required=True)
    reg.add_argument("--host", required=True)
    reg.add_argument("--container", required=True)
    reg.add_argument("--user", default="root")
    reg.add_argument("--port", type=int, default=22)
    reg.add_argument("--identity-file")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--target", required=True)
    run_parser.add_argument("--entrypoint", choices=("daily", "cli"), default="daily")
    run_parser.add_argument("--model", required=True)
    run_parser.add_argument("--devices", default="0,1,2,3")
    run_parser.add_argument("--timeout", type=int, default=400)
    run_parser.add_argument("--dry-run", action="store_true")
    run_parser.add_argument("--auto-pr", action="store_true")
    run_parser.add_argument("--skip-pr-detection", action="store_true")
    run_parser.add_argument("--parallel", type=int, default=1)
    run_parser.add_argument("--parallel-devices")
    run_parser.add_argument("--nodes", choices=("all", "single"), default="all")
    run_parser.add_argument("--section")
    run_parser.add_argument("--param")
    run_parser.add_argument("--test-value")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return register(args) if args.command == "register" else run(args)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
