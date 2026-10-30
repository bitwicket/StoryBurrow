from pathlib import Path
import shutil

PAYLOAD_NAMES = ('upload.zip', 'folder', 'parts')


def prune_completed_payload(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir() or root.parent.name != 'import-staging':
        raise ValueError('Unexpected upload session directory')
    for name in PAYLOAD_NAMES:
        item = root / name
        if item.is_symlink():
            # Never follow upload symlinks.
            item.unlink()
        elif item.is_dir():
            shutil.rmtree(item)
        elif item.is_file():
            item.unlink()
