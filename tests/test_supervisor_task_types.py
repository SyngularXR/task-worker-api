"""Both supervisor gates must admit every trusted backend finalizer."""
import ast
from pathlib import Path

import pytest

from task_worker_api import admission_supervisor, docker_supervisor
from task_worker_api.enums import TaskType


@pytest.mark.parametrize("module", [admission_supervisor, docker_supervisor])
def test_backend_finalizer_allowlist_matches_the_task_contract(module):
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    allowlists = [ast.literal_eval(node) for node in ast.walk(tree)
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts
        and all(isinstance(item, ast.Constant) and isinstance(item.value, str)
            and item.value.startswith("finalize_") for item in node.elts)]
    assert len(allowlists) == 1
    assert set(allowlists[0]) == {task.value for task in TaskType if task.value.startswith("finalize_")}
