"""B2 提示词去重复、anchors 契约与缺 title 标签的三组定向离线检查（--limit 1–3）。

组 1：prompt 单次内联契约 + 真实 attempt.worktree/base_commit + 旧契约兼容；
      并断言包装行仍分行给出 allowed_paths/artifacts 语义、进度与验收独立、执行者不自动
      人工 accept、允许读源码/报缺证据，且默认不测试但写明契约授权的有限检查可执行。
组 2：anchors 合法/越界/反向/非正整数/未哈希输入，以及 artifacts 越界拒绝（含正例）。
组 3：用本机已有 Node 实际执行 web/task-graph.js 的标签逻辑（无浏览器/联网），核对三条历史
      缺 title 任务：图内节点用尾部限长标签且互不相同，文本列表直接显示完整 task_id，
      带 title 任务仍显示 title；完整 task_id 也在列表按钮 title 属性保留。不是纯字符串出现断言。

--limit N 在执行前截断：只运行前 N 组（不是运行全部再裁输出）。只做本地文件读写与一次本地 Node
子进程（无浏览器、无联网、无模型请求、不启动持久服务）。本修订明确授权 --limit 3 一次与纯语法
检查（node --check、内存 compile）；禁止全量与持久服务。失败如实记录并保留报告；不自动重跑。
"""
import argparse
import copy
import json
from pathlib import Path
import subprocess
import sys
import uuid

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import adapters
import bridge

# 历史缺 title 任务的真实 task_id（来源：docs/ui-restored.txt 文本列表 + 任务表全 ID）。
HISTORICAL_TITLELESS_IDS = (
    "opencode-go-fullchain-docs-r3",
    "opencode-readme-update-20261015",
    "ui-real-opencode-20261015-1238",
)
LABEL_MAX = 28  # 与 web/task-graph.js 的 LABEL_MAX 对齐

# 组 3 的 Node 夹具：在无浏览器环境下真实执行 web/task-graph.js 的 mount/renderList/标签逻辑。
NODE_HARNESS = r"""
"use strict";
const fs = require("fs");
const vm = require("vm");
const path = require("path");

const SRC = fs.readFileSync(path.join(process.cwd(), "web", "task-graph.js"), "utf8");

function makeEl() {
  return {
    hidden: false, textContent: "", innerHTML: "", title: "", dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    querySelectorAll() { return []; },
    addEventListener() {}
  };
}
const els = {};
["taskGraph", "graphList", "graphStatus", "graphNote", "graphEmpty", "graphError"].forEach(function (id) { els[id] = makeEl(); });

function wrapNode(spec) {
  return {
    _id: spec.data.id, _spec: spec,
    id() { return this._id; },
    data() { return this._spec.data; },
    addClass() {}, removeClass() {}, toggleClass() {}, hasClass() { return false; }
  };
}
function wrapEdge(spec) {
  const end = { hasClass() { return false; } };
  return { _id: spec.data.id, toggleClass() {}, hasClass() { return false; }, source() { return end; }, target() { return end; } };
}
function fakeCytoscape() {
  const cy = {
    _nodes: [], _edges: [],
    elements() { return { remove() { cy._nodes = []; cy._edges = []; }, filter() { return []; } }; },
    add(items) {
      items.forEach(function (spec) {
        if (spec.data && spec.data.source) { cy._edges.push(wrapEdge(spec)); }
        else { cy._nodes.push(wrapNode(spec)); }
      });
      return cy;
    },
    layout() { return { run() {} }; },
    nodes() { return cy._nodes; },
    edges() { return cy._edges; },
    getElementById() { return { length: 0 }; },
    on() {}, fit() {}, stop() {}, destroy() {}
  };
  fakeCytoscape.last = cy;
  return cy;
}

const sandbox = { console, JSON, Date, Math, RegExp, isNaN, Object, Array, String, Number };
sandbox.window = sandbox;
sandbox.document = { getElementById(id) { return els[id] || makeEl(); } };
sandbox.cytoscape = fakeCytoscape;
vm.createContext(sandbox);
vm.runInContext(SRC, sandbox, { filename: "web/task-graph.js" });

const tasks = __TASKS__;
const graph = sandbox.BridgeTaskGraph.mount({ graphId: "taskGraph", listId: "graphList", statusId: "graphStatus",
  noteId: "graphNote", emptyId: "graphEmpty", errorId: "graphError" });
if (!graph || !graph.available) { console.error("graph unavailable"); process.exit(3); }
graph.update(tasks, true);

const nodeLabels = fakeCytoscape.last._nodes.map(function (n) { return { id: n.id(), label: n.data().label }; });
const listItems = [];
const itemRe = /<button[^>]*\bdata-id="([^"]*)"[^>]*\btitle="([^"]*)"[^>]*>([\s\S]*?)<\/button>/g;
let match;
while ((match = itemRe.exec(els.graphList.innerHTML)) !== null) {
  listItems.push({ id: match[1], title: match[2], text: match[3] });
}
console.log(JSON.stringify({ node_labels: nodeLabels, list_items: listItems, note_html: els.graphNote.innerHTML }));
"""


