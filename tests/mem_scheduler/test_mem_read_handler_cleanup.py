"""fine extraction 失败时不能删除原始 fast memory。"""

from types import SimpleNamespace
from unittest.mock import Mock

from memos.mem_scheduler.task_schedule_modules.handlers.mem_read_handler import (
    MemReadMessageHandler,
)


def test_failed_fine_extraction_keeps_raw_memories():
    reader = SimpleNamespace(
        fine_transfer_simple_mem=Mock(side_effect=RuntimeError("llm timeout")),
        save_rawfile=False,
        graph_db=None,
        memory_version_switch="off",
    )
    text_mem = Mock()
    text_mem.get.return_value = SimpleNamespace(metadata=SimpleNamespace(background=""))

    handler = MemReadMessageHandler(SimpleNamespace(get_mem_reader=lambda: reader))
    handler._process_memories_with_reader(
        mem_ids=["raw-1"],
        user_id="user-1",
        mem_cube_id="cube-1",
        text_mem=text_mem,
        user_name="user-1",
    )

    # 抽取失败时唯一副本必须保留; 清理与刷新都不能发生。
    text_mem.delete.assert_not_called()
    text_mem.soft_delete.assert_not_called()
    text_mem.memory_manager.remove_and_refresh_memory.assert_not_called()


def test_empty_refinement_still_cleans_up_raw_memories():
    """抽取成功但没有产出时，仍按原行为清理原始记忆。"""
    reader = SimpleNamespace(
        fine_transfer_simple_mem=Mock(return_value=[]),
        save_rawfile=False,
        graph_db=None,
        memory_version_switch="off",
    )
    text_mem = Mock()
    text_mem.get.return_value = SimpleNamespace(metadata=SimpleNamespace(background=""))

    handler = MemReadMessageHandler(SimpleNamespace(get_mem_reader=lambda: reader))
    handler._process_memories_with_reader(
        mem_ids=["raw-1"],
        user_id="user-1",
        mem_cube_id="cube-1",
        text_mem=text_mem,
        user_name="user-1",
    )

    text_mem.delete.assert_called_once_with(["raw-1"], user_name="user-1")
