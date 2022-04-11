import gzip
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional

from . import s3, gcs
from .utils import path_walk

TEXT_HTML = "text/html"
APPLICATION_JSON = "application/json"
INDEX_HTML = "index.html"


def _get_implementation(remote: str):
    if remote.startswith("s3://"):
        return s3
    elif remote.startswith("gs://"):
        return gcs
    else:
        raise ValueError(
            f"remote path must have scheme like s3:// or gs://, got: {remote!r}"
        )


def remote_storage_pull(src: str, dst: Path, decompress: bool = False):
    if decompress:
        dst_path = Path(dst)
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            remote_storage_pull(src, tmp_path, decompress=False)
            keep = set()
            for f1 in tmp_path.rglob("*"):
                f2 = dst_path / f1.relative_to(tmp_path)
                keep.add(f2)
                if f1.is_dir():
                    if f2.exists() and not f2.is_dir():
                        f2.unlink()
                    f2.mkdir()
                else:
                    with gzip.open(f1, "rb") as f1_:
                        f2.write_bytes(f1_.read())
    else:
        _get_implementation(src).pull(src, dst)


def remote_storage_push(src: Path, dst: str, compress: bool = False, delete: bool = False, exclude: Optional[str] = None):
    impl = _get_implementation(dst)
    if compress:
        if exclude is not None:
            raise NotImplementedError("exclude is not supported while compressing")
        # cloudfront is supposed to automatically gzip objects, but it won't do that
        # if the object size is > 10 megabytes (https://webmasters.stackexchange.com/a/111734)
        # which our files sometimes are. to work around this, as well as to support google
        # cloud storage, we'll gzip the contents into a temporary directory, and upload that
        # with a special content encoding
        with TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            kwargs = {
                "content_encoding": "gzip",
                "cache_control": "max-age=28800",
                "acl": "public-read",
            }
            if src.is_dir():
                for f1 in path_walk(src):
                    if not f1.is_dir():
                        f2 = tmp / f1.relative_to(src)
                        f2.parent.mkdir(parents=True, exist_ok=True)
                        with gzip.open(f2, "wb") as f2_:
                            f2_.write(f1.read_bytes())
                index = tmp / INDEX_HTML
                if index.exists():
                    impl.push(index, f"{dst}/{INDEX_HTML}", content_type=TEXT_HTML, **kwargs)
                    kwargs["exclude"] = INDEX_HTML
                impl.push(tmp, dst, content_type=APPLICATION_JSON, delete=delete, **kwargs)
            else:
                tmp_file = tmp / src.name
                with gzip.open(tmp_file, "wb") as tmp_file_:
                    tmp_file_.write(src.read_bytes())
                kwargs["content_type"] = TEXT_HTML if src.name == INDEX_HTML else APPLICATION_JSON
                impl.push(tmp_file, dst, **kwargs)
    else:
        impl.push(src, dst, delete=delete, exclude=exclude)
