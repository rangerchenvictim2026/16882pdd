"""按明确的公开范围制作源码包；不复制数据库、登录资料或内部记录。"""
import hashlib
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREFIX = '16882pdd-0.1.0'
ROOT_FILES = {'README.md', 'LICENSE', 'CHANGELOG.md', 'THIRD_PARTY_NOTICES.md', '.gitignore',
              'package.json', 'pnpm-lock.yaml', 'pnpm-workspace.yaml', 'requirements.txt', 'requirements.lock.txt',
              'index.html', 'tsconfig.json', 'vite.config.ts', '启动工作台.command', '启动演示.command'}
PUBLIC_DOCS = {'docs/用户指南.md', 'docs/登录与隐私.md', 'docs/开发指南.md', 'docs/已知限制.md', 'docs/架构说明.md', 'docs/开源参考.md'}
DIRECTORIES = {'backend': {'.py'}, 'scripts': {'.py'}, 'tests': {'.py'}, 'src': {'.tsx', '.ts', '.css'},
               'public/demo-assets': {'.svg'}, 'docs/images': {'.png'}}


def ensure_plain_path(root, path):
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f'公开范围包含符号链接，已停止：{current.relative_to(root)}')
    if not path.resolve().is_relative_to(root):
        raise ValueError(f'公开文件超出源码目录，已停止：{path.relative_to(root)}')


def public_files(root):
    files = []
    for name in sorted(ROOT_FILES | PUBLIC_DOCS):
        path = root / name
        ensure_plain_path(root, path)
        if path.is_file():
            files.append(path)
    for directory, suffixes in DIRECTORIES.items():
        parent = root / directory
        ensure_plain_path(root, parent)
        if not parent.exists():
            continue
        for path in parent.rglob('*'):
            ensure_plain_path(root, path)
            if '__pycache__' in path.parts or any(part.startswith('.') for part in path.relative_to(parent).parts):
                continue
            if path.is_file() and path.suffix in suffixes:
                files.append(path)
    return sorted(set(files), key=lambda path: path.relative_to(root).as_posix())


def build_package(root, target):
    root, target = Path(root).resolve(), Path(target)
    for name in ['README.md', 'LICENSE', 'package.json', 'requirements.lock.txt', 'backend/main.py', 'scripts/start.py']:
        ensure_plain_path(root, root / name)
        if not (root / name).is_file():
            raise ValueError(f'缺少公开包必要文件：{name}')
    files = public_files(root)
    manifest = [{'path': path.relative_to(root).as_posix(), 'size': path.stat().st_size,
                 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()} for path in files]
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, f'{PREFIX}/{path.relative_to(root).as_posix()}')
        archive.writestr(f'{PREFIX}/发布文件清单.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


if __name__ == '__main__':
    try:
        target = ROOT / 'release' / '16882pdd-v0.1.0-source.zip'
        manifest = build_package(ROOT, target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        target.with_suffix('.zip.sha256').write_text(f'{digest}  {target.name}\n', encoding='utf-8')
        print(f'源码包已生成：release/{target.name}，共{len(manifest)}个文件')
        print('已排除data目录、登录资料、内部进度、原始截图、依赖环境及构建产物；仍需人工审阅新增源码。')
    except (OSError, ValueError) as error:
        print(f'打包失败：{error}', file=sys.stderr)
        sys.exit(1)