def make_repo(home, name, files):
    repo = home / name
    repo.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return repo


def expected_wrapper(task, attempt):
    """prompt_for 中除内联 JSON 外的固定包装行；用于结构化断言 goal 等字段不在 JSON 外重复。"""
    return [
        "你是此任务的执行者。用简体中文交付。不要创建子代理。",
        "默认不执行测试／服务／桌面；只有契约明确授权的有限语法或定向检查（如 node --check、内存 compile）可执行，不擅自扩大范围。",
        "只在当前工作区内修改契约允许的路径；不要提交、切分支或修改 .git、外部配置、供应商、桌面或服务。",
        "allowed_paths 是可修改范围，artifacts 是必须交付的产物；产物须落在允许范围内，越界改动会被拒收。",
        "完整契约已内联一次（下列 JSON 是唯一契约来源，勿逐字复述其中字段）：",
        "task_id=" + task["task_id"] + " attempt_id=" + attempt["attempt_id"],
        "工作区（真实 attempt.worktree）：" + attempt["worktree"],
        "基线 commit：" + attempt["base_commit"],
        "进度（NOT_STARTED/READY/RUNNING/DONE/BLOCKED）与验收（PASS/FAIL/BLOCKED）是两套独立字段；执行者不得自动判定人工 accept。",
        "允许阅读本工作区源码；遇到不明确接口或缺少依据时报告阻塞及所需证据，不猜测、不伪造通过。",
        "结束时说明实际修改、未执行的验证和阻塞；不要写入桥的状态文件。",
    ]


def no_error(action):
    try:
        action()
        return True
    except ValueError:
        return False


