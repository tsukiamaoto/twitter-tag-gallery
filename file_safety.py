"""Path boundaries and content identities shared by scan/import/move services."""
import hashlib
import os
from pathlib import Path


class UnsafePath(ValueError):
    pass


def inside(root, path, *, allow_root=False):
    root, path = Path(root).resolve(), Path(path).resolve()
    if path == root:
        if allow_root:
            return path
        raise UnsafePath("目標不能是圖庫根目錄")
    if root not in path.parents:
        raise UnsafePath("路徑必須位於圖庫內")
    return path


def gallery_path(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or relative.drive or ".." in relative.parts:
        raise UnsafePath("不允許絕對路徑或上層路徑")
    return inside(root, Path(root) / relative)


def validate_source(root, source):
    root, source = Path(root).resolve(), Path(source).resolve()
    if root == source or root in source.parents or source in root.parents:
        raise UnsafePath("來源與圖庫不可互相包含；請選擇圖庫外的獨立資料夾")
    if source.name.casefold() == ".tag_gallery":
        raise UnsafePath("不可匯入圖庫的系統資料夾")
    return source


def file_identity(stat):
    if not stat.st_ino:
        return None
    birth = f":{stat.st_ctime_ns}" if os.name=="nt" else ""
    return f"{stat.st_dev}:{stat.st_ino}{birth}"


def content_hash(path):
    from background_work import media_io
    path = Path(path)
    before = path.stat()
    digest = hashlib.sha256()
    with media_io(background=True), path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise OSError("檔案在計算雜湊時變動，稍後重新掃描")
    return digest.hexdigest()


def prune_directories(root, names):
    """Do not follow directory links/junctions or gallery metadata."""
    return [name for name in names if not name.startswith(".")
            and not (Path(root) / name).is_symlink()
            and not os.path.islink(Path(root) / name)
            and Path(root).resolve() in (Path(root) / name).resolve().parents]
