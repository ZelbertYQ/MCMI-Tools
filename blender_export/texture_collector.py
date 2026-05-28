import os
import re

from dataclasses import dataclass, field
from pathlib import Path
from typing import List


@dataclass
class Texture:
    hash: str
    path: Path
    filename: str
    relative_path: Path


def get_textures(object_source_folder: Path, exclude_hashes: List[str]):
    textures = {}
    hash_pattern = re.compile(r'(?<![a-f0-9])([a-f0-9]{8})(?![a-f0-9])')
    for texture_path in sorted(object_source_folder.rglob('*')):
        if not texture_path.is_file():
            continue
        if texture_path.suffix.lower() not in {'.dds', '.jpg', '.jpeg', '.png'}:
            continue

        result = hash_pattern.findall(texture_path.name.lower())
        if len(result) == 0:
            continue

        texture_hash = result[-1]

        if exclude_hashes and texture_hash in exclude_hashes:
            continue

        if texture_hash in textures:
            continue

        relative_path = texture_path.relative_to(object_source_folder)
        textures[texture_hash] = Texture(
            hash=texture_hash,
            path=texture_path,
            filename=relative_path.as_posix(),
            relative_path=relative_path,
        )
    return list(textures.values())
