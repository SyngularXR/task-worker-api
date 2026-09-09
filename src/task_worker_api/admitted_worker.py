"""One supervised worker process: python -m task_worker_api.admitted_worker CONFIG.

CONFIG is operator-owned and read-only. It supplies backend_url, worker_id,
credential_file, journal_file, report_file, work_dir and handlers (task type to
module:function). Handler modules are imported only after start acknowledgement.
"""
import asyncio
from functools import partial
import importlib
import json
from pathlib import Path
import sys

from .claim_journal import ClaimJournal
from .enums import TaskType
from .resources import ClaimResult, AdmissionError
from .resource_protocol import SignedHostReport
from .worker import Worker


async def _invoke(spec, ctx, params):
    module, name = spec.split(":", 1)
    return await getattr(importlib.import_module(module), name)(ctx, params)


async def run(config):
    journal = ClaimJournal(Path(config["journal_file"]))
    pending = journal.pending()
    if not pending or not pending[1] or not pending[1]["claim"]:
        raise AdmissionError("claim_request_unknown")
    claim = ClaimResult.model_validate(pending[1]["claim"])
    credential = Path(config["credential_file"]).read_text().strip()
    worker = Worker(backend_url=config["backend_url"], api_key=credential, worker_id=config["worker_id"],
        work_dir=str(Path(config["work_dir"]).resolve()), handlers={TaskType(kind): partial(_invoke, spec) for kind, spec in config["handlers"].items()},
        foreign_targets=[])

    async def report():
        return SignedHostReport.model_validate_json(Path(config["report_file"]).read_text())

    try:
        await worker.run_admitted_attempt(claim, journal, report)
    finally:
        await worker._client.close()


if __name__ == "__main__":
    asyncio.run(run(json.loads(Path(sys.argv[1]).read_text())))
