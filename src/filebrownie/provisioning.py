"""Versioned model provisioning. Only the setup service downloads; processing only verifies.

Files are pinned by URL, size and SHA-256 in `models_manifest.json`. Processing services never
download (D38, D40): a missing or altered file is a setup error with instructions.
"""

import hashlib
import json
import urllib.request
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path

COMPONENTS = ("tesseract", "vision")
SETUP_HINT = "run: docker compose --profile setup run --rm model-setup"
CHUNK = 1024 * 1024


class SetupError(Exception):
    """Fixed safe codes only."""


def manifest() -> dict:
    return json.loads(files("filebrownie").joinpath("models_manifest.json").read_text())


def component_files(component: str) -> list[dict]:
    try:
        return manifest()[component]["files"]
    except KeyError:
        raise SetupError("UNKNOWN_MODEL_COMPONENT") from None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def verify(models_dir: Path, component: str, deep: bool = True) -> list[str]:
    """Relative paths that are missing or do not match their pinned size and checksum.

    `deep=False` checks existence and size only, for multi-gigabyte weights at run time;
    setup always verifies checksums.
    """
    problems = []
    for item in component_files(component):
        path = models_dir / item["path"]
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size != item["size"]:
                problems.append(item["path"])
            elif deep and _sha256(path) != item["sha256"]:
                problems.append(item["path"])
        except OSError:
            problems.append(item["path"])
    return problems


def require(models_dir: Path, component: str) -> None:
    """Fail closed with setup instructions; never fetches anything."""
    deep = manifest()[component].get("runtime_check") != "size"
    if verify(models_dir, component, deep):
        raise SetupError(f"MODELS_NOT_PROVISIONED:{component}: {SETUP_HINT}")


Opener = Callable[[str], object]


def provision(models_dir: Path, component: str, opener: Opener | None = None) -> list[str]:
    """Download missing or altered files. Needs the network-enabled setup service."""
    opener = opener or (lambda url: urllib.request.urlopen(url, timeout=60))  # noqa: S310
    changed = []
    bad = set(verify(models_dir, component))
    for item in component_files(component):
        if item["path"] not in bad:
            continue
        if not item["url"].startswith("https://"):
            raise SetupError("INSECURE_MODEL_URL")
        target = models_dir / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        digest, size = hashlib.sha256(), 0
        try:
            with opener(item["url"]) as response, partial.open("wb") as out:
                while chunk := response.read(CHUNK):
                    size += len(chunk)
                    if size > item["size"]:
                        raise SetupError("DOWNLOAD_SIZE_MISMATCH")
                    digest.update(chunk)
                    out.write(chunk)
            if size != item["size"] or digest.hexdigest() != item["sha256"]:
                raise SetupError("DOWNLOAD_CHECKSUM_MISMATCH")
            partial.replace(target)
        except SetupError:
            partial.unlink(missing_ok=True)
            raise
        except OSError:
            partial.unlink(missing_ok=True)
            raise SetupError("MODEL_DOWNLOAD_FAILED") from None
        changed.append(item["path"])
    return changed


def component_version(component: str) -> str:
    """Stable identity of the pinned files, used in step-cache versions."""
    digest = hashlib.sha256(
        json.dumps([(item["path"], item["sha256"]) for item in component_files(component)]).encode()
    )
    return digest.hexdigest()[:16]
