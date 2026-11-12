"""The skills release contains both complete, installable skill folders."""

from pathlib import Path
import re
from zipfile import ZipFile

import pytest
import yaml

from ci.build_skill_bundle import SKILL_NAMES, build_bundle


def test_skills_have_portable_metadata_and_references():
    skills_dir = Path(__file__).resolve().parents[1] / "skills"
    for name in SKILL_NAMES:
        skill_file = skills_dir / name / "SKILL.md"
        content = skill_file.read_text()
        assert content.startswith("---\n")
        metadata = yaml.safe_load(content.split("---", 2)[1])
        assert metadata["name"] == name
        assert metadata["description"].strip()
        for target in re.findall(r"\]\(([^)]+)\)", content):
            if "://" not in target and not target.startswith("#"):
                assert (skill_file.parent / target.split("#", 1)[0]).is_file(), target


def test_skill_bundle_is_complete_and_reproducible(tmp_path):
    skills_dir = Path(__file__).resolve().parents[1] / "skills"
    first = build_bundle(skills_dir, tmp_path / "first", "0.1.0a1")
    second = build_bundle(skills_dir, tmp_path / "second", "0.1.0a1")

    assert first.name == "chaos-jungle-skills-v0.1.0a1.zip"
    assert first.read_bytes() == second.read_bytes()
    with ZipFile(first) as archive:
        expected = {
            f"{name}/{path.relative_to(skills_dir / name).as_posix()}"
            for name in SKILL_NAMES
            for path in (skills_dir / name).rglob("*")
            if path.is_file()
        }
        assert set(archive.namelist()) == expected
        for name in SKILL_NAMES:
            assert f"{name}/SKILL.md" in expected
        assert all(archive.read(entry) for entry in expected)


def test_skill_bundle_rejects_symlinks(tmp_path):
    skills_dir = tmp_path / "skills"
    for name in SKILL_NAMES:
        skill_dir = skills_dir / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: example\n---\n")
    (skills_dir / SKILL_NAMES[0] / "linked.md").symlink_to(tmp_path / "private.md")

    with pytest.raises(ValueError, match="Unsafe skill archive entry"):
        build_bundle(skills_dir, tmp_path / "dist", "0.1.0a1")
