"""Prepare short-path clean Git repositories for explicitly approved real runs."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[1]


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True, encoding="utf-8").stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("--executors", nargs="+", choices=("dsh", "opencode"), required=True)
    args = parser.parse_args()
    if not 1 <= args.limit <= 3:
        parser.error("--limit 必须为 1–3")
    executors = list(dict.fromkeys(args.executors))[:args.limit]
    home = ROOT / "data" / ("rt-" + uuid.uuid4().hex[:6])
    home.mkdir(parents=True)
    store = home / "s"
    manifest = {"fixture_root": str(home), "store": str(store), "executors": {}}
    for executor in executors:
        short = "d" if executor == "dsh" else "o"
        repo = home / short
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        readme = repo / "README.md"
        readme.write_text(f"Disposable integration fixture for {executor}.\n", encoding="utf-8")
        git(repo, "add", "README.md")
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=Bridge Live Fixture",
                        "-c", "user.email=bridge-fixture@example.invalid", "commit", "-m", "fixture"],
                       check=True, capture_output=True)
        commit = git(repo, "rev-parse", "HEAD")
        status = git(repo, "status", "--porcelain")
        if status:
            raise RuntimeError(f"fixture repo is not clean: {executor}")
        manifest["executors"][executor] = {"repo": str(repo.resolve()), "base_commit": commit,
            "input_sha256": hashlib.sha256(readme.read_bytes()).hexdigest(),
            "task_id": f"real-{executor}-check"}
    (store).mkdir()
    manifest_path = home / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    raise SystemExit(main())
