"""一键启动本机工作台；已有构建可在没有Node.js的电脑上运行。"""
import os
import json
import shutil
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies"


def run(args, env=None):
    result = subprocess.run([str(a) for a in args], cwd=ROOT, env=env)
    if result.returncode:
        raise RuntimeError("准备工作台时发生错误，请检查网络与上方提示")


def launch_options(args):
    unknown = set(args) - {'--demo', '--no-open'}
    if unknown:
        raise RuntimeError('不支持的启动参数；仅可使用--demo及--no-open')
    demo = '--demo' in args
    return demo, '--no-open' not in args, 8766 if demo else 8765


def open_when_ready(address):
    for _ in range(30):
        try:
            with urlopen(address + '/api/health', timeout=1) as response:
                if response.status == 200:
                    webbrowser.open(address)
                    return
        except OSError:
            threading.Event().wait(.5)


def main():
    demo, show_browser, port = launch_options(sys.argv[1:])
    address = f'http://127.0.0.1:{port}'
    try:
        with urlopen(address + '/api/health', timeout=1) as response:
            health = json.load(response)
            if health.get('local_only') is True:
                if bool(health.get('demo')) != demo:
                    raise RuntimeError(f'端口{port}运行的是另一种模式，请先停止该服务再启动')
                print("工作台已在运行，正在打开界面。", flush=True)
                if show_browser:
                    webbrowser.open(address)
                return
    except (OSError, ValueError):
        pass
    python = ROOT / ".venv/bin/python"
    if not python.exists():
        run([sys.executable, "-m", "venv", ROOT / ".venv"])
    check = subprocess.run([str(python), "-c", "import fastapi,uvicorn,playwright,pydantic"], capture_output=True)
    if check.returncode:
        print("正在安装本机服务依赖…", flush=True)
        run([python, "-m", "pip", "install", "-r", "requirements.lock.txt"])
    node = shutil.which("node") or str(RUNTIME / "node/bin/node")
    if not Path(node).exists():
        if not (ROOT / "dist/index.html").exists():
            raise RuntimeError("首次构建需要Node.js 22.12或更新版本，请先安装后重新打开")
    else:
        env = {**os.environ, "PATH": f"{Path(node).parent}{os.pathsep}{os.environ.get('PATH','')}"}
        if not (ROOT / "node_modules/vite/bin/vite.js").exists():
            pnpm = shutil.which("pnpm") or str(RUNTIME / "bin/fallback/pnpm")
            if not Path(pnpm).exists():
                raise RuntimeError("缺少前端依赖管理器，请安装pnpm后重新打开")
            run([pnpm, "install", "--frozen-lockfile"], env)
        index = ROOT / "dist/index.html"
        inputs = [*ROOT.glob("src/**/*"), ROOT / "package.json", ROOT / "vite.config.ts", ROOT / "index.html"]
        if not index.exists() or any(p.is_file() and p.stat().st_mtime > index.stat().st_mtime for p in inputs):
            print("正在构建工作台界面…", flush=True)
            run([node, ROOT / "node_modules/typescript/bin/tsc", "--noEmit"], env)
            run([node, ROOT / "node_modules/vite/bin/vite.js", "build"], env)
    print(f'{"演示" if demo else ""}工作台地址：{address}\n关闭此窗口或按 Control+C 停止服务。', flush=True)
    if demo:
        print('演示资料全部虚构，存放于本机临时目录，不连接平台或使用正式登录资料。', flush=True)
    if show_browser:
        threading.Thread(target=open_when_ready, args=(address,), daemon=True).start()
    server_env = {**os.environ, 'WORKBENCH_DEMO': '1' if demo else '0'}
    run([python, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", str(port)], server_env)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError) as error:
        print(f"启动失败：{error}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("工作台已停止")
