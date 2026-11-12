"""Release archives preserve each complete, installable skill folder."""

from pathlib import Path
from zipfile import ZipFile

import pytest

from ci.build_skill_archives import SKILL_NAMES, build_archives


def test_skill_archives_are_complete_and_reproducible(tmp_path):
    skills_dir = Path(__file__).resolve().parents[1] / "skills"
    first = build_archives(skills_dir, tmp_path / "first", "0.1.0a0")
    second = build_archives(skills_dir, tmp_path / "second", "0.1.0a0")

    assert [path.name for path in first] == [f"{name}-v0.1.0a0.zip" for name in SKILL_NAMES]
    for name, archive_path, repeated_path in zip(SKILL_NAMES, first, second):
        assert archive_path.read_bytes() == repeated_path.read_bytes()
        with ZipFile(archive_path) as archive:
            expected = {
                f"{name}/{path.relative_to(skills_dir / name).as_posix()}"
                for path in (skills_dir / name).rglob("*")
                if path.is_file()
            }
            assert set(archive.namelist()) == expected
            assert f"{name}/SKILL.md" in expected
            assert all(archive.read(entry) for entry in expected)


def test_skill_archive_rejects_symlinks(tmp_path):
    skills_dir = tmp_path / "skills"
    for name in SKILL_NAMES:
        skill_dir = skills_dir / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: example\n---\n")
    (skills_dir / SKILL_NAMES[0] / "linked.md").symlink_to(tmp_path / "private.md")

    with pytest.raises(ValueError, match="Unsafe skill archive entry"):
        build_archives(skills_dir, tmp_path / "dist", "0.1.0a0")
