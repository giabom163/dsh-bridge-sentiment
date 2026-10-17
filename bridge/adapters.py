"""已核对本机接口的执行器；仅构造命令，不在导入时启动进程。"""
import os
from pathlib import Path
import re
import json
import subprocess
from urllib.parse import urlsplit

NODE = Path(r"C:\Users\Lenovo\.workbuddy\binaries\node\versions\22.22.2-3\node.exe")
DSH = Path(r"C:\Users\Lenovo\AppData\Roaming\npm\node_modules\@deepseek-ai\dsh\lib\bin.js")
OPENCODE = Path(r"C:\Users\Lenovo\AppData\Roaming\ai.opencode.desktop\cli\2.0.20\opencode-cli.exe")


def build(task, prompt, config):
    adapter = task["executor"]
    if adapter == "dsh":
        node = Path(config.get("node", str(NODE))).resolve()
        entry = Path(config.get("dsh", str(DSH))).resolve()
        if not node.is_file() or not entry.is_file():
            raise ValueError("DSH 或托管 Node 入口不存在")
        if task.get("model"):
            raise ValueError("本机 DSH headless 无模型参数；不能在契约中假装覆盖模型")
        return [str(node), str(entry), "--profile", "headless", "--json", "-"], prompt.encode("utf-8")
    if adapter == "opencode":
        entry = Path(config.get("opencode", str(OPENCODE))).resolve()
        if not entry.is_file():
            raise ValueError("OpenCode CLI 入口不存在")
        model = task.get("model")
        if not isinstance(model, str) or not re.fullmatch(r"[\w.-]+/[\w.#+-]+", model):
            raise ValueError("OpenCode 必须明确指定 provider/model；不使用不明默认值")
        args = [str(entry), "run", "--format", "json", "--model", model]
        mode = task.get("opencode_mode", "service")
        if mode == "shared":
            native = config.get("native_session")
            if native:
                local_server(native["server"])
                args += ["--session", native["session_id"]]
            # plan 只构造命令；真正运行必须先创建并记录原生会话。
            args += ["--title", task.get("title", task["task_id"])]
        elif mode == "service" and service_ready(config):
            # 复用本机后台服务：省掉每个任务 spawn 一个私有 serve 的开销
            # （2026-10-17 实测同一条最小任务：服务模式中位 2603ms vs standalone 3785ms）。
            pass
        else:
            args += ["--standalone"]
        agent = task.get("agent")
        if agent:
            if not re.fullmatch(r"[\w.-]+", agent):
                raise ValueError("非法 Agent 名称")
            args += ["--agent", agent]
        # 提示词一律走 stdin：argv 有 32767 字符上限，长契约会触发 WinError 206。
        return args, prompt.encode("utf-8")
    raise ValueError("只支持 dsh / opencode 执行器")


def shared(task):
    return task["executor"] == "opencode" and task.get("opencode_mode", "service") == "shared"


def service_ready(config):
    """后台服务是否可用；不可用时由调用方退回 --standalone，绝不假装共享。"""
    try:
        service(config)
        return True
    except Exception:
        return False


def local_server(value):
    p = urlsplit(value)
    if (p.scheme != "http" or p.hostname != "127.0.0.1" or not p.port or p.username
            or p.password or p.path not in ("", "/") or p.query or p.fragment):
        raise ValueError("共享服务必须是无凭据的本机 127.0.0.1 HTTP 地址")
    return value.rstrip("/")


def service(config):
    p = subprocess.run([config.get("opencode", str(OPENCODE)), "service", "status"],
                       capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if p.returncode:
        raise ValueError("OpenCode 后台服务未就绪；不自动退回私有实例")
    return local_server(p.stdout.decode("utf-8", "strict").strip())


def native_api(config, server, operation, body=None, session_id=None, cwd=None):
    # CLI 负责本机服务认证；桥不复制或记录凭据。
    if service(config) != local_server(server):
        raise ValueError("OpenCode 后台服务地址已变化；拒绝向其他服务发送任务")
    # 本机 2.0.20 的 --server 不继承后台服务认证，会要求 OPENCODE_PASSWORD。
    # 使用 CLI 自管连接；上面的 service status 核对将其固定到已记录服务。
    args = [config.get("opencode", str(OPENCODE)), "api", operation]
    if session_id:
        if not re.fullmatch(r"ses_[A-Za-z0-9]+", session_id):
            raise ValueError("原生会话编号非法")
        args += ["--param", "sessionID=" + session_id]
    if body is not None:
        args += ["--data", json.dumps(body, ensure_ascii=False)]
    p = subprocess.run(args, cwd=cwd, capture_output=True, timeout=30,
                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if p.returncode:
        raise ValueError("OpenCode API 失败（exit %s）：%s %s" %
                         (p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")))
    value = json.loads(p.stdout)
    return value.get("data", value) if isinstance(value, dict) else value


def facts(config):
    return {
        "dsh": {"entry": config.get("dsh", str(DSH)),
                "entry_exists": Path(config.get("dsh", str(DSH))).is_file(),
                "node_exists": Path(config.get("node", str(NODE))).is_file(),
                "model_selection": "原生 headless 配置；尚未运行验收"},
        "opencode": {"entry": config.get("opencode", str(OPENCODE)),
                     "entry_exists": Path(config.get("opencode", str(OPENCODE))).is_file(),
                     "mode": "shared：复用本机后台服务；旧契约缺省 standalone"},
        "platform": os.name,
        "upstream_request_limit": "未实证原生请求层；外层仅实现单任务与墙钟超时，未动态验收",
    }


class ProcessTree:
    """Windows Job 限制到自身进程树；不按进程名杀程序。"""
    def __init__(self, process):
        self.process = process
        self.handle = None
        if os.name != "nt":
            return
        import ctypes as c
        from ctypes import wintypes as w
        class Basic(c.Structure):
            _fields_ = [("process_time", c.c_longlong), ("job_time", c.c_longlong),
                        ("flags", w.DWORD), ("min_working_set", c.c_size_t),
                        ("max_working_set", c.c_size_t), ("active_process_limit", w.DWORD),
                        ("affinity", c.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]
        class IO(c.Structure):
            _fields_ = [(name, c.c_ulonglong) for name in
                        ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
        class Extended(c.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_memory", c.c_size_t),
                        ("job_memory", c.c_size_t), ("peak_process_memory", c.c_size_t),
                        ("peak_job_memory", c.c_size_t)]
        k = c.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        k.CreateJobObjectW.restype = w.HANDLE
        k.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        k.SetInformationJobObject.restype = w.BOOL
        k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        k.AssignProcessToJobObject.restype = w.BOOL
        k.CloseHandle.argtypes = [w.HANDLE]
        k.CloseHandle.restype = w.BOOL
        handle = k.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(c.get_last_error(), "无法创建任务进程组")
        info = Extended()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(handle, 9, c.byref(info), c.sizeof(info)):
            error = c.get_last_error()
            k.CloseHandle(handle)
            raise OSError(error, "无法设置任务进程组")
        if not k.AssignProcessToJobObject(handle, w.HANDLE(int(process._handle))):
            error = c.get_last_error()
            k.CloseHandle(handle)
            raise OSError(error, "无法登记自身进程；拒绝继续运行")
        self.kernel = k
        self.handle = handle

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
        elif os.name != "nt" and self.process.poll() is None:
            import signal
            os.killpg(self.process.pid, signal.SIGTERM)
