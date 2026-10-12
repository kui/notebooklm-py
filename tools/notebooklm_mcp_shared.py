# /// script
# requires-python = ">=3.11"
# dependencies = ["psutil>=5.9"]
# ///
"""notebooklm MCP サーバを全セッションで1プロセスだけ動かすための stdio ラッパー。

Claude Code は `.mcp.json` の stdio サーバをセッションごとに起動するので、
notebooklm-mcp を直接書くとセッションの数だけ本体が立ち上がり、それぞれが
Google の Cookie を独立にローテーションしてしまう。このラッパーは:

1. 共有の HTTP サーバ（notebooklm-mcp --transport http）が無ければ、
   切り離した監視プロセス（supervisor）経由で起動する。
2. 自分を「接続中のクライアント」として登録し、stdio <-> HTTP を mcp-proxy で中継する。
3. supervisor は登録クライアントの生存を定期確認し、誰もいなくなって
   猶予時間が過ぎたら本体を止めて自分も終了する。

使っていないときは何も動かず、最初のセッションで起動し、最後のセッションが終わると止まる。

状態は STATE_DIR（既定 ~/.notebooklm/shared-mcp）に置く:
  global.lock        起動・登録・停止判断をまとめて直列化するファイルロック
  clients/<pid>.json 接続中ラッパーの pid と生成時刻（pid 再利用対策）
  supervisor.json    supervisor の pid・生成時刻と、管理している本体の pid
  server.log         本体の標準出力・標準エラー
  shim.log           ラッパーと supervisor の動作ログ

標準出力は MCP の通信路なので、ラッパー自身は stdout に何も書かない。
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import psutil

HOST = "127.0.0.1"
PORT = int(os.environ.get("NOTEBOOKLM_SHARED_MCP_PORT", "9420"))
URL = f"http://{HOST}:{PORT}/mcp"
STATE_DIR = Path(
    os.environ.get("NOTEBOOKLM_SHARED_MCP_STATE", Path.home() / ".notebooklm" / "shared-mcp")
)
CLIENTS_DIR = STATE_DIR / "clients"
LOCK_PATH = STATE_DIR / "global.lock"
SUPERVISOR_PATH = STATE_DIR / "supervisor.json"
SERVER_LOG = STATE_DIR / "server.log"
SHIM_LOG = STATE_DIR / "shim.log"

SERVER_CMD = [
    "uvx",
    "--from",
    "notebooklm-py[mcp]",
    "notebooklm-mcp",
    "--transport",
    "http",
    "--host",
    HOST,
    "--port",
    str(PORT),
]
AUTH_CHECK_CMD = ["uvx", "--from", "notebooklm-py[mcp]", "notebooklm", "auth", "check", "--test"]
# 本体の起動前に行うので、クライアントの待ち時間（SERVER_READY_TIMEOUT）に収める。
AUTH_CHECK_TIMEOUT = 15.0
# ブラウザプロファイルにログインが残っていれば操作なしで終わる。残っていなければ
# サインイン画面が開いたまま LOGIN_BROWSER_TIMEOUT 秒待つ。Playwright が要るので [browser] も入れる。
LOGIN_BROWSER_TIMEOUT = 120
LOGIN_CMD = [
    "uvx",
    "--from",
    "notebooklm-py[mcp,browser]",
    "notebooklm",
    "login",
    "--browser-timeout",
    str(LOGIN_BROWSER_TIMEOUT),
]
LOGIN_TIMEOUT = LOGIN_BROWSER_TIMEOUT + 60.0
# ツールはどれも呼び出しごとに完結するので、MCP のセッションを持たせない。
# セッションがあると本体の再起動（認証の読み直し）で古いセッション ID が拒否され、
# 接続中の全クライアントが "Session terminated" になって手動の再接続が要る。
SERVER_ENV = {**os.environ, "FASTMCP_STATELESS_HTTP": "true"}
PROXY_CMD = [
    "uvx",
    "mcp-proxy@0.13.0",
    "--log-level",
    "WARNING",
    "--transport",
    "streamablehttp",
    URL,
]

# Claude Code の MCP 接続タイムアウト（30秒）に収まるようにする。
SERVER_READY_TIMEOUT = 25.0
# 最後のクライアントが消えてから本体を止めるまでの猶予。セッションの再接続で
# すぐ戻ってくる場合に本体を立て直さずに済むようにする。
IDLE_GRACE_SECONDS = float(os.environ.get("NOTEBOOKLM_SHARED_MCP_IDLE_GRACE", "30"))
SUPERVISE_INTERVAL = 3.0
LOCK_TIMEOUT = 60.0
# supervisor の起動を依頼してから、supervisor 自身が supervisor.json を書くまでの猶予。
# この間に来たクライアントは重複して起動を依頼しない。
SPAWN_PENDING_SECONDS = 20.0
RESTART_BACKOFF_SECONDS = 30.0
LOG_MAX_BYTES = 5 * 1024 * 1024

IS_WINDOWS = os.name == "nt"


def log(msg: str) -> None:
    with contextlib.suppress(OSError):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with SHIM_LOG.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{os.getpid()}] {msg}\n")


# --- ファイルロック ------------------------------------------------------------


@contextlib.contextmanager
def global_lock(timeout: float = LOCK_TIMEOUT) -> Iterator[None]:
    """プロセス間の排他ロック。プロセスが死ねば OS が解放する。"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    f = open(LOCK_PATH, "a+b")  # noqa: SIM115 - ロック中は開いたままにする
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if IS_WINDOWS:
                    import msvcrt

                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"lock timeout: {LOCK_PATH}") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            if IS_WINDOWS:
                import msvcrt

                f.seek(0)
                with contextlib.suppress(OSError):
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    finally:
        f.close()


