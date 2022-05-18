from pathlib import Path
from subprocess import check_call


def _sync(src: str, dst: str, **kwargs):
    check_call(
        [
            "aws",
            "s3",
            "sync",
            src,
            dst,
        ]
        + [
            arg if value is True else f"{arg}={value}"
            for key, value in kwargs.items()
            for arg in [f"--{key.replace('_', '-')}"]
            if value is not None and value is not False
        ]
    )


def pull(src: str, dst: Path, **kwargs):
    _sync(src, str(dst), **kwargs)


def push(src: Path, dst: str, **kwargs):
    _sync(str(src), dst, **kwargs)
