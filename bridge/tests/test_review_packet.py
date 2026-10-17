"""review_packet 的离线隔离检查。支持 --limit N；默认首批 3 项。无网络/模型。"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "templates" / "review_packet.py"
PYTHON = Path(r"C:\Users\Lenovo\.workbuddy\binaries\python\versions\3.13.12\python.exe")
OUT_ROOT = Path(r"C:\Users\Lenovo\.codex\documents\r0-review-packet-20261017\isolated-fixtures")


def run(argv, cwd=None):
    proc = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=60)
    if proc.returncode:
        raise RuntimeError(f"exit={proc.returncode}: {proc.stderr.decode('utf-8','replace')}")
    return proc.stdout


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(obj):
    return (json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def snapshot(root):
    names = run(["git", "-C", str(root), "ls-files", "-c", "-o", "--exclude-standard", "-z"]).split(b"\0")
    return {n.decode(): sha((root / n.decode()).read_bytes()) for n in names if n and (root / n.decode()).is_file()}


def setup(case_dir, number):
    repo, old_wt, wt = case_dir / "repo", case_dir / "wt-old", case_dir / "relocated-wt"
    store = case_dir / "store"
    repo.mkdir(parents=True)
    run(["git", "init", "-q", str(repo)])
    run(["git", "-C", str(repo), "config", "user.name", "Fixture"])
    run(["git", "-C", str(repo), "config", "user.email", "fixture@example.invalid"])
    run(["git", "-C", str(repo), "config", "core.autocrlf", "false"])
    eol = b"crlf" if number == 8 else b"lf"
    (repo / ".gitattributes").write_bytes(b"*.txt text eol=" + eol + b"\n")
    (repo / "readme.txt").write_bytes(b"baseline\n")
    (repo / "gone.txt").write_bytes(b"delete me\n")
    (repo / "rename.txt").write_bytes(b"rename me\n")
    run(["git", "-C", str(repo), "add", ".gitattributes", "readme.txt", "gone.txt", "rename.txt"])
    run(["git", "-C", str(repo), "commit", "-qm", "baseline"])
    base = run(["git", "-C", str(repo), "rev-parse", "HEAD"]).decode().strip()
    run(["git", "-C", str(repo), "worktree", "add", "--detach", str(old_wt), base])
    run(["git", "-C", str(repo), "worktree", "move", str(old_wt), str(wt)])
    task = f"fixture-review-{number:02d}"
    aid = "attempt-" + f"{number:032x}"
    task_dir = store / "tasks" / task
    att_dir = task_dir / "attempts" / aid
    att_dir.mkdir(parents=True)
    contract = {"task_id": task, "title": "isolated fixture", "goal": "verify input", "repo": str(repo),
                "executor": "opencode", "opencode_mode": "shared", "allowed_paths": ["readme.txt", "review-note.txt"],
                "artifacts": ["review-note.txt"], "completion_criteria": ["packet matches inputs"],
                "timeout_seconds": 30}
    contract2 = {**contract, "title": "later revision must not replace old one"}
    (task_dir / "contract.r1.json").write_bytes(canonical(contract))
    (task_dir / "contract.r2.json").write_bytes(canonical(contract2))
    contract_hash = sha(canonical(contract))
    before = snapshot(wt)
    if number == 1:
        (wt / "readme.txt").write_bytes(b"unstaged edit\n")
    elif number == 2:
        (wt / "readme.txt").write_bytes(b"staged edit\n")
        run(["git", "-C", str(wt), "add", "readme.txt"])
    elif number == 3:
        (wt / "review-note.txt").write_bytes(b"untracked review evidence\n")
    elif number == 4:
        run(["git", "-C", str(wt), "update-index", "--chmod=+x", "rename.txt"])
        run(["git", "-C", str(wt), "mv", "rename.txt", "renamed.txt"])
        (wt / "gone.txt").unlink()
        (wt / "payload.bin").write_bytes(b"\x00\x01binary\xff")
    elif number == 8:
        (wt / "readme.txt").write_bytes(b"CRLF edit\r\n")
    elif number == 11:
        (wt / "large.txt").write_bytes(b"x" * (70 * 1024))
    after = snapshot(wt)
    changes = [{"path": p, "before_sha256": before.get(p), "after_sha256": after.get(p)}
               for p in sorted(before.keys() | after.keys()) if before.get(p) != after.get(p)]
    marker = (wt / ".git").read_bytes()
    attempt = {"attempt_id": aid, "task_id": task, "revision": 1, "contract_sha256": contract_hash,
               "base_commit": base, "worktree": str(wt)}
    result = {"attempt_id": aid, "task_id": task, "revision": 1, "contract_sha256": contract_hash,
              "base_commit": base, "git_marker_sha256": sha(marker), "after_snapshot_sha256": sha(canonical(after)),
              "changes": changes, "exit_code": 0, "stop_reason": None, "native_stop_confirmed": True}
    (att_dir / "attempt.json").write_bytes(canonical(attempt))
    result_bytes = canonical(result)
    (att_dir / "result.json").write_bytes(result_bytes)
    (att_dir / "before.json").write_bytes(canonical(before))
    (att_dir / "after.json").write_bytes(canonical(after))
    (task_dir / "state.json").write_bytes(canonical({"task_id": task, "current_attempt": aid, "revision": 2}))
    events = [{"action": "starting", "attempt_id": aid, "plan_sha256": "a" * 64},
              {"action": "collected", "attempt_id": aid, "result_sha256": sha(result_bytes)}]
    (task_dir / "events.jsonl").write_bytes(b"".join(
        (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode() for event in events))
    if number == 5:
        attempt["worktree"] = str(case_dir / "missing-worktree")
        (att_dir / "attempt.json").write_bytes(canonical(attempt))
        decoy = att_dir / "workspace"
        decoy.mkdir()
        (decoy / ".git").write_text("gitdir: bogus\n", encoding="utf-8")
    elif number == 6:
        (task_dir / "contract.r1.json").write_bytes(canonical({**contract, "title": "tampered historical contract"}))
    elif number == 7:
        (att_dir / "attempt.json").write_text("{invalid json", encoding="utf-8")
    elif number == 9:
        before["readme.txt"] = sha(b"content was actually changed\n")
        result["changes"] = [{"path": p, "before_sha256": before.get(p), "after_sha256": after.get(p)}
                              for p in sorted(before.keys() | after.keys()) if before.get(p) != after.get(p)]
        (att_dir / "before.json").write_bytes(canonical(before))
        result_bytes = canonical(result)
        (att_dir / "result.json").write_bytes(result_bytes)
        events[1]["result_sha256"] = sha(result_bytes)
        (task_dir / "events.jsonl").write_bytes(b"".join(
            (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode() for event in events))
    elif number == 10:
        config = repo / ".git" / "config"
        text = config.read_text(encoding="utf-8")
        config.write_text(text.replace("repositoryformatversion = 0", "repositoryformatversion = invalid"), encoding="utf-8")
    packet_dir = case_dir / "packets"
    packet_dir.mkdir()
    packet = packet_dir / "packet.md"
    tool_limit = "8" if number == 4 else "3"
    command = [str(PYTHON), "-B", str(TOOL), "--store", str(store), "--task", task,
               "--attempt", aid, "--limit", tool_limit, "--out", str(packet)]
    proc = subprocess.run(command, capture_output=True, timeout=90)
    stdout, stderr = proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")
    evidence = {"case": number, "command": command, "exit_code": proc.returncode,
                "stdout": stdout, "stderr": stderr, "packet_exists": packet.is_file()}
    if packet.is_file():
        data = packet.read_bytes()
        text = data.decode("utf-8")
        evidence.update(packet_sha256=sha(data),
                       contains_revision_1=str(task_dir / "contract.r1.json") in text,
                       contains_later_contract_marker="later revision must not replace old one" in text)
        if number <= 3:
            marker_text = {1: "unstaged edit", 2: "staged edit", 3: "untracked review evidence"}[number]
            evidence["contains_expected_content"] = marker_text in text
    if number <= 3:
        expected = (proc.returncode == 0 and packet.is_file() and evidence.get("contains_revision_1")
                    and not evidence.get("contains_later_contract_marker") and evidence.get("contains_expected_content"))
    elif number == 4:
        packet_data = packet.read_bytes() if packet.is_file() else b""
        attached = list(packet.with_name(packet.name + ".attachments").glob("*.bin"))
        expected = (proc.returncode == 0 and packet.is_file() and b"rename.txt" in packet_data
                    and b"renamed.txt" in packet_data and b"deleted file mode" in packet_data
                    and b"100755" in packet_data and len(attached) == 1
                    and sha(attached[0].read_bytes()) in packet_data.decode("utf-8"))
    elif number == 5:
        expected = proc.returncode != 0 and not packet.exists() and "worktree" in stdout
    elif number == 6:
        expected = proc.returncode != 0 and not packet.exists() and "契约版本／哈希不一致" in stdout
    elif number == 7:
        expected = proc.returncode != 0 and not packet.exists() and "JSONDecodeError" in stdout
    elif number == 8:
        expected = proc.returncode == 0 and packet.is_file() and b"proven_lf_crlf_checkout" in packet.read_bytes()
    elif number == 9:
        expected = proc.returncode != 0 and not packet.exists() and "纯 LF/CRLF 转换证据" in stdout
    elif number == 10:
        expected = proc.returncode != 0 and not packet.exists() and "git rev-parse" in stdout
    elif number == 11:
        attachments = packet.with_name(packet.name + ".attachments")
        attached = list(attachments.glob("*.bin")) if attachments.is_dir() else []
        expected = (proc.returncode == 0 and packet.is_file() and len(attached) == 1
                    and sha(attached[0].read_bytes()) in packet.read_text(encoding="utf-8"))
    elif number == 12:
        second_packet = packet.with_name("packet-repeat.md")
        second = subprocess.run([*command[:-1], str(second_packet)], capture_output=True, timeout=90)
        evidence["repeat_exit_code"] = second.returncode
        evidence["repeat_packet_sha256"] = sha(second_packet.read_bytes()) if second_packet.is_file() else None
        evidence["repeat_matches"] = second_packet.is_file() and second_packet.read_bytes() == packet.read_bytes()
        overwrite = subprocess.run(command, capture_output=True, timeout=90)
        evidence["overwrite_exit_code"] = overwrite.returncode
        expected = proc.returncode == 0 and packet.is_file() and evidence["repeat_matches"] and overwrite.returncode != 0
    evidence["assertions_pass"] = bool(expected)
    if not expected:
        raise AssertionError(json.dumps(evidence, ensure_ascii=False))
    return evidence


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--start", type=int, default=1)
    args = ap.parse_args()
    if args.limit < 1 or args.start < 1 or args.start > 12 or args.start + args.limit - 1 > 12:
        ap.error("--limit 必须为正数且测试范围不得超出 12 项")
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    run_dir = OUT_ROOT / uuid.uuid4().hex
    run_dir.mkdir()
    results = []
    for number in range(args.start, args.start + args.limit):
        case_dir = run_dir / f"case-{number:02d}"
        case_dir.mkdir()
        try:
            results.append(setup(case_dir, number))
        except Exception as exc:
            report_path = run_dir / "results.json"
            report = {"status": "FAIL", "limit": args.limit, "start": args.start,
                      "completed": results, "failed_case": number,
                      "error": f"{type(exc).__name__}: {exc}"}
            report_path.write_bytes((json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode())
            print(json.dumps({"status": "FAIL", "report": str(report_path),
                              "failed_case": number, "error": report["error"]}, ensure_ascii=False, indent=2))
            return 1
    report = {"limit": args.limit, "start": args.start, "results": results}
    report_path = run_dir / "results.json"
    report_path.write_bytes((json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode())
    print(json.dumps({"status": "PASS", "report": str(report_path),
                      "results": [{k: v for k, v in item.items() if k not in ("stdout", "stderr", "command")}
                                  for item in results]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.exit(main())
