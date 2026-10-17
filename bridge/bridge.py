"""本地协同桥：单任务、文件契约、原生执行器、证据回收。"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import queue
import re
import subprocess
import sys
import threading
import time
import uuid

sys.dont_write_bytecode = True
import adapters

ROOT = Path(__file__).resolve().parent
ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
SECRET = re.compile(r"\b(?:oc_sk_|sk-|st_|rt_)[A-Za-z0-9_.-]{12,}|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
# 锚点：仓库相对路径:行或起-止（逗号分隔），如 web/task-graph.js:147,184,251-253。
ANCHOR = re.compile(r"(?P<path>[^:]+):(?P<lines>\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*)")
# 本机实测（git 2.55.0.windows.2，LongPathsEnabled=1）：worktree 绝对路径 ≥216 字符时
# git worktree add 必然报 "fatal: '$GIT_DIR' too big"（退出码 128），且 -c core.longpaths=true
# 无效；仓库自身路径长短不影响该阈值。留出余量，超安全阈值就改放短根，超硬上限直接给桥自己的错误。
WORKTREE_PATH_SAFE = 200
WORKTREE_PATH_LIMIT = 215
# 收尾确认等待上限（秒）：本机实测 session.get 单次往返约 0.08 秒，已停止会话立即返回
# outcome；此值只用于容忍收尾落盘延迟，不是墙钟超时。超时后最多多等这么久来拿"已停稳"结论。
STOP_CONFIRM_WAIT_SECONDS = 30
# 执行期健康探针节拍（秒）。实测 session.get 单次往返约 0.08 秒，10 秒一次可忽略开销。
HEALTH_PROBE_INTERVAL = 10.0
# 父会话若导出这些变量，git 会指向错误仓库或同样报错；调用 git 前统一剥离。
GIT_ENV_STRIP = frozenset(("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                           "GIT_COMMON_DIR", "GIT_ALTERNATE_OBJECT_DIRECTORIES"))


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def atomic(path, value):
    path = Path(path)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with tmp.open("xb") as f:
        f.write(json_bytes(value))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def scrub(text):
    text = SECRET.sub("[REDACTED]", text)
    return re.sub(r'(?i)((?:api[_-]?key|password|authorization|access[_-]?token|refresh[_-]?token)\s*["\x27\\]*\s*[:=]\s*["\x27\\]*)([^\s,"\x27\\}]+)',
                  r'\1[REDACTED]', text)


def git(repo, *args):
    env = {key: value for key, value in os.environ.items() if key.upper() not in GIT_ENV_STRIP}
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=60, env=env)
    if p.returncode:
        raise ValueError("git " + args[0] + " 失败：" + scrub(p.stderr.decode("utf-8", "replace")))
    return p.stdout


def default_worktree_root():
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "agent-bridge" / "wt"


def worktree_path(attempt_dir, attempt_id, config):
    """返回 (工作区路径, 是否已改放短根)。过长路径会让 git 直接失败，必须先判定。"""
    natural = attempt_dir / "workspace"
    if len(str(natural)) < WORKTREE_PATH_SAFE:
        return natural, False
    root = config.get("worktree_fallback_root") or default_worktree_root()
    fallback = Path(root) / attempt_id.replace("attempt-", "a")
    if len(str(fallback)) >= WORKTREE_PATH_LIMIT:
        raise ValueError("工作区路径过长且短根仍超限（%d ≥ %d）；先缩短桥的 data 目录：%s"
                         % (len(str(fallback)), WORKTREE_PATH_LIMIT, fallback))
    return fallback, True


def recorded_worktree(attempt_dir):
    """读取 attempt.json 记录的真实工作区；早期现场没有该字段时回退到默认位置。"""
    path = Path(attempt_dir) / "attempt.json"
    if path.is_file():
        value = read(path).get("worktree")
        if isinstance(value, str) and value:
            return Path(value)
    return Path(attempt_dir) / "workspace"


def inside(base, relative):
    p = PurePosixPath(relative)
    if (not relative or not p.parts or p.is_absolute() or ".." in p.parts or "\\" in relative
            or ":" in relative or any(part.lower() == ".git" for part in p.parts)
            or relative.rstrip("/") != p.as_posix()
            or any(part.endswith((".", " ")) or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part) for part in p.parts)):
        raise ValueError("非法仓库相对路径：" + relative)
    candidate = base
    for part in p.parts:
        candidate = candidate / part
        if candidate.is_symlink() or candidate.is_junction():
            raise ValueError("不允许符号链接或目录联接：" + relative)
    resolved = candidate.resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise ValueError("路径越界：" + relative)
    return resolved


def validate(task):
    if not isinstance(task, dict) or not ID.fullmatch(task.get("task_id", "")):
        raise ValueError("task_id 应为小写字母／数字／连字符，最多 64 字符")
    for key in ("goal", "repo", "executor"):
        if not isinstance(task.get(key), str) or not task[key].strip():
            raise ValueError("缺少字段：" + key)
    repo = Path(task["repo"])
    if not repo.is_absolute() or not repo.is_dir():
        raise ValueError("repo 必须是已有仓库的绝对目录")
    if task["executor"] not in ("dsh", "opencode"):
        raise ValueError("executor 只支持 dsh / opencode")
    if "title" in task and (not isinstance(task["title"], str) or not 1 <= len(task["title"].strip()) <= 100):
        raise ValueError("title 必须为 1–100 字符的短标题")
    if task.get("opencode_mode", "service") not in ("service", "standalone", "shared"):
        raise ValueError("opencode_mode 只支持 service / shared / standalone")
    if task["executor"] != "opencode" and "opencode_mode" in task:
        raise ValueError("opencode_mode 仅用于 OpenCode")
    for key in ("allowed_paths", "artifacts", "completion_criteria"):
        if not isinstance(task.get(key), list) or not task[key] or not all(isinstance(x, str) and x.strip() for x in task[key]):
            raise ValueError(key + " 必须是非空字符串数组")
    refs = task.get("input_refs", [])
    if not isinstance(refs, list) or any(not isinstance(x, str) for x in refs):
        raise ValueError("input_refs 必须是字符串数组")
    for item in task["allowed_paths"] + task["artifacts"] + refs:
        inside(repo, item)
    if any(not allowed(x, task["allowed_paths"]) for x in task["artifacts"]):
        raise ValueError("产物必须落在允许修改的路径内")
    # anchors 可选：旧契约没有该字段时继续兼容。
    anchors = task.get("anchors", [])
    if not isinstance(anchors, list) or any(not isinstance(x, str) or not x.strip() for x in anchors):
        raise ValueError("anchors 必须是字符串数组")
    for anchor in anchors:
        path, spans = parse_anchor(anchor)
        if path not in refs:
            raise ValueError("锚点文件必须列入 input_refs 以固定 hash：" + path)
        target = inside(repo, path)
        if not target.is_file():
            raise ValueError("锚点文件不存在：" + path)
        total = len(target.read_text(encoding="utf-8").splitlines())
        for start, end in spans:
            if end > total:
                raise ValueError("锚点行号越界：%s 第 %d 行超出文件 %d 行" % (path, end, total))
    timeout = task.get("timeout_seconds", 300)
    if type(timeout) is not int or not 10 <= timeout <= 3600:
        raise ValueError("timeout_seconds 应为 10–3600 秒整数")
    deps = task.get("dependencies", [])
    if not isinstance(deps, list) or any(not isinstance(x, str) or not ID.fullmatch(x) or x == task["task_id"] for x in deps):
        raise ValueError("依赖任务编号非法")
    task["repo"] = str(repo.resolve())
    task["timeout_seconds"] = timeout
    task["dependencies"] = deps
    return task


def allowed(path, paths):
    return any(path == p or (p.endswith("/") and path.startswith(p)) for p in paths)


def parse_anchor(anchor):
    """解析 anchors 项：仓库相对路径:行或区间（逗号分隔）。拒绝格式、反向区间与非正整数。"""
    match = ANCHOR.fullmatch(anchor)
    if not match:
        raise ValueError("锚点格式应为 相对路径:行[,行|起-止]：" + anchor)
    spans = []
    for chunk in match.group("lines").split(","):
        if "-" in chunk:
            left, right = chunk.split("-", 1)
            start, end = int(left), int(right)
            if start > end:
                raise ValueError("锚点区间反向（起 > 止）：" + anchor)
        else:
            start = end = int(chunk)
        if start < 1:
            raise ValueError("锚点行号必须为正整数：" + anchor)
        spans.append((start, end))
    return match.group("path"), spans


@contextmanager
def writer(store):
    store.mkdir(parents=True, exist_ok=True)
    with (store / ".write.lock").open("a+b") as f:
        if f.seek(0, 2) == 0:
            f.write(b"0")
            f.flush()
        f.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("已有桥写入／执行任务；只读 status 或请求 cancel，不能并行修改")
        try:
            yield
        finally:
            f.seek(0)
            if os.name == "nt":
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def task_dir(store, task_id):
    if not isinstance(task_id, str) or not ID.fullmatch(task_id):
        raise ValueError("非法任务编号")
    path = store / "tasks" / task_id
    if path.is_symlink() or not path.resolve().is_relative_to(store.resolve()):
        raise ValueError("任务目录越界")
    return path


def event(directory, action, **data):
    with (directory / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"time": now(), "action": action, **data}, ensure_ascii=False) + "\n")


def load_task(directory):
    state = read(directory / "state.json")
    task = read(directory / ("contract.r" + str(state["revision"]) + ".json"))
    if digest(json_bytes(task)) != state["contract_sha256"]:
        raise ValueError("契约哈希不匹配；拒绝使用外部改写的任务")
    return task, state


def submit(store, source):
    task = validate(read(source))
    directory = task_dir(store, task["task_id"])
    with writer(store):
        directory.mkdir(parents=True, exist_ok=False)
        atomic(directory / "contract.r1.json", task)
        state = {"task_id": task["task_id"], "revision": 1, "contract_sha256": digest(json_bytes(task)),
                 "status": "ready", "acceptance": "BLOCKED", "current_attempt": None, "created_at": now()}
        atomic(directory / "state.json", state)
        event(directory, "submitted", contract_sha256=state["contract_sha256"])
    return state


def plan(store, task_id, config):
    directory = task_dir(store, task_id)
    task, state = load_task(directory)
    repo = Path(task["repo"])
    top = Path(git(repo, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if top != repo.resolve():
        raise ValueError("repo 必须指向 Git 仓库根目录")
    if git(repo, "status", "--porcelain"):
        raise ValueError("目标仓库有未提交改动；先由用户处理，桥不覆盖或搬走改动")
    commit = git(repo, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
    if git(repo, "rev-parse", "--verify", commit + "^{commit}").decode().strip() != commit:
        raise ValueError("基线 commit 回核不一致")
    for dep in task["dependencies"]:
        if read(task_dir(store, dep) / "state.json")["status"] != "accepted":
            raise ValueError("依赖尚未接收：" + dep)
    inputs = {}
    tracked = set(git(repo, "ls-files", "-z").decode("utf-8", "strict").split("\0"))
    for ref in task.get("input_refs", []):
        p = inside(repo, ref)
        if ref not in tracked or not p.is_file() or p.is_symlink():
            raise ValueError("输入必须是已跟踪的仓库内普通文件：" + ref)
        inputs[ref] = digest(p.read_bytes())
    # 先构造命令确认接口参数，不在这里启动。
    command, _ = adapters.build(task, "读取任务契约并按边界实现。", config)
    entry_paths = command[:2] if task["executor"] == "dsh" else command[:1]
    entry_hashes = {path: digest(Path(path).read_bytes()) for path in entry_paths}
    result = {"task_id": task_id, "revision": state["revision"], "contract_sha256": state["contract_sha256"],
              "base_commit": commit, "repo": str(repo), "executor": task["executor"], "model": task.get("model"),
              "inputs": inputs, "allowed_paths": task["allowed_paths"], "timeout_seconds": task["timeout_seconds"],
              "executable_sha256": entry_hashes,
              "opencode_mode": task.get("opencode_mode", "service") if task["executor"] == "opencode" else None,
              "title": task.get("title", task_id),
              "effects": ["新增一个 detached Git worktree",
                          "先建共享服务空会话，确认客户端可见后再运行" if adapters.shared(task) else "启动一个新的执行器及其私有子进程",
                          "执行器可能调用模型并消耗额度",
                          "共享会话用原生 interrupt；停稳未确认时保持 BLOCKED" if adapters.shared(task) else "超时／取消只结束桥登记的自身进程树"],
              "limitations": ["尚无真实任务验收", "原生模型请求节流／重试／步数上限未实证，不能当硬预算",
                              "worktree 不等于强安全沙箱；路径检查发现越界后拒收",
                              "Git 忽略的文件不纳入快照；外部路径写入不能由事后 diff 证明不存在",
                              "Windows Job 登记发生在进程创建后，存在早期子进程竞态；取消需实际验收"]}
    result["plan_sha256"] = digest(json_bytes(result))
    return result


def snapshot(repo):
    names = git(repo, "ls-files", "-c", "-o", "--exclude-standard", "-z").split(b"\0")
    if len(names) > 10001:
        raise ValueError("仓库文件超过 10000；先缩小任务，不全量扫描")
    result = {}
    for raw in names:
        if not raw:
            continue
        name = raw.decode("utf-8", "strict")
        p = inside(repo, name)
        if p.is_symlink():
            raise ValueError("任务中出现符号链接：" + name)
        if p.is_file():
            if p.stat().st_size > 20 * 1024 * 1024:
                raise ValueError("文件超过 20MB，不自动处理：" + name)
            result[name] = digest(p.read_bytes())
    return result


def prompt_for(task, directory, attempt):
    """组装提示词。完整契约只内联一次，不回抄字段；工作区取真实 attempt.worktree。

    directory 是桥任务 store 目录（不是工作区），刻意不写进提示词；timeout 只来自契约字段，
    包装文本不重复。默认不测试，但明确写出契约授权的有限语法/定向检查可执行；并分行说明
    allowed_paths/artifacts 语义、进度与验收独立、执行者不自动人工 accept，允许读源码或报缺证据。
    """
    worktree = attempt.get("worktree") or "(未记录)"
    base_commit = attempt.get("base_commit") or "(未记录)"
    return "\n".join([
        "你是此任务的执行者。用简体中文交付。不要创建子代理。",
        "默认不执行测试／服务／桌面；只有契约明确授权的有限语法或定向检查（如 node --check、内存 compile）可执行，不擅自扩大范围。",
        "只在当前工作区内修改契约允许的路径；不要提交、切分支或修改 .git、外部配置、供应商、桌面或服务。",
        "allowed_paths 是可修改范围，artifacts 是必须交付的产物；产物须落在允许范围内，越界改动会被拒收。",
        "完整契约已内联一次（下列 JSON 是唯一契约来源，勿逐字复述其中字段）：",
        json.dumps(task, ensure_ascii=False),
        "task_id=" + task["task_id"] + " attempt_id=" + attempt["attempt_id"],
        "工作区（真实 attempt.worktree）：" + worktree,
        "基线 commit：" + base_commit,
        "进度（NOT_STARTED/READY/RUNNING/DONE/BLOCKED）与验收（PASS/FAIL/BLOCKED）是两套独立字段；执行者不得自动判定人工 accept。",
        "允许阅读本工作区源码；遇到不明确接口或缺少依据时报告阻塞及所需证据，不猜测、不伪造通过。",
        "结束时说明实际修改、未执行的验证和阻塞；不要写入桥的状态文件。",
    ])


def run(store, task_id, config, expected, approve, acknowledge, prepare_only=False, confirm_visible=False):
    if not approve or not acknowledge:
        raise ValueError("先审阅 plan；执行需 --approve-start 和 --ack-unmetered，不能将未实证的预算当已限制")
    directory = task_dir(store, task_id)
    with writer(store):
        task, state = load_task(directory)
        if state["status"] not in ("ready", "rework", "prepared"):
            raise ValueError("任务当前状态不允许启动；失败后的重派先 revise")
        is_shared = adapters.shared(task)
        if prepare_only and (not is_shared or state["status"] == "prepared"):
            raise ValueError("仅 shared 模式的待规划任务可以创建空会话；不会重复创建")
        if is_shared and not prepare_only and (state["status"] != "prepared" or not confirm_visible):
            raise ValueError("先 prepare 创建空会话，并确认真实客户端可见后再发送提示词")
        approved = plan(store, task_id, config)
        if approved["plan_sha256"] != expected:
            raise ValueError("计划已变化或哈希不匹配；必须重新审阅")
        if state["status"] == "prepared":
            return resume_shared(store, directory, task, state, config, approved, expected, confirm_visible)
        attempt_id = "attempt-" + uuid.uuid4().hex
        attempt_dir = directory / "attempts" / attempt_id
        attempt_dir.mkdir(parents=True)
        try:
            worktree, relocated = worktree_path(attempt_dir, attempt_id, config)
            attempt = {**approved, "attempt_id": attempt_id, "worktree": str(worktree), "started_at": now()}
            if relocated:
                attempt["worktree_relocated_from"] = str(attempt_dir / "workspace")
            atomic(attempt_dir / "attempt.json", attempt)
            if relocated:
                event(directory, "worktree_relocated", attempt_id=attempt_id, worktree=str(worktree),
                      reason="路径过长时 git 报 '$GIT_DIR' too big；已按安全阈值改放短根")
            worktree.parent.mkdir(parents=True, exist_ok=True)
            git(Path(task["repo"]), "worktree", "add", "--detach", str(worktree), approved["base_commit"])
            git_marker = digest((worktree / ".git").read_bytes())
            before = snapshot(worktree)
            for ref, expected_hash in approved["inputs"].items():
                if before.get(ref) != expected_hash:
                    raise ValueError("工作区输入与批准的计划不一致：" + ref)
            atomic(attempt_dir / "before.json", before)
            if is_shared:
                server = adapters.service(config)
                # 先写发送意图。超时或崩溃后保留现场，禁止自动再建一次。
                atomic(attempt_dir / "native-create-intent.json", {"server": server, "time": now()})
                info = adapters.native_api(config, server, "session.create",
                                          {"title": task.get("title", task_id),
                                           "location": {"directory": str(worktree)}}, cwd=worktree)
                session_id = info.get("id") if isinstance(info, dict) else None
                if not isinstance(session_id, str) or not re.fullmatch(r"ses_[A-Za-z0-9]+", session_id):
                    raise ValueError("session.create 未返回可核对的 session ID；不发送提示词")
                native = {"server": server, "session_id": session_id, "title": task.get("title", task_id),
                          "directory": str(worktree), "model": task.get("model"), "created_at": now(),
                          "prompt_sent": False, "visibility": "awaiting_client_confirmation", "session": info}
                atomic(attempt_dir / "native-session.json", native)
                state.update(status="prepared", current_attempt=attempt_id, acceptance="BLOCKED")
                atomic(directory / "state.json", state)
                event(directory, "native_session_created", attempt_id=attempt_id, session_id=session_id)
                return state
            command, input_bytes = adapters.build(task, prompt_for(task, directory, attempt), config)
        except Exception as error:
            result = {"attempt_id": attempt_id, "task_id": task_id, "revision": state["revision"],
                      "contract_sha256": state["contract_sha256"], "exit_code": None,
                      "acceptance": "FAIL", "stop_reason": "preparation_error", "finished_at": now(),
                      "native_errors": [{"stage": "prepare", "message": scrub(str(error))}],
                      "model_started": False}
            atomic(attempt_dir / "result.json", result)
            state.update(status="failed", current_attempt=attempt_id, acceptance="FAIL",
                         result_sha256=digest((attempt_dir / "result.json").read_bytes()))
            atomic(directory / "state.json", state)
            event(directory, "preparation_failed", attempt_id=attempt_id, error=scrub(str(error)))
            return {**state, "result_file": str(attempt_dir / "result.json")}
        return execute_attempt(directory, task, state, config, expected, attempt_dir, attempt,
                               worktree, git_marker, before, command, input_bytes)


def resume_shared(store, directory, task, state, config, approved, expected, confirm_visible):
    attempt_dir = directory / "attempts" / state["current_attempt"]
    attempt = read(attempt_dir / "attempt.json")
    native = read(attempt_dir / "native-session.json")
    worktree = recorded_worktree(attempt_dir)
    if attempt["plan_sha256"] != expected or native.get("prompt_sent") or (attempt_dir / "prompt-intent.json").exists():
        raise ValueError("计划变化或提示词已提交／提交状态不明；禁止重复发送")
    before = read(attempt_dir / "before.json")
    if snapshot(worktree) != before or git(worktree, "rev-parse", "HEAD").decode().strip() != approved["base_commit"]:
        raise ValueError("空会话准备后工作区发生变化；拒绝执行")
    info = adapters.native_api(config, native["server"], "session.get", session_id=native["session_id"], cwd=worktree)
    if not isinstance(info, dict) or info.get("id") != native["session_id"]:
        raise ValueError("原生会话回读不一致")
    prompt = prompt_for(task, directory, attempt)
    command, input_bytes = adapters.build(task, prompt, {**config, "native_session": native})
    native.update(visibility="user_confirmed" if confirm_visible else "unverified", prompt_sha256=digest(prompt.encode("utf-8")))
    atomic(attempt_dir / "native-session.json", native)
    # 任何后续异常不自动重派，避免双重计费。
    atomic(attempt_dir / "prompt-intent.json", {"time": now(), "prompt_sha256": native["prompt_sha256"],
                                               "session_id": native["session_id"]})
    return execute_attempt(directory, task, state, config, expected, attempt_dir, attempt, worktree,
                           digest((worktree / ".git").read_bytes()), before, command, input_bytes)


def native_cost(info):
    """从原生会话回读里取真实用量。本机实测（OpenCode 2.0.20）session.get 返回
    cost 与 tokens{input,output,reasoning,cache{read,write}}，故不再以 None 表示"未知"。
    取不到时返回 None，并由调用方注明未取到，而不是补零。"""
    if not isinstance(info, dict):
        return None
    cost = info.get("cost")
    tokens = info.get("tokens")
    if cost is None and not tokens:
        return None
    return {"cost": cost, "tokens": tokens,
            "note": "原生服务回读的真实计费与用量；跨执行器语义未逐一核实，不据此推算请求数"}


def probe_health(attempt_dir, config, native, session_id, worktree, run_started, last_stream_at):
    """执行期只读健康探针：只回读、只记录，不改判定、不发模型。
    本机实测 session.get 单次往返约 0.08 秒，故按 10 秒节奏采样开销可忽略。
    静默或停滞只写入 health.json 供审查者判断，本版本不在执行中触发中断。"""
    try:
        info = adapters.native_api(config, native["server"], "session.get", session_id=session_id, cwd=worktree)
    except Exception:
        return  # 探针失败不得影响主循环；下一次采样再试
    time_info = (info.get("time") or {}) if isinstance(info, dict) else {}
    idle = time_info.get("idle")
    updated = time_info.get("updated")
    # stall 用服务端时间字段算，不用本地墙钟，避免把本地调度延迟算成模型停滞。
    stall = None
    if isinstance(idle, (int, float)) and isinstance(updated, (int, float)) and idle >= updated:
        stall = (idle - updated) / 1000.0
    atomic(attempt_dir / "health.json", {
        "at": now(),
        "outcome": info.get("outcome") if isinstance(info, dict) else None,
        "token_usage": native_cost(info),
        "stall_seconds": stall,
        "silence_seconds": round(time.monotonic() - last_stream_at, 1),
        "elapsed_seconds": round(time.monotonic() - run_started, 1),
        "note": "只读回读；不代表模型已停止或未计费。stall 由服务端时间字段算出，非本地墙钟"})


def interrupt_and_confirm(attempt_dir, config, native, session_id, worktree):
    """中断原生会话并有界确认停止。返回 (stop_outcome, info)。

    本机实测：session.interrupt 幂等（对已完成会话重复调用不报错、不改动会话数据），
    停止后 outcome 立即非空，故确认循环按 1 秒节奏、上限 STOP_CONFIRM_WAIT_SECONDS。
    单次采样必然误判——历史 b-flash-r5 在中断回执到手 0.25 秒后采样不到 outcome，
    被记成 stop_unconfirmed；事后官方回读为 interrupted。"""
    record = {"requested_at": now()}
    try:
        receipt = adapters.native_api(config, native["server"], "session.interrupt",
                                      session_id=session_id, cwd=worktree)
        record["receipt"] = receipt
    except Exception as error:
        record["interrupt_error"] = scrub(str(error))
    stop_outcome = None
    info = None
    deadline = time.monotonic() + STOP_CONFIRM_WAIT_SECONDS
    while True:
        try:
            info = adapters.native_api(config, native["server"], "session.get",
                                       session_id=session_id, cwd=worktree)
        except Exception as error:
            record["confirm_error"] = scrub(str(error))
            break
        outcome = info.get("outcome") if isinstance(info, dict) else None
        if isinstance(outcome, str) and outcome:
            stop_outcome = outcome
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(1.0)
    record.update(confirmed=stop_outcome is not None, outcome=stop_outcome,
                  waited_seconds=round(STOP_CONFIRM_WAIT_SECONDS - max(0.0, deadline - time.monotonic()), 1))
    atomic(attempt_dir / "interrupt.json", record)
    return stop_outcome, info


def execute_attempt(directory, task, state, config, expected, attempt_dir, attempt, worktree,
                    git_marker, before, command, input_bytes):
        attempt_id = attempt["attempt_id"]
        approved = attempt
        task_id = task["task_id"]
        native_path = attempt_dir / "native-session.json"
        native = read(native_path) if native_path.is_file() else None
        state.update(status="running", current_attempt=attempt_id, acceptance="BLOCKED", started_at=now())
        atomic(directory / "state.json", state)
        event(directory, "starting", attempt_id=attempt_id, plan_sha256=expected)
        process = tree = None
        exit_code = None
        stop_reason = None
        native_errors = []
        session_id = native["session_id"] if native else None
        native_warnings = []
        # service 模式探测不到本机 opencode 后台服务时会静默回落 standalone（每任务多付约 1.2s）。
        # 这里把回落显式记成警告与事件，避免“桥变慢了却查不到原因”。
        if (task.get("executor") == "opencode" and task.get("opencode_mode", "service") == "service"
                and "--standalone" in command):
            native_warnings.append({
                "stage": "mode",
                "message": "opencode_mode=service 但本机后台服务不可用，已回落 --standalone"
                           "（每任务多付约 1.2s）；执行 `opencode service start` 可恢复"})
            event(directory, "opencode_service_unavailable", attempt_id=attempt_id)
        stop_confirmed = None
        # 收尾循环确认到的停止结论（succeeded/interrupted/failed）。必须在此初始化：
        # 正常完成路径不会走中断分支，final_outcome 保持 None，落盘不能用它代替本值。
        stop_outcome = None
        started_ms = int(time.time() * 1000)
        interrupted = False
        final_usage = None      # 原生回读到的真实计费/用量；取不到保持 None，不补零
        final_outcome = None    # 中断确认得到的停止结论（succeeded/interrupted/failed）
        final_info = None       # 中断确认时顺带回读到的会话快照，收尾复用，避免二次等待
        streams = queue.Queue(maxsize=256)
        def pump(stream, name):
            try:
                for line in iter(stream.readline, b""):
                    streams.put((name, line))
            finally:
                streams.put((name, None))
                stream.close()
        def feed_input():
            try:
                if input_bytes:
                    process.stdin.write(input_bytes)
                    process.stdin.flush()
            except OSError as error:
                streams.put(("stdin_error", str(error)))
            finally:
                try:
                    process.stdin.close()
                except OSError:
                    pass
        try:
            deadline = time.monotonic() + task["timeout_seconds"]
            if native and adapters.service(config) != native["server"]:
                raise ValueError("发送前 OpenCode 后台服务发生变化；拒绝发送提示词")
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            process = subprocess.Popen(command, cwd=worktree, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       creationflags=flags, start_new_session=os.name != "nt")
            if native:
                native.update(prompt_sent=True, observer_pid=process.pid, observer_started_at=now())
                atomic(native_path, native)
            try:
                tree = adapters.ProcessTree(process)
            except Exception:
                process.terminate()
                process.wait(timeout=10)
                raise
            state["pid"] = process.pid
            atomic(directory / "state.json", state)
            for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                threading.Thread(target=pump, args=(stream, name), daemon=True).start()
            threading.Thread(target=feed_input, daemon=True).start()
            closed = set()
            stopped_at = None
            # 健康探针与静默计时：探针只回读不改判定；silence 用于审查者判断是否值得提前止损。
            run_started = time.monotonic()
            last_probe = run_started
            last_stream_at = run_started
            handles = {name: (attempt_dir / (name + ".log")).open("w", encoding="utf-8") for name in ("stdout", "stderr")}
            try:
                while len(closed) < 2:
                    cancel = attempt_dir / "cancel.json"
                    if cancel.exists() and read(cancel).get("attempt_id") == attempt_id:
                        stop_reason = "cancelled"
                    if time.monotonic() >= deadline and stop_reason is None:
                        stop_reason = "timeout"
                    if stop_reason:
                        stopped_at = stopped_at or time.monotonic()
                        if native and not interrupted:
                            interrupted = True
                            # 主循环停在这里直到确认完成（上限 STOP_CONFIRM_WAIT_SECONDS）：
                            # 让中断回执与停止结论都落盘，收尾就不用再等第二次。
                            final_outcome, final_info = interrupt_and_confirm(
                                attempt_dir, config, native, session_id, worktree)
                            if final_outcome is None:
                                native_errors.append({"stage": "interrupt",
                                                      "message": "已发出中断但在等待上限内未取到停止结论；需只读核查原生会话"})
                        tree.close()
                        if process.poll() is None:
                            process.wait(timeout=10)
                        break
                    # 完成主进程也关闭私有子进程，避免持有管道导致永不返回。
                    if process.poll() is not None:
                        tree.close()
                    if native and time.monotonic() - last_probe >= HEALTH_PROBE_INTERVAL:
                        last_probe = time.monotonic()
                        probe_health(attempt_dir, config, native, session_id, worktree, run_started, last_stream_at)
                    try:
                        name, line = streams.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    if name == "stdin_error":
                        native_errors.append({"stage": "stdin", "message": scrub(line)})
                        continue
                    if line is None:
                        closed.add(name)
                        continue
                    text = scrub(line.decode("utf-8", "replace"))
                    last_stream_at = time.monotonic()
                    parsed = None
                    if name == "stdout":
                        try:
                            parsed = json.loads(text)
                        except ValueError:
                            pass
                    if isinstance(parsed, dict):
                        if parsed.get("type") in ("thinking", "reasoning"):
                            continue  # 不向账本展示模型推理文本。
                        if task["executor"] == "dsh" and parsed.get("type") == "session":
                            session_id = parsed.get("sessionId")
                        if task["executor"] == "opencode":
                            observed = parsed.get("sessionID")
                            if observed:
                                if native and observed != session_id:
                                    native_errors.append({"stage": "session", "message": "输出会话与指定会话不一致"})
                                else:
                                    session_id = observed
                            part = parsed.get("part") or {}
                            tool_state = part.get("state") or {}
                            if tool_state.get("status") == "error":
                                native_warnings.append({"stage": "tool", "tool": part.get("tool"),
                                                        "message": tool_state.get("error", "原生工具失败")})
                            if native:
                                native.update(last_event=parsed.get("type"), last_event_at=now(),
                                              warnings=native_warnings[-20:])
                                if part.get("messageID") or parsed.get("messageID"):
                                    native["message_id"] = part.get("messageID") or parsed["messageID"]
                                atomic(native_path, native)
                        if parsed.get("type") == "error":
                            native_errors.append(parsed)
                        if task["executor"] == "dsh" and parsed.get("phase") == "turn_end":
                            reason = parsed.get("reason", {})
                            if reason.get("kind") != "completed":
                                native_errors.append(parsed)
                    handles[name].write(text)
                    handles[name].flush()
            finally:
                for f in handles.values():
                    f.close()
            exit_code = process.wait(timeout=10)
        except (Exception, KeyboardInterrupt) as error:
            stop_reason = stop_reason or "runner_error"
            native_errors.append({"message": scrub(str(error)), "exception": type(error).__name__})
        finally:
            if native and stop_reason and process and not interrupted:
                interrupted = True
                final_outcome, final_info = interrupt_and_confirm(attempt_dir, config, native, session_id, worktree)
                if final_outcome is None:
                    native_errors.append({"stage": "interrupt",
                                          "message": "已发出中断但在等待上限内未取到停止结论；需只读核查原生会话"})
            if tree:
                tree.close()
            if process and tree and process.poll() is None:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    native_errors.append({"stage": "stop", "message": "自身进程未确认停止；需只读核查 PID"})
            if process and process.poll() is not None:
                exit_code = process.returncode
            if native:
                # 即使观察进程结束，也要向原生服务回读；不将断开观察等同于停止任务。
                #
                # 本机实测（2026-10-16，OpenCode 2.0.20，50 个真实会话）：
                #   session.get 的 time.idle 不是在会话停止时立即落盘，而是**按契约超时窗延迟出现**。
                #   四个被中断的会话，idle − updated 分别等于 170.163 / 180.077 / 300.453 / 900.275 秒，
                #   与各自契约的 timeout_seconds（170/180/300/900）逐项吻合。
                # 因此旧判据 idle >= started_ms 在超时后必然为假：即使会话早已按中断停止，
                # 也会被记成 stop_unconfirmed，导致产物按规则不得重派。
                # 停止的**可用判据是 outcome**：实测已停止会话立即返回 interrupted/succeeded/failed。
                # 但收尾落 outcome 需要一点时间，故改为有界确认循环，而不是单次采样。
                stop_outcome = final_outcome    # 主循环若已中断并确认，直接沿用其结论
                info = final_info               # 主循环已回读到快照时收尾不再二次等待
                confirm_deadline = time.monotonic() + STOP_CONFIRM_WAIT_SECONDS
                while info is None and stop_outcome is None:
                    try:
                        info = adapters.native_api(config, native["server"], "session.get", session_id=session_id, cwd=worktree)
                    except Exception as error:
                        native_errors.append({"stage": "native_status", "message": scrub(str(error))})
                        break
                    outcome = info.get("outcome") if isinstance(info, dict) else None
                    if isinstance(outcome, str) and outcome:
                        stop_outcome = outcome
                        break
                    if time.monotonic() >= confirm_deadline:
                        break
                    time.sleep(1.0)
                if info is None:
                    # 一次都没读上：必须留证，不能静默滑到"未确认"。
                    native_errors.append({"stage": "native_status",
                                          "message": "收尾未能回读原生会话；停止状态未知"})
                idle = (info.get("time") or {}).get("idle", 0) if isinstance(info, dict) else 0
                idle_settled = isinstance(idle, (int, float)) and idle >= started_ms
                stop_confirmed = stop_outcome is not None
                final_usage = native_cost(info)
                if info is not None:
                    native.update(session=info, stop_confirmed=stop_confirmed, stop_outcome=stop_outcome,
                                  idle_settled=idle_settled, observed_at=now())
                    atomic(native_path, native)
                if not stop_confirmed:
                    native_errors.append({"stage": "native_status", "message": "未确认原生会话已停稳；禁止重派或接收"})
                elif stop_outcome != "succeeded" and not stop_reason:
                    native_errors.append({"stage": "native_outcome", "message": str(stop_outcome)})
        # 失败同样留证；不自动修复、不清理 worktree。
        try:
            if digest((worktree / ".git").read_bytes()) != git_marker:
                raise ValueError("工作区 .git 指针被改写；拒绝继续读取可能越界的 Git 数据")
            if git(worktree, "rev-parse", "HEAD").decode().strip() != approved["base_commit"]:
                raise ValueError("执行者修改了工作区 HEAD；本任务禁止提交或切分支")
            after = snapshot(worktree)
            changes = [{"path": p, "before_sha256": before.get(p), "after_sha256": after.get(p)}
                       for p in sorted(before.keys() | after.keys()) if before.get(p) != after.get(p)]
            violations = [x["path"] for x in changes if not allowed(x["path"], task["allowed_paths"])]
            missing = [p for p in task["artifacts"] if not inside(worktree, p).is_file()]
            diff = scrub(git(worktree, "diff", "--binary", "HEAD").decode("utf-8", "replace"))
            (attempt_dir / "changes.diff").write_text(diff, encoding="utf-8")
            atomic(attempt_dir / "after.json", after)
        except Exception as error:
            changes, violations, missing = [], [], []
            native_errors.append({"message": scrub(str(error)), "stage": "collect"})
        # A 轴：执行事实。stop_reason 单独成字段，不再与产品越界／缺产物挤进同一个 bool。
        exec_failed = exit_code != 0 or bool(native_errors or violations or missing)
        facts = {"exit_code": exit_code, "stop_reason": stop_reason,
                 "native_error_count": len(native_errors),
                 "violations": violations, "missing_artifacts": missing,
                 "stop_confirmed": stop_confirmed, "timed_out": stop_reason == "timeout"}
        status = ("stop_unconfirmed" if native and stop_confirmed is not True
                  else "cancelled" if stop_reason == "cancelled"
                  else "failed" if exec_failed
                  else "awaiting_review")
        result = {"attempt_id": attempt_id, "task_id": task_id, "revision": state["revision"],
                  "contract_sha256": state["contract_sha256"], "exit_code": exit_code, "stop_reason": stop_reason,
                  "session_id": session_id, "changes": changes, "violations": violations, "missing_artifacts": missing,
                  "native_errors": native_errors, "finished_at": now(),
                  # acceptance 只表达机器执行安全性（BLOCKED / PASS，不再写 FAIL）；
                  # 执行失败由 execution_facts + review_gate 表达，CLI 退出码见 exit_code_for()。
                  "acceptance": "BLOCKED",
                  "native_warnings": native_warnings, "native_stop_confirmed": stop_confirmed,
                  "git_marker_sha256": git_marker,
                  "base_commit": approved["base_commit"],
                  "after_snapshot_sha256": digest(json_bytes(after)) if 'after' in locals() else None,
                  "upstream_requests": None, "usage": native.get("session", {}).get("tokens") if native else None,
                  # 本机实测 session.get 返回真实 cost 与 tokens，故单列一项；取不到为 None 而不是零。
                  "native_cost": final_usage,
                  "stop_outcome": stop_outcome,
                  "usage_note": "保留原生 JSON 日志；未核实跨执行器 usage 语义，不估算次数或费用"}
        result["execution_facts"] = facts
        # B 轴：可审准入。此处结果刚写出，稳定性用 stable=True 表达；result_sha256 的占位只为满足
        # "必须已登记结果哈希"这一项，真实哈希在落盘后回填到 state 与 CLI 退出码判定。
        gate = reviewability(attempt_dir, {**state, "result_sha256": "pending"},
                             result=result, stable=True)
        result["reviewable"] = gate["reviewable"]
        result["review_gate"] = gate
        atomic(attempt_dir / "result.json", result)
        current = read(directory / "state.json")
        if current["current_attempt"] != attempt_id or current["contract_sha256"] != result["contract_sha256"]:
            event(directory, "stale_result_rejected", attempt_id=attempt_id)
            raise ValueError("当前尝试或契约已变化；迟到结果已保存但拒绝覆盖状态")
        state.update(status=status, acceptance=result["acceptance"], finished_at=now(),
                     result_sha256=digest((attempt_dir / "result.json").read_bytes()),
                     reviewable=gate["reviewable"], review_gate=gate, execution_facts=facts)
        atomic(directory / "state.json", state)
        event(directory, "collected", attempt_id=attempt_id, status=status, exit_code=exit_code,
              result_sha256=digest((attempt_dir / "result.json").read_bytes()))
        return {**state, "result_file": str(attempt_dir / "result.json")}


def cancel(store, task_id, expected_attempt, approved):
    if not approved:
        raise ValueError("取消会中断该原生会话或结束桥自身任务进程树；需 --approve-cancel")
    directory = task_dir(store, task_id)
    _, state = load_task(directory)
    if state["status"] != "running" or state["current_attempt"] != expected_attempt:
        raise ValueError("任务不在运行或尝试编号已变化，不能取消")
    atomic(directory / "attempts" / expected_attempt / "cancel.json",
           {"attempt_id": expected_attempt, "requested_at": now()})
    return {"status": "cancel_requested", "attempt_id": expected_attempt,
            "acceptance": "BLOCKED", "note": "由持有进程组的运行器处理；请求写入不代表已停稳"}


def native_status(store, task_id, config):
    directory = task_dir(store, task_id)
    task, state = load_task(directory)
    if not adapters.shared(task) or not state.get("current_attempt"):
        raise ValueError("此任务没有共享原生会话")
    attempt_dir = directory / "attempts" / state["current_attempt"]
    native = read(attempt_dir / "native-session.json")
    info = adapters.native_api(config, native["server"], "session.get", session_id=native["session_id"])
    # 单独记录，避免与执行线程的 native-session.json 竞争；不改结果或伪造回收。
    observation = {"time": now(), "session": info, "session_id": native["session_id"],
                   "note": "原生回读成功不等于桥已完成文件回收或用户验收"}
    atomic(attempt_dir / "native-observation.json", observation)
    return observation


def stable_result(attempt_dir, state):
    """结果文件是否与登记的 result_sha256 一致。review() 与 reviewability() 共用，避免两处实现漂移。"""
    path = Path(attempt_dir) / "result.json"
    if not path.is_file():
        return False
    recorded = state.get("result_sha256")
    return isinstance(recorded, str) and digest(path.read_bytes()) == recorded


def execution_facts(result):
    """A 轴：执行事实。新结果直接读 execution_facts；旧结果现场派生，不改写历史文件。"""
    facts = result.get("execution_facts")
    if isinstance(facts, dict):
        return facts
    errors = result.get("native_errors")
    return {"exit_code": result.get("exit_code"),
            "stop_reason": result.get("stop_reason"),
            "native_error_count": len(errors) if isinstance(errors, list) else None,
            "violations": result.get("violations") or [],
            "missing_artifacts": result.get("missing_artifacts") or [],
            "stop_confirmed": result.get("native_stop_confirmed"),
            "timed_out": result.get("stop_reason") == "timeout",
            "derived_from": "legacy-result"}


def reviewability(attempt_dir, state, result=None, stable=None):
    """B 轴：可审准入（准入，不是通过）。顺序固定、可复算，见分账契约 §2.2。

    stop_reason == "timeout" 不参与判定（只进 execution_facts）；
    stop_confirmed is not True（未知）仍然硬拒，安全门不放宽。
    stable 由调用方给出时跳过磁盘复核（用于刚写出的结果）。
    """
    attempt_dir = Path(attempt_dir)
    if result is None:
        path = attempt_dir / "result.json"
        if not path.is_file():
            return {"reviewable": False, "reason": "result_missing"}
        result = read(path)
    facts = execution_facts(result)
    # 停止确认只对"有原生会话"的尝试适用：非原生（standalone / dsh）没有原生会话可确认，
    # 其停止事实由桥自身进程回收与 exit_code 表达。这与 status 判定里 `native and ...` 同口径。
    native_present = (attempt_dir / "native-session.json").is_file()
    reason = None

    def first(condition, name):
        nonlocal reason
        if reason is None and condition:
            reason = name

    first(native_present and facts.get("stop_confirmed") is not True, "stop_unconfirmed")
    first(facts.get("exit_code") != 0, "exit_nonzero")
    count = facts.get("native_error_count")
    first(bool(count) if isinstance(count, int) else bool(result.get("native_errors")), "native_errors")
    first(bool(facts.get("violations")), "violations")
    first(bool(facts.get("missing_artifacts")), "missing_artifacts")
    first(not result.get("after_snapshot_sha256"), "artifacts_incomplete")
    first(not result.get("git_marker_sha256"), "artifacts_incomplete")
    first(not result.get("base_commit"), "artifacts_incomplete")
    first(not isinstance(state.get("result_sha256"), str), "artifacts_incomplete")
    for change in result.get("changes") or []:
        # before_sha256 为 null 表示"这个文件之前不存在"（新建），是合法证据，不是缺证据。
        # 只有 after_sha256 缺失才算 artifacts_incomplete（否则新建文件的任务永远不可审）。
        if "before_sha256" in change and change.get("before_sha256") is not None \
                and not isinstance(change.get("before_sha256"), str):
            first(True, "artifacts_incomplete")
        first(not change.get("after_sha256"), "artifacts_incomplete")
    if stable is None:
        stable = stable_result(attempt_dir, state)
    first(stable is not True, "file_unstable")
    return {"reviewable": reason is None, "reason": reason}


def review_inputs(store, items):
    """只读复审引用的外部产物（分账契约 §3.3）：核对存在性、字节哈希与停稳确认。"""
    checked = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("审查输入必须是对象数组")
        task_id, attempt_id, sha = item.get("task_id"), item.get("attempt_id"), item.get("result_sha256")
        if not isinstance(task_id, str) or not isinstance(attempt_id, str) or not isinstance(sha, str):
            raise ValueError("审查输入缺 task_id / attempt_id / result_sha256")
        attempt_dir = task_dir(store, task_id) / "attempts" / attempt_id
        path = attempt_dir / "result.json"
        if not path.is_file():
            raise ValueError("审查输入引用的结果不存在：" + task_id + "/" + attempt_id)
        if digest(path.read_bytes()) != sha:
            raise ValueError("审查输入 sha256 与实际字节不一致：" + task_id + "/" + attempt_id)
        if execution_facts(read(path)).get("stop_confirmed") is not True:
            raise ValueError("审查输入引用的产物未确认停稳：" + task_id + "/" + attempt_id)
        artifacts = []
        worktree = recorded_worktree(attempt_dir)
        for artifact in item.get("artifacts") or []:
            if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str):
                raise ValueError("审查输入 artifacts 项缺 path")
            target = inside(worktree, artifact["path"])
            if not target.is_file() or digest(target.read_bytes()) != artifact.get("sha256"):
                raise ValueError("审查输入产物与字节不一致：" + artifact["path"])
            artifacts.append({"path": artifact["path"], "sha256": artifact["sha256"]})
        checked.append({"task_id": task_id, "attempt_id": attempt_id, "result_sha256": sha,
                        "artifacts": artifacts, "stop_confirmed": True})
    return checked


def review(store, task_id, decision, audit, user, verdict="blocked", review_input=None, reason=None):
    directory = task_dir(store, task_id)
    with writer(store):
        task, state = load_task(directory)
        attempt_id = state.get("current_attempt")
        attempt_dir = directory / "attempts" / attempt_id if attempt_id else None
        if decision == "void":
            # 作废 = 人工判定"这个任务没有价值"。它不依赖机器证据，因此**不看可审准入、也不要求 attempt 存在**
            # （最该作废的恰恰是那些从没跑过、或跑完发现没用的任务）。
            if state["status"] in ("running", "prepared", "cancel_requested"):
                raise ValueError("运行中／待确认的任务不能作废；请先用 cancel 中断")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("作废必须填写理由")
            if isinstance(state.get("void"), dict):
                raise ValueError("任务已经作废过；审计记录不覆盖，如需重新判定请新建任务说明原因")
            record = reason.strip()[:2000]
            previous_status = state.get("status")
            state["void"] = {"reason": record, "at": now(), "actor": "user",
                             "previous_status": previous_status,
                             "previous_acceptance": state.get("acceptance")}
            state.update(status="cancelled", acceptance="BLOCKED")
            atomic(directory / "state.json", state)
            event(directory, "void_task", reason=record, previous_status=previous_status)
            return state
        if state["status"] not in ("awaiting_review", "awaiting_user"):
            gate = (state.get("review_gate") if isinstance(state.get("review_gate"), dict)
                    else reviewability(attempt_dir, state) if attempt_dir else {"reviewable": False, "reason": "no_attempt"})
            if not gate.get("reviewable"):
                raise ValueError("任务未进入可审查状态（review_gate.reason=%s）" % gate.get("reason"))
        result_path = attempt_dir / "result.json"
        if digest(result_path.read_bytes()) != state.get("result_sha256"):
            raise ValueError("结果文件被改写；拒绝接收")
        result = read(result_path)
        if result["attempt_id"] != state["current_attempt"] or result["contract_sha256"] != state["contract_sha256"]:
            raise ValueError("结果与当前任务不匹配")
        evidence = {}
        for key, value in (("audit", audit), ("user", user)):
            if value:
                p = Path(value).resolve()
                if not p.is_file() or not p.stat().st_size:
                    raise ValueError("证据文件不存在或为空：" + key)
                evidence[key] = {"path": str(p), "sha256": digest(p.read_bytes())}
        if decision == "accept":
            # 机器接收门一字不改：可审 ≠ 可接收。
            if result["exit_code"] != 0 or result["violations"] or result["missing_artifacts"] or result["native_errors"]:
                raise ValueError("机器证据不满足，拒绝接收")
            if set(evidence) != {"audit", "user"}:
                raise ValueError("接收必须同时附只读审计和用户真实路径证据")
            worktree = recorded_worktree(attempt_dir)
            if digest((worktree / ".git").read_bytes()) != result["git_marker_sha256"]:
                raise ValueError("审查期间 .git 指针发生变化，拒绝接收")
            if git(worktree, "rev-parse", "HEAD").decode().strip() != result["base_commit"]:
                raise ValueError("审查期间 HEAD 发生变化，拒绝接收")
            if digest(json_bytes(snapshot(worktree))) != result["after_snapshot_sha256"]:
                raise ValueError("回收后工作区又被修改，拒绝接收")
            state.update(status="accepted", acceptance="PASS")
            # D 轴：人工接收。acceptance 只表达机器执行安全性，不再承载产品意见。
            state["human_accept"] = {"decision": "accept", "evidence": evidence, "at": now()}
        elif decision == "reject":
            # D 轴：人工打回。回到可修订状态（rework），不写 PASS、不改历史产物、不自动重跑。
            if state["status"] not in ("awaiting_review", "awaiting_user"):
                raise ValueError("只有待审查／待接收的任务可以打回（当前 %s）" % state["status"])
            if "audit" not in evidence:
                raise ValueError("打回也需要只读审计证据")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("打回必须填写理由（会作为反馈记录，供下一次修订使用）")
            state.update(status="rework", acceptance="BLOCKED")
            state["human_accept"] = {"decision": "reject", "reason": reason.strip()[:2000],
                                     "evidence": evidence, "at": now()}
        elif decision == "void":
            raise ValueError("作废分支已在入口处理（不会走到这里）")
        elif decision == "dismiss":
            # GitHub 式撤销打回：只对"已被人工打回"的任务生效，必须填理由，写 revisions 账后回到待接收。
            previous = state.get("human_accept") or {}
            if previous.get("decision") != "reject":
                raise ValueError("只有被人工打回的任务可以撤销打回")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("撤销打回必须填写理由")
            record = reason.strip()[:2000]
            state["revisions"] = list(state.get("revisions") or []) + [{
                "action": "dismiss_reject", "at": now(), "actor": "user", "reason": record,
                "previous_decision": previous.get("decision"), "previous_reason": previous.get("reason"),
                "previous_at": previous.get("at")}]
            state.update(status="awaiting_user", acceptance="BLOCKED")
            state["human_accept"] = {**previous, "dismissed": True, "dismissed_at": now(),
                                     "dismissed_reason": record}
        else:
            if "audit" not in evidence:
                raise ValueError("审计阶段需要证据文件")
            if state["status"] == "accepted":
                raise ValueError("已接收的任务不能重新打开只读审查；如需复审请先打回或修订")
            if verdict not in ("pass", "fail", "blocked"):
                raise ValueError("verdict 只支持 pass / fail / blocked")
            state.update(status="awaiting_user", acceptance="BLOCKED")
            # C 轴：产品意见（只读审查结论），与人工接收分开记录。
            state["product_review"] = {"verdict": verdict, "reviewer": "user",
                                       "evidence_sha256": evidence["audit"]["sha256"],
                                       "at": now(), "findings": []}
        if review_input:
            evidence["review_inputs"] = review_inputs(store, read(Path(review_input)))
        gate = reviewability(attempt_dir, state, result=result)
        state["reviewable"] = gate["reviewable"]
        state["review_gate"] = gate
        state["review_evidence"] = evidence
        atomic(directory / "state.json", state)
        event(directory, "human_review", decision=decision,
              verdict=verdict if decision == "audit" else None,
              evidence=evidence, reviewable=gate["reviewable"], review_gate_reason=gate["reason"])
    return state


def backfill(store, task_id, approved):
    """§4.2 存量回填：只补 execution_facts / reviewable / review_gate。

    绝不改 acceptance、status、review_evidence，也不倒推结论；写前把 result.json 与 state.json
    各备份为 *.bak-<epoch-ms>。result.json 字节会变，因此必须同步 state.result_sha256，
    否则后续 review() 会按"结果文件被改写"拒绝（这不是改验收或状态，是保持登记一致）。
    """
    if not approved:
        raise ValueError("回填会写历史产物；需 --approve-backfill")
    directory = task_dir(store, task_id)
    with writer(store):
        task, state = load_task(directory)
        attempt_id = state.get("current_attempt")
        if not attempt_id:
            return {"task_id": task_id, "changed": False, "note": "没有当前尝试，无法派生可审准入"}
        attempt_dir = directory / "attempts" / attempt_id
        path = attempt_dir / "result.json"
        if not path.is_file():
            return {"task_id": task_id, "attempt_id": attempt_id, "changed": False, "note": "当前尝试没有 result.json"}
        result = read(path)
        if isinstance(result.get("execution_facts"), dict) and isinstance(result.get("review_gate"), dict):
            return {"task_id": task_id, "attempt_id": attempt_id, "changed": False,
                    "review_gate": result["review_gate"], "note": "已有四轴字段，未改动"}
        keep = {key: state.get(key) for key in ("status", "acceptance", "review_evidence")}
        gate = reviewability(attempt_dir, state, result=result)
        stamp = int(time.time() * 1000)
        backups = []
        for target in (path, directory / "state.json"):
            backup = target.with_name(target.name + ".bak-" + str(stamp))
            backup.write_bytes(target.read_bytes())
            backups.append(str(backup))
        result["execution_facts"] = execution_facts(result)
        result["reviewable"] = gate["reviewable"]
        result["review_gate"] = gate
        atomic(path, result)
        state["result_sha256"] = digest(path.read_bytes())
        state["reviewable"] = gate["reviewable"]
        state["review_gate"] = gate
        state["execution_facts"] = result["execution_facts"]
        state["backfilled_at"] = now()
        assert all(state.get(key) == value for key, value in keep.items()), "回填不得改动 status/acceptance/review_evidence"
        atomic(directory / "state.json", state)
        event(directory, "backfilled", attempt_id=attempt_id, review_gate=gate, backups=backups)
        return {"task_id": task_id, "attempt_id": attempt_id, "changed": True,
                "reviewable": gate["reviewable"], "review_gate": gate,
                "execution_facts": result["execution_facts"], "backups": backups,
                "untouched": keep}


def revise(store, task_id, source):
    directory = task_dir(store, task_id)
    replacement = validate(read(source))
    with writer(store):
        task, state = load_task(directory)
        if state["status"] in ("running", "prepared", "stop_unconfirmed", "accepted") or replacement["task_id"] != task_id:
            raise ValueError("运行中／已接收任务不能修订，且 task_id 必须相同")
        state["revision"] += 1
        atomic(directory / ("contract.r" + str(state["revision"]) + ".json"), replacement)
        state.update(status="rework", acceptance="BLOCKED", current_attempt=None,
                     contract_sha256=digest(json_bytes(replacement)))
        atomic(directory / "state.json", state)
        event(directory, "revised", revision=state["revision"], contract_sha256=state["contract_sha256"])
    return state


def review_gates(store):
    """§4.1 只读派生视图：遍历各 task 当前 attempt，对现存 result 计算 review_gate；不写任何文件。"""
    store = Path(store)
    tasks = store / "tasks"
    derived = {}
    if not tasks.is_dir():
        return derived
    for directory in sorted(tasks.iterdir()):
        if not (directory / "state.json").is_file():
            continue
        state = read(directory / "state.json")
        attempt_id = state.get("current_attempt")
        attempt_dir = directory / "attempts" / attempt_id if attempt_id else None
        entry = {"title": state.get("title"), "status": state.get("status"),
                 "acceptance": state.get("acceptance"), "revision": state.get("revision"),
                 "current_attempt": attempt_id}
        if attempt_dir and (attempt_dir / "result.json").is_file():
            result = read(attempt_dir / "result.json")
            gate = reviewability(attempt_dir, state, result=result)
            facts = execution_facts(result)
            entry["reviewable"] = gate["reviewable"]
            entry["review_gate"] = gate
            entry["execution_facts"] = {key: facts.get(key) for key in
                                        ("exit_code", "stop_reason", "native_error_count", "violations",
                                         "missing_artifacts", "stop_confirmed", "timed_out")}
        else:
            entry["reviewable"] = False
            entry["review_gate"] = {"reviewable": False, "reason": "result_missing"}
        derived[directory.name] = entry
    return derived


def exit_code_for(result):
    """§5.5 退出码：不再依赖 acceptance 取值；旧结果仍看 acceptance == FAIL。"""
    facts = result.get("execution_facts")
    exec_failed = bool(facts) and bool(facts.get("exit_code") != 0 or facts.get("native_error_count")
                                       or facts.get("violations") or facts.get("missing_artifacts"))
    return 1 if (result.get("acceptance") == "FAIL" or exec_failed) else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("--store", type=Path, default=ROOT / "data")
    parser.add_argument("--config", type=Path)
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("doctor")
    p = subs.add_parser("submit"); p.add_argument("contract", type=Path)
    p = subs.add_parser("backfill")
    p.add_argument("task_id", nargs="?")
    p.add_argument("--all", dest="all_tasks", action="store_true", help="对 store 内全部任务回填")
    p.add_argument("--approve-backfill", action="store_true", help="确认会写历史 result/state（含逐文件备份）")
    for name in ("plan", "status", "prepare", "run", "cancel", "review", "revise"):
        p = subs.add_parser(name); p.add_argument("task_id")
        if name in ("prepare", "run"):
            p.add_argument("--plan-sha256", required=True)
            p.add_argument("--approve-start", action="store_true")
            p.add_argument("--ack-unmetered", action="store_true")
            p.add_argument("--confirm-visible", action="store_true")
        if name == "cancel":
            p.add_argument("--attempt-id", required=True)
            p.add_argument("--approve-cancel", action="store_true")
        if name == "review":
            p.add_argument("--decision", choices=("audit", "accept", "reject", "dismiss", "void"), required=True)
            p.add_argument("--audit-evidence", type=Path)
            p.add_argument("--user-evidence", type=Path)
            p.add_argument("--verdict", choices=("pass", "fail", "blocked"), default="blocked",
                           help="只读审查（audit）的产品意见；accept 不使用本字段")
            p.add_argument("--review-input", type=Path,
                           help="可选：只读复审引用的外部产物数组（JSON，分账契约 §3.3）")
            p.add_argument("--reason", help="打回理由；--decision reject 时必填，写入 human_accept.reason")
        if name == "revise":
            p.add_argument("contract", type=Path)
    args = parser.parse_args()
    if not 1 <= args.limit <= 3:
        parser.error("Demo --limit 必须为 1–3；每次 run 仅执行一个任务")
    store = args.store.resolve()
    config = read(args.config) if args.config else {}
    if args.command == "doctor":
        result = {"facts": adapters.facts(config), "store": str(store),
                  "review_gates": review_gates(store), "model_requests": 0, "acceptance": "BLOCKED"}
    elif args.command == "submit": result = submit(store, args.contract)
    elif args.command == "plan": result = plan(store, args.task_id, config)
    elif args.command == "status": result = load_task(task_dir(store, args.task_id))[1]
    elif args.command in ("prepare", "run"): result = run(store, args.task_id, config, args.plan_sha256, args.approve_start, args.ack_unmetered, args.command == "prepare", args.confirm_visible)
    elif args.command == "cancel": result = cancel(store, args.task_id, args.attempt_id, args.approve_cancel)
    elif args.command == "review": result = review(store, args.task_id, args.decision, args.audit_evidence, args.user_evidence, args.verdict, args.review_input, args.reason)
    elif args.command == "backfill":
        if args.all_tasks:
            tasks_dir = store / "tasks"
            targets = sorted(p.name for p in tasks_dir.iterdir() if (p / "state.json").is_file()) if tasks_dir.is_dir() else []
            result = {"store": str(store), "approved": bool(args.approve_backfill),
                      "tasks": {name: backfill(store, name, args.approve_backfill) for name in targets}}
        elif args.task_id:
            result = backfill(store, args.task_id, args.approve_backfill)
        else:
            parser.error("backfill 需要 task_id 或 --all")
    else: result = revise(store, args.task_id, args.contract)
    print(scrub(json.dumps(result, ensure_ascii=False, indent=2)))
    return exit_code_for(result)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    try:
        sys.exit(main())
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(json.dumps({"acceptance": "BLOCKED", "error": scrub(str(error))}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