# --- 小物 ------------------------------------------------------------------------


def write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def process_matches(pid: int | None, create_time: float | None) -> bool:
    """pid が生きていて、かつ記録した生成時刻と一致する（= pid 再利用ではない）か。"""
    if not pid:
        return False
    try:
        p = psutil.Process(pid)
        if create_time is not None and abs(p.create_time() - create_time) > 1.0:
            return False
        return p.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def port_listening() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((HOST, PORT)) == 0


def listener_pid() -> int | None:
    """PORT で LISTEN しているプロセスの pid。"""
    with contextlib.suppress(psutil.Error, OSError):
        for c in psutil.net_connections(kind="tcp"):
            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == PORT:
                return c.pid
    return None


def is_notebooklm_server(pid: int | None) -> bool:
    if not pid:
        return False
    with contextlib.suppress(psutil.Error):
        p = psutil.Process(pid)
        for proc in [p, *p.parents()]:
            cmd = " ".join(proc.cmdline())
            if "notebooklm-mcp" in cmd and "http" in cmd:
                return True
    return False


def kill_tree(pid: int) -> None:
    with contextlib.suppress(psutil.Error):
        root = psutil.Process(pid)
        procs = [*root.children(recursive=True), root]
        for p in procs:
            with contextlib.suppress(psutil.Error):
                p.terminate()
        _, alive = psutil.wait_procs(procs, timeout=5)
        for p in alive:
            with contextlib.suppress(psutil.Error):
                p.kill()


def detached_popen(
    cmd: list[str], stdout_path: Path, env: dict[str, str] | None = None
) -> subprocess.Popen:
    """呼び出し元（Claude Code のプロセスツリー）から切り離して起動する。"""
    out = open(stdout_path, "ab")  # noqa: SIM115 - 子プロセスに渡す
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": out,
        "stderr": subprocess.STDOUT,
        "env": env,
    }
    if IS_WINDOWS:
        # DETACHED_PROCESS と組み合わせると CREATE_NO_WINDOW は無視され、子の uv.exe などが
        # 見えるコンソールウィンドウを新しく作ってしまう。非表示のコンソールを持たせて
        # 子孫にもそれを引き継がせる。
        base = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        try:
            return subprocess.Popen(
                cmd, creationflags=base | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kwargs
            )
        except OSError:
            # ジョブがブレークアウェイを許可していない場合
            return subprocess.Popen(cmd, creationflags=base, **kwargs)
    return subprocess.Popen(cmd, start_new_session=True, **kwargs)


# --- クライアント登録 --------------------------------------------------------------


def client_path(pid: int) -> Path:
    return CLIENTS_DIR / f"{pid}.json"


def register_client() -> None:
    CLIENTS_DIR.mkdir(parents=True, exist_ok=True)
    me = psutil.Process()
    write_json_atomic(client_path(me.pid), {"pid": me.pid, "create_time": me.create_time()})


def unregister_client() -> None:
    with contextlib.suppress(OSError):
        client_path(os.getpid()).unlink()


