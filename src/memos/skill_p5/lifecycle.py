"""Immutable lifecycle baseline for the explicit P5 execution profile."""

from pathlib import Path

from memos.skill_lifecycle.observers import memory_subject, tool_subject


def subjects_for_packet(packet, cli_path):
    root = Path(__file__).resolve().parents[1]
    implementations = sorted((root / "skill_adapter").glob("*.py"))
    implementations += sorted((root / "skill_p5").glob("*.py"))
    implementations += [root / "skill_management" / "runtime.py"]
    return [memory_subject(source["snapshot"]) for source in packet["sources"]] + [
        tool_subject(cli_path, implementation_paths=implementations)
    ]
