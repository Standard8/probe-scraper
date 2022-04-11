from pathlib import Path
from typing import Optional

from google.cloud import storage

from .utils import compile_glob, extract_bucket_and_prefix, path_walk


def pull(src: str, dst: Path):
    bucket_name, prefix = extract_bucket_and_prefix(src)
    prefix_length = len(prefix)
    bucket = storage.Client().get_bucket(bucket_name)
    # TODO verify that this correctly handles prefix being a file
    for blob in bucket.list_blobs(prefix=prefix):
        if blob.name == prefix:
            file_path = dst
        else:
            file_path = dst / blob.name[prefix_length:]
        file_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"Downloading gs://{blob.bucket.name}/{blob.name} to {file_path}")
        blob.download_to_filename(file_path)


def _upload_to_filename(blob, file_path, acl: Optional[str] = None, content_type: Optional[str] = None, content_encoding: Optional[str] = None, cache_control: Optional[str] = None):
    if cache_control is not None:
        blob.cache_control = cache_control
    if content_type is not None:
        blob.content_type = content_type
    if content_encoding is not None:
        blob.content_encoding = content_encoding
    print(f"Uploading {file_path} to gs://{blob.bucket.name}/{blob.name}")
    blob.upload_from_filename(file_path, predefined_acl=acl)


def push(src: Path, dst: str, delete: bool = False, exclude: Optional[str] = None, **kwargs):
    bucket_name, prefix = extract_bucket_and_prefix(dst)
    bucket = storage.Client().get_bucket(bucket_name)
    if src.is_dir():
        prefix_path = Path(prefix)
        keep = set()
        exclude_pattern = compile_glob(exclude)
        for file_path in path_walk(src, exclude_pattern):
            if not file_path.is_dir():
                blob = bucket.blob(str(prefix_path / file_path.relative_to(src)))
                _upload_to_filename(blob, file_path, **kwargs)
                keep.add(blob.name)
        if delete:
            for blob in bucket.list_blobs(prefix=prefix):
                if blob.name not in keep and not exclude_pattern.fullmatch(blob.name):
                    blob.delete()
    else:
        # src is a file
        _upload_to_filename(bucket.blob(prefix), src, **kwargs)