def live_clients() -> int:
    """生存クライアント数を数え、死んだクライアントの登録は消す。ロック内で呼ぶ。"""
    count = 0
    if not CLIENTS_DIR.exists():
        return 0
    for path in CLIENTS_DIR.glob("*.json"):
        info = read_json(path)
        if info and process_matches(info.get("pid"), info.get("create_time")):
            count += 1
        else:
            with contextlib.suppress(OSError):
                path.unlink()
    return count


def supervisor_alive() -> bool:
    """supervisor が動いている、または起動中（SPAWN_PENDING_SECONDS 以内）か。"""
    info = read_json(SUPERVISOR_PATH)
    if not info:
        return False
    if "pending_since" in info:
        return time.time() - info["pending_since"] < SPAWN_PENDING_SECONDS
    return process_matches(info.get("pid"), info.get("create_time"))


# --- supervisor ------------------------------------------------------------------


def start_server() -> int:
    """本体を起動し pid を返す。既に notebooklm-mcp が PORT を握っていればそれを引き継ぐ。"""
    pid = listener_pid()
    if pid and is_notebooklm_server(pid):
        log(f"adopting existing server pid={pid}")
        return pid
    if port_listening():
        raise RuntimeError(f"port {PORT} is used by another program (pid={pid})")
    for path in (SERVER_LOG, SHIM_LOG):
        with contextlib.suppress(OSError):
            if path.stat().st_size > LOG_MAX_BYTES:
                os.replace(path, path.with_suffix(".log.old"))
    proc = detached_popen(SERVER_CMD, SERVER_LOG, env=SERVER_ENV)
    log(f"started server pid={proc.pid}")
    return proc.pid


