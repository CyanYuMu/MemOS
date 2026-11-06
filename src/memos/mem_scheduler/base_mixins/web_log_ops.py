from __future__ import annotations

from memos.context.context import get_current_api_path
from memos.log import get_logger
from memos.mem_scheduler.schemas.message_schemas import ScheduleLogForWebItem


logger = get_logger(__name__)


class BaseSchedulerWebLogMixin:
    def _submit_web_logs(
        self,
        messages: ScheduleLogForWebItem | list[ScheduleLogForWebItem],
        additional_log_info: str | None = None,
    ) -> None:
        if isinstance(messages, ScheduleLogForWebItem):
            messages = [messages]

        for message in messages:
            if self.rabbitmq_config is None:
                return
            try:
                current_api_path = get_current_api_path()
                if current_api_path and not getattr(message, "api_path", None):
                    message.api_path = current_api_path
                logger.info(
                    "[DIAGNOSTIC] base_scheduler._submit_web_logs: enqueue publish %s",
                    message.model_dump_json(indent=2),
                )
                self.rabbitmq_publish_message(message=message.to_dict())
                logger.info(
                    "[DIAGNOSTIC] base_scheduler._submit_web_logs: publish dispatched item_id=%s task_id=%s label=%s",
                    message.item_id,
                    message.task_id,
                    message.label,
                )
            except Exception as e:
                logger.error(
                    "[DIAGNOSTIC] base_scheduler._submit_web_logs failed: %s",
                    e,
                    exc_info=True,
                )

        logger.debug(
            "%s submitted. %s in queue. additional_log_info: %s",
            len(messages),
            self._web_log_message_queue.qsize(),
            additional_log_info,
        )
