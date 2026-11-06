# src/memos/mem_scheduler/utils/metrics.py


from prometheus_client import Counter, Gauge, Summary


# --- Metric Definitions ---

TASKS_ENQUEUED_TOTAL = Counter(
    "memos_scheduler_tasks_enqueued_total",
    "Total number of tasks enqueued",
    ["user_id", "task_type"],
)

TASKS_DEQUEUED_TOTAL = Counter(
    "memos_scheduler_tasks_dequeued_total",
    "Total number of tasks dequeued",
    ["user_id", "task_type"],
)

TASK_DURATION_SECONDS = Summary(
    "memos_scheduler_task_duration_seconds",
    "Task processing duration in seconds",
    ["user_id", "task_type"],
)

TASK_WAIT_DURATION_SECONDS = Summary(
    "memos_scheduler_task_wait_duration_seconds",
    "Task waiting duration in seconds",
    ["user_id", "task_type"],
)

TASKS_FAILED_TOTAL = Counter(
    "memos_scheduler_tasks_failed_total",
    "Total number of failed tasks",
    ["user_id", "task_type", "error_type"],
)

TASKS_COMPLETED_TOTAL = Counter(
    "memos_scheduler_tasks_completed_total",
    "Total number of successfully completed tasks",
    ["user_id", "task_type"],
)

QUEUE_LENGTH = Gauge(
    "memos_scheduler_queue_length", "Current length of the task queue", ["user_id"]
)


# --- Instrumentation Functions ---


def task_enqueued(user_id: str, task_type: str, count: int = 1):
    TASKS_ENQUEUED_TOTAL.labels(user_id=user_id, task_type=task_type).inc(count)


def task_dequeued(user_id: str, task_type: str, count: int = 1):
    TASKS_DEQUEUED_TOTAL.labels(user_id=user_id, task_type=task_type).inc(count)


def observe_task_duration(duration: float, user_id: str, task_type: str):
    TASK_DURATION_SECONDS.labels(user_id=user_id, task_type=task_type).observe(duration)


def observe_task_wait_duration(duration: float, user_id: str, task_type: str):
    TASK_WAIT_DURATION_SECONDS.labels(user_id=user_id, task_type=task_type).observe(duration)


def task_failed(user_id: str, task_type: str, error_type: str):
    TASKS_FAILED_TOTAL.labels(user_id=user_id, task_type=task_type, error_type=error_type).inc()


def task_completed(user_id: str, task_type: str, count: int = 1):
    TASKS_COMPLETED_TOTAL.labels(user_id=user_id, task_type=task_type).inc(count)


def update_queue_length(length: int, user_id: str):
    QUEUE_LENGTH.labels(user_id=user_id).set(length)


# --- TimingSpan Context Manager ---
