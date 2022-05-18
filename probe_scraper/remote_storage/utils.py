import fnmatch
import os.path
import re
from pathlib import Path
from typing import Generator, Optional, Tuple

UNMATCHABLE_PATTERN = re.compile("(?!)")


def compile_glob(glob: Optional[str]) -> re.Pattern:
    return re.compile(fnmatch.translate(glob)) if glob else UNMATCHABLE_PATTERN


def extract_bucket_and_prefix(bucket_path: str) -> Tuple[str, str]:
    bucket, prefix = bucket_path.split("://", 1)[-1].split("/", 1)
    return bucket, prefix


def path_walk(
    base_path: Path, exclude: re.Pattern = UNMATCHABLE_PATTERN
) -> Generator[Path, None, None]:
    for path in base_path.iterdir():
        # add trailing path separator to correctly exclude directories for patterns like "*.git/*"
        if any(map(exclude.fullmatch, [f"{path}", f"{path}{os.path.sep}"])):
            continue
        yield path
        if path.is_dir():
            yield from path_walk(path, exclude)
