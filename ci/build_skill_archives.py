"""Build standalone, reproducible archives for the assistant skills."""

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


SKILL_NAMES = ("chaos-jungle-injection", "chaos-jungle-evaluation")
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def build_archives(skills_dir: Path, output_dir: Path, version: str) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    archives = []
    for name in SKILL_NAMES:
        source = skills_dir / name
        if not (source / "SKILL.md").is_file():
            raise FileNotFoundError(f"Missing skill instructions: {source / 'SKILL.md'}")

        entries = sorted(source.rglob("*"))
        for path in entries:
            if path.is_symlink() or any(part.startswith(".") for part in path.relative_to(source).parts):
                raise ValueError(f"Unsafe skill archive entry: {path}")
        files = [path for path in entries if path.is_file()]

        archive_path = output_dir / f"{name}-v{version}.zip"
        with ZipFile(archive_path, "w", compression=ZIP_DEFLATED) as archive:
            for path in files:
                entry = ZipInfo(f"{name}/{path.relative_to(source).as_posix()}", ZIP_TIMESTAMP)
                entry.compress_type = ZIP_DEFLATED
                entry.external_attr = 0o100644 << 16
                archive.writestr(entry, path.read_bytes())
        archives.append(archive_path)
    return archives


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="Verified package version without the v prefix")
    parser.add_argument("--skills-dir", type=Path, default=Path("skills"))
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    args = parser.parse_args()
    for archive in build_archives(args.skills_dir, args.output_dir, args.version):
        print(archive)


if __name__ == "__main__":
    main()
