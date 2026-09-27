"""Cross-process lock for a selected source-local Message."""
from contextlib import contextmanager
import os


@contextmanager
def import_lock(directory, id):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{id}.lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ValueError(f"Message {id} 正在导入；异常退出后请确认无导入进程再删除 {id}.lock") from None
    try:
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)