def check_prompt(home):
    repo = make_repo(home, "prompt-repo", {"web/task-graph.js": "line1\nline2\nline3\nline4\n"})
    store = home / "prompt-store"          # 桥任务 store 目录：绝不能被当成工作区
    store.mkdir(parents=True, exist_ok=True)
    worktree = home / "prompt-worktree"    # 真实 attempt.worktree
    worktree.mkdir(parents=True, exist_ok=True)
    task = {"task_id": "prompt-fixture", "title": "提示词去重复夹具", "opencode_mode": "shared",
            "goal": "目标", "repo": str(repo.resolve()), "executor": "opencode",
            "model": "opencode-go/deepseek-v4.1-flash",
            "allowed_paths": ["web/task-graph.js"], "artifacts": ["web/task-graph.js"],
            "input_refs": ["web/task-graph.js"], "anchors": ["web/task-graph.js:1,3-4"],
            "completion_criteria": ["离线检查"]}
    validated = bridge.validate(copy.deepcopy(task))
    attempt = {"attempt_id": "attempt-fixture", "worktree": str(worktree), "base_commit": "a" * 40}
    prompt = bridge.prompt_for(validated, store, attempt)
    lines = prompt.split("\n")
    contract_json = json.dumps(validated, ensure_ascii=False)
    json_lines = [line for line in lines if line == contract_json]
    wrapper = [line for line in lines if line != contract_json]
    expected = expected_wrapper(validated, attempt)

    # 旧契约兼容：无 title / anchors / opencode_mode，缺省 standalone。
    old = {"task_id": "prompt-legacy", "goal": "旧契约", "repo": str(repo.resolve()), "executor": "opencode",
           "model": "opencode-go/deepseek-v4.1-flash",
           "allowed_paths": ["web/task-graph.js"], "artifacts": ["web/task-graph.js"],
           "completion_criteria": ["旧契约可用"]}
    old_validated = bridge.validate(copy.deepcopy(old))
    old_prompt = bridge.prompt_for(old_validated, store, attempt)
    old_json = json.dumps(old_validated, ensure_ascii=False)

    detail = {
        "contract_json_once": len(json_lines) == 1,
        "contract_json_content_matches": bool(json_lines) and json.loads(json_lines[0]) == validated,
        "goal_not_repeated_outside_json": wrapper == expected,
        "wrapper_states_allowed_artifacts_semantics": any("allowed_paths 是可修改范围，artifacts 是必须交付的产物" in line for line in wrapper),
        "wrapper_separates_progress_and_acceptance": any("两套独立字段" in line and "自动判定人工 accept" in line for line in wrapper),
        "wrapper_authorizes_only_limited_checks": any("契约明确授权的有限语法或定向检查" in line for line in wrapper),
        "wrapper_allows_source_or_evidence": any("允许阅读本工作区源码" in line and "报告阻塞及所需证据" in line for line in wrapper),
        "workspace_from_attempt_worktree": ("工作区（真实 attempt.worktree）：" + str(worktree)) in wrapper,
        "store_dir_not_used_as_workspace": str(store) not in prompt,
        "base_commit_inline": ("基线 commit：" + attempt["base_commit"]) in prompt,
        "legacy_contract_validates": bool(old_validated.get("goal")),
        "legacy_json_has_no_title": "title" not in json.loads(old_json),
        "legacy_prompt_single_json": old_prompt.count(old_json) == 1,
    }
    ok = all(detail.values())
    return {"check": "prompt_single_json_workspace_basecommit", "status": "PASS" if ok else "FAIL", "detail": detail}


def check_anchors_and_artifacts(home):
    repo = make_repo(home, "anchor-repo", {
        "web/task-graph.js": "\n".join("l%d" % i for i in range(1, 7)) + "\n",  # 6 行
        "docs/extra.md": "a\nb\n",
    })
    base = {"task_id": "anchor-fixture", "goal": "锚点与产物边界", "repo": str(repo.resolve()),
            "executor": "opencode", "model": "opencode-go/deepseek-v4.1-flash",
            "allowed_paths": ["web/task-graph.js"], "artifacts": ["web/task-graph.js"],
            "input_refs": ["web/task-graph.js"], "completion_criteria": ["离线检查"]}

    def accepts(**over):
        return no_error(lambda: bridge.validate(copy.deepcopy({**base, **over})))

    def rejects(**over):
        try:
            bridge.validate(copy.deepcopy({**base, **over}))
        except ValueError:
            return True
        return False

    detail = {
        "legal_anchor_accepted": accepts(anchors=["web/task-graph.js:1,3-4"]),
        "out_of_bounds_rejected": rejects(anchors=["web/task-graph.js:7"]),
        "reversed_range_rejected": rejects(anchors=["web/task-graph.js:5-2"]),
        "nonpositive_line_rejected": rejects(anchors=["web/task-graph.js:0"]),
        "bad_format_rejected": rejects(anchors=["web/task-graph.js:abc"]),
        "unanchored_file_not_in_input_refs_rejected": rejects(anchors=["docs/extra.md:1"]),
        "artifact_outside_allowed_paths_rejected": rejects(artifacts=["other.txt"]),
        "artifact_inside_allowed_paths_accepted": accepts(artifacts=["web/task-graph.js"]),
        "legacy_no_anchors_accepted": accepts(),
    }
    ok = all(detail.values())
    return {"check": "anchors_and_artifacts_boundaries", "status": "PASS" if ok else "FAIL", "detail": detail}


def expected_label(task_id):
    if len(task_id) <= LABEL_MAX:
        return task_id
    return "…" + task_id[-(LABEL_MAX - 1):]


