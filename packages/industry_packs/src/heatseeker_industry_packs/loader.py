"""Pack discovery, loading, and validation (ADR-0005).

A pack is a directory of YAML files under a packs root. Validation is strict and
aggregates every problem before failing, so a pack author sees all errors at once.
"""

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import ValidationError

from heatseeker_industry_packs.schemas import (
    KNOWN_FILES,
    REQUIRED_FILES,
    Manifest,
    StrictModel,
)

# Pack files are hand-edited config; anything huge is a mistake, not a taxonomy.
MAX_FILE_BYTES = 2 * 1024 * 1024


class PackValidationError(Exception):
    """All validation problems for one pack, aggregated."""

    def __init__(self, pack_path: Path, problems: list[str]):
        self.pack_path = pack_path
        self.problems = problems
        summary = "\n  - ".join(problems)
        super().__init__(f"invalid pack at {pack_path}:\n  - {summary}")


@dataclass
class LoadedPack:
    path: Path
    manifest: Manifest
    files: dict[str, StrictModel] = field(default_factory=dict)  # rel-path -> parsed model
    content_hash: str = ""

    @property
    def pack_id(self) -> str:
        return self.manifest.id

    @property
    def version(self) -> str:
        return self.manifest.version


def default_packs_root() -> Path:
    """packages/industry_packs/packs — resolvable in editable installs; env overrides."""
    override = os.environ.get("HEATSEEKER_PACKS_ROOT")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "packs"


def discover_packs(packs_root: Path | None = None) -> list[Path]:
    root = packs_root or default_packs_root()
    if not root.exists():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / "manifest.yaml").exists())


def _iter_yaml_files(pack_path: Path) -> list[str]:
    """Relative POSIX paths of every .yaml/.yml under the pack directory."""
    found = []
    for path in pack_path.rglob("*"):
        if path.suffix.lower() in (".yaml", ".yml") and path.is_file():
            found.append(path.relative_to(pack_path).as_posix())
    return sorted(found)


def load_pack(pack_path: Path) -> LoadedPack:
    """Parse and validate one pack directory. Raises PackValidationError on any problem."""
    pack_path = Path(pack_path)
    problems: list[str] = []
    parsed: dict[str, StrictModel] = {}

    if not pack_path.is_dir():
        raise PackValidationError(pack_path, ["pack directory does not exist"])

    yaml_files = _iter_yaml_files(pack_path)

    for required in REQUIRED_FILES:
        if required not in yaml_files:
            problems.append(f"missing required file: {required}")

    hasher = hashlib.sha256()
    raw_manifest: dict | None = None
    for rel in yaml_files:
        raw = (pack_path / rel).read_bytes()
        hasher.update(rel.encode())
        hasher.update(raw)

        model_cls = KNOWN_FILES.get(rel)
        if model_cls is None:
            allowed = ", ".join(sorted(KNOWN_FILES))
            problems.append(f"unknown pack file '{rel}' (allowed: {allowed})")
            continue
        if len(raw) > MAX_FILE_BYTES:
            problems.append(f"{rel}: file exceeds {MAX_FILE_BYTES // 1024} KiB limit")
            continue
        try:
            data = yaml.safe_load(raw)
        except (yaml.YAMLError, UnicodeDecodeError) as exc:
            problems.append(f"{rel}: parse error: {exc}")
            continue
        if not isinstance(data, dict):
            problems.append(f"{rel}: expected a mapping at top level")
            continue
        if rel == "manifest.yaml":
            raw_manifest = data
        try:
            parsed[rel] = model_cls.model_validate(data)
        except ValidationError as exc:
            for err in exc.errors():
                location = ".".join(str(part) for part in err["loc"])
                problems.append(f"{rel}: {location}: {err['msg']}")

    manifest = parsed.get("manifest.yaml")
    # Checked from raw YAML so the mismatch surfaces even when other manifest
    # fields fail validation.
    declared_id = (raw_manifest or {}).get("id")
    if declared_id and declared_id != pack_path.name:
        problems.append(f"manifest id '{declared_id}' must match directory name '{pack_path.name}'")
    seed_sources = parsed.get("sources/seed_sources.yaml")
    if manifest is not None and seed_sources is not None and seed_sources.pack != manifest.id:
        problems.append(
            f"sources/seed_sources.yaml: pack '{seed_sources.pack}' != manifest id '{manifest.id}'"
        )

    if problems:
        raise PackValidationError(pack_path, problems)

    return LoadedPack(
        path=pack_path,
        manifest=manifest,
        files=parsed,
        content_hash=hasher.hexdigest(),
    )
