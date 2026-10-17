"""The supervisor must request every finalizer the backend contract can produce."""
import ast
from pathlib import Path

from task_worker_api import admission_supervisor
from task_worker_api.enums import TaskType


def test_backend_finalizer_registration_matches_the_task_contract():
    tree = ast.parse(Path(admission_supervisor.__file__).read_text())
    registrations = [ast.literal_eval(node.value) for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.List)
        and any(isinstance(target, ast.Name) and target.id == 'task_types' for target in node.targets)]
    assert len(registrations) == 1
    assert set(registrations[0]) == {task.value for task in TaskType if task.value.startswith('finalize_')}