def check_node_labels(home):
    rows = [{"task_id": tid,
             "task": {"task_id": tid, "goal": "缺 title 历史任务", "model": "opencode-go/deepseek-v4.1-flash"},
             "state": {"status": "awaiting_review", "acceptance": "BLOCKED"}}
            for tid in HISTORICAL_TITLELESS_IDS]
    rows.append({"task_id": "titled-fixture",
                 "task": {"task_id": "titled-fixture", "title": "B0 状态映射审查", "goal": "有 title"},
                 "state": {"status": "accepted", "acceptance": "PASS"}})
    harness = NODE_HARNESS.replace("__TASKS__", json.dumps(rows, ensure_ascii=False))
    script = home / "label-harness.js"
    script.write_text(harness, encoding="utf-8")
    node = Path(adapters.NODE)
    if not node.is_file():
        return {"check": "titleless_id_tail_and_full_list_node", "status": "FAIL",
                "detail": {"node_exists": False, "node": str(node)}}
    try:
        proc = subprocess.run([str(node), str(script)], cwd=str(ROOT), capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as error:
        return {"check": "titleless_id_tail_and_full_list_node", "status": "FAIL",
                "detail": {"node_error": bridge.scrub(str(error))}}
    if proc.returncode != 0:
        return {"check": "titleless_id_tail_and_full_list_node", "status": "FAIL",
                "detail": {"node_returncode": proc.returncode,
                           "stderr": bridge.scrub(proc.stderr.decode("utf-8", "replace"))[-800:]}}
    data = json.loads(proc.stdout.decode("utf-8"))
    labels = {item["id"]: item["label"] for item in data["node_labels"]}
    items = {item["id"]: item for item in data["list_items"]}

    def list_text(tid):
        return items.get(tid, {}).get("text")

    detail = {
        "graph_tail_matches_real_node_labels": all(labels.get(tid) == expected_label(tid) for tid in HISTORICAL_TITLELESS_IDS),
        "graph_tails_pairwise_distinct": len({labels.get(tid) for tid in HISTORICAL_TITLELESS_IDS}) == 3,
        "graph_title_still_prioritized": labels.get("titled-fixture") == "B0 状态映射审查",
        "list_shows_full_task_id_when_titleless": all(list_text(tid) == tid for tid in HISTORICAL_TITLELESS_IDS),
        "list_full_ids_pairwise_distinct": len({list_text(tid) for tid in HISTORICAL_TITLELESS_IDS}) == 3,
        "list_title_still_prioritized": list_text("titled-fixture") == "B0 状态映射审查",
        "list_keeps_full_id_title_attr": all(items.get(tid, {}).get("title") == tid for tid in HISTORICAL_TITLELESS_IDS),
        "node_labels": labels,
        "list_items": items,
    }
    ok = all(value for key, value in detail.items() if key not in ("node_labels", "list_items"))
    return {"check": "titleless_id_tail_and_full_list_node", "status": "PASS" if ok else "FAIL", "detail": detail}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, required=True)
    args = parser.parse_args()
    if not 1 <= args.limit <= 3:
        parser.error("--limit 必须为 1–3；首次使用 --limit 3")
    home = ROOT / "data" / ("prompt-check-" + uuid.uuid4().hex[:12])
    home.mkdir(parents=True, exist_ok=True)
    plan = (
        ("prompt_single_json_workspace_basecommit", lambda: check_prompt(home)),
        ("anchors_and_artifacts_boundaries", lambda: check_anchors_and_artifacts(home)),
        ("titleless_id_tail_and_full_list_node", lambda: check_node_labels(home)),
    )
    checks, executed = [], []
    for name, run in plan[:args.limit]:  # 执行前按 limit 截断：只运行前 N 组
        executed.append(name)
        try:
            checks.append(run())
        except Exception as error:  # 保真实错误，不伪造通过
            checks.append({"check": name, "status": "FAIL", "exception": type(error).__name__,
                           "message": bridge.scrub(str(error))})
    record = {"limit": args.limit, "executed_checks": executed, "checks": checks,
              "retained_directory": str(home), "model_requests": 0, "acceptance": "BLOCKED"}
    bridge.atomic(home / "checks.json", record)
    for check in checks:
        print(json.dumps(check, ensure_ascii=False))
    print(json.dumps({"limit": args.limit, "executed_checks": executed, "retained_directory": str(home),
                      "model_requests": 0, "acceptance": "BLOCKED"}, ensure_ascii=False))
    return 1 if any(item["status"] != "PASS" for item in checks) else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