def run_logged(cmd: list[str], timeout: float) -> bool:
    """コマンドを非表示で実行し、出力を shim.log に残す。終了コード 0 なら True。"""
    kwargs: dict = {"stdin": subprocess.DEVNULL, "capture_output": True, "timeout": timeout}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    name = " ".join(cmd[3:6])
    try:
        proc = subprocess.run(cmd, **kwargs)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"{name} failed: {e!r}")
        return False
    tail = (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()[-300:]
    log(f"{name} exit={proc.returncode}: {tail!r}")
    return proc.returncode == 0


def auth_ok() -> bool:
    """保存済み Cookie で NotebookLM のトークンが取れるか確かめる（必要なら Cookie の更新も行う）。"""
    return run_logged(AUTH_CHECK_CMD, AUTH_CHECK_TIMEOUT)


def supervise() -> int:
    me = psutil.Process()
    # 本体が止まっている間（PC の停止中など）は Cookie の更新が走らないので、
    # 起動のたびに確かめる。本体の起動前に済ませ、古い Cookie を読ませない。
    authed = auth_ok()
    with global_lock():
        info = read_json(SUPERVISOR_PATH)
        if info and "pending_since" not in info and supervisor_alive():
            log("another supervisor is alive; exiting")
            return 0
        server_pid = start_server()
        write_json_atomic(
            SUPERVISOR_PATH,
            {"pid": me.pid, "create_time": me.create_time(), "server_pid": server_pid},
        )
    if not authed:
        # Cookie が切れても NotebookLM はログイン画面へリダイレクトせず、未ログイン用のページを
        # 返すことがある。ライブラリはこれを認証切れと判定しないので、自動復旧は動かない。
        # ブラウザプロファイルのログインは Cookie より長く持つので、そこから取り直す。
        # 時間がかかるので、MCP の接続を待たせないよう本体の起動後に行う。
        log("auth check failed; running notebooklm login")
        if run_logged(LOGIN_CMD, LOGIN_TIMEOUT):
            with global_lock():
                kill_tree(server_pid)
                # 止めた本体がまだポートを握っていると start_server がそれを引き継いでしまう。
                deadline = time.monotonic() + 5
                while port_listening() and time.monotonic() < deadline:
                    time.sleep(0.2)
                server_pid = start_server()
                info = read_json(SUPERVISOR_PATH) or {}
                info["server_pid"] = server_pid
                write_json_atomic(SUPERVISOR_PATH, info)
            log("restarted server after login")
    server_ct = psutil.Process(server_pid).create_time() if psutil.pid_exists(server_pid) else None
    last_start = time.monotonic()
    idle_since: float | None = None
    try:
        while True:
            time.sleep(SUPERVISE_INTERVAL)
            with global_lock():
                n = live_clients()
                if n > 0:
                    idle_since = None
                elif idle_since is None:
                    idle_since = time.monotonic()
                elif time.monotonic() - idle_since >= IDLE_GRACE_SECONDS:
                    # 停止判断と supervisor.json の削除をロック内で行うので、
                    # この後に来たクライアントは「supervisor 不在」を見て新しく起動する。
                    log("no clients; stopping server")
                    kill_tree(server_pid)
                    with contextlib.suppress(OSError):
                        SUPERVISOR_PATH.unlink()
                    return 0
                if not process_matches(server_pid, server_ct):
                    # 起動直後に落ち続ける（認証ファイルが無い等）ときに再起動を連打しない。
                    if time.monotonic() - last_start < RESTART_BACKOFF_SECONDS:
                        continue
                    log("server exited unexpectedly; restarting")
                    last_start = time.monotonic()
                    server_pid = start_server()
                    server_ct = psutil.Process(server_pid).create_time()
                    info = read_json(SUPERVISOR_PATH) or {}
                    info["server_pid"] = server_pid
                    write_json_atomic(SUPERVISOR_PATH, info)
    except BaseException as e:
        log(f"supervisor crashed: {e!r}; stopping server")
        kill_tree(server_pid)
        with contextlib.suppress(OSError):
            SUPERVISOR_PATH.unlink()
        raise


# --- クライアント（.mcp.json から起動される側） ---------------------------------------


def spawn_supervisor() -> int:
    """supervisor を孫プロセスとして起動してすぐ終了する（--spawn-supervisor）。

    間に1段はさむことで supervisor の親プロセスが消え、クライアント側の
    プロセスツリーごと終了（taskkill /T など）されても巻き込まれない。
    """
    proc = detached_popen([sys.executable, os.path.abspath(__file__), "--supervise"], SHIM_LOG)
    log(f"spawned supervisor pid={proc.pid}")
    return 0


def ensure_supervisor() -> None:
    """登録と supervisor の起動確認を1つのロック内で行う。"""
    with global_lock():
        register_client()
        if supervisor_alive():
            return
        # supervisor が自分の情報で上書きするまでの「起動中」の印。
        write_json_atomic(SUPERVISOR_PATH, {"pending_since": time.time()})
        subprocess.call(
            [sys.executable, os.path.abspath(__file__), "--spawn-supervisor"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # supervisor がロックを取って supervisor.json を書くのはこのロックを抜けた後。


def wait_for_server() -> bool:
    deadline = time.monotonic() + SERVER_READY_TIMEOUT
    while time.monotonic() < deadline:
        if port_listening():
            return True
        time.sleep(0.2)
    return False


def client_main() -> int:
    try:
        ensure_supervisor()
        if not wait_for_server():
            log("server did not become ready in time")
            print(f"notebooklm shared MCP server did not start; see {SERVER_LOG}", file=sys.stderr)
            return 1
        log("proxying")
        # stdin/stdout はそのまま mcp-proxy に引き継ぐ。
        return subprocess.call(PROXY_CMD)
    finally:
        unregister_client()
        log("client exit")


def status() -> int:
    with global_lock():
        n = live_clients()
        info = read_json(SUPERVISOR_PATH)
    print(
        json.dumps(
            {
                "url": URL,
                "listening": port_listening(),
                "listener_pid": listener_pid(),
                "supervisor": info,
                "supervisor_alive": supervisor_alive(),
                "live_clients": n,
            },
            ensure_ascii=False,
        )
    )
    return 0


def restart_server() -> int:
    """本体だけを止める。supervisor が異常終了とみなして起動し直す（認証の読み直し用）。"""
    with global_lock():
        info = read_json(SUPERVISOR_PATH)
        if not info or not supervisor_alive() or not info.get("server_pid"):
            print(json.dumps({"restarted": False, "reason": "server is not running"}))
            return 0
        kill_tree(info["server_pid"])
        log(f"server pid={info['server_pid']} killed by --restart-server")
    print(json.dumps({"restarted": True}))
    return 0


def main(argv: list[str]) -> int:
    if "--supervise" in argv:
        return supervise()
    if "--spawn-supervisor" in argv:
        return spawn_supervisor()
    if "--status" in argv:
        return status()
    if "--restart-server" in argv:
        return restart_server()
    return client_main()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
