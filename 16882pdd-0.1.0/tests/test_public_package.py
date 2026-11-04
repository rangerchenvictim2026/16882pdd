"""源码包只接纳明确的公开范围，防止带入本机资料或链接目标。"""
import json
import zipfile
from pathlib import Path

import pytest


def source_tree(root):
    for name in ['README.md', 'LICENSE', 'package.json', 'requirements.lock.txt', 'backend/main.py', 'scripts/start.py',
                 'src/main.tsx', 'public/demo-assets/bowl.svg', 'docs/用户指南.md', 'docs/images/demo-overview.png']:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('用于测试打包范围的占位资料', encoding='utf-8')
    return root


def test_source_package_excludes_local_records_credentials_and_cache(tmp_path):
    from scripts.package_demo import build_package
    root = source_tree(tmp_path / 'source')
    for name in ['data/workbench.sqlite3', 'data/pdd-chrome-profile/Cookies', '.env', '.git/config',
                 '.venv/私有资料', 'backend/__pycache__/main.pyc', 'docs/进度/本机记录.md',
                 'docs/归档/截图/登录页.jpg', 'docs/截图/尚未审阅.png', 'docs/未知文档.md']:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('不应进入源码包的本机占位资料', encoding='utf-8')
    target = tmp_path / '16882pdd.zip'
    manifest = build_package(root, target)
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
        assert '16882pdd-0.1.0/backend/main.py' in names
        assert '16882pdd-0.1.0/docs/images/demo-overview.png' in names
        assert not any(any(word in name for word in ['/data/', '/.env', '/.git/', '/.venv/', '__pycache__', '/进度/', '/归档/', '/截图/']) for name in names)
        stored = json.loads(archive.read('16882pdd-0.1.0/发布文件清单.json'))
        assert stored == manifest and all(len(row['sha256']) == 64 for row in manifest)


def test_source_package_refuses_symlinks_and_missing_license(tmp_path):
    from scripts.package_demo import build_package
    root = source_tree(tmp_path / 'source')
    private = tmp_path / '本机资料.py'
    private.write_text('不应读取的内容', encoding='utf-8')
    (root / 'backend' / 'linked.py').symlink_to(private)
    with pytest.raises(ValueError, match='符号链接'):
        build_package(root, tmp_path / '不应生成.zip')
    (root / 'backend' / 'linked.py').unlink()
    (root / 'LICENSE').unlink()
    with pytest.raises(ValueError, match='LICENSE'):
        build_package(root, tmp_path / '不应生成.zip')


@pytest.mark.parametrize('ancestor', ['docs', 'public'])
def test_source_package_refuses_symlinked_ancestor_directories(tmp_path, ancestor):
    from scripts.package_demo import build_package
    root = source_tree(tmp_path / 'source')
    outside = tmp_path / f'非公开的{ancestor}'
    (root / ancestor).rename(outside)
    (root / ancestor).symlink_to(outside, target_is_directory=True)
    target = tmp_path / '不应生成.zip'
    with pytest.raises(ValueError, match='符号链接'):
        build_package(root, target)
    assert not target.exists()
