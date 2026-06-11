"""Task-success benchmark — context adequacy at a matched token budget."""

from .run import run_task_success
from .tasks import Task, get_tasks, score_response

__all__ = ["Task", "get_tasks", "run_task_success", "score_response"]
