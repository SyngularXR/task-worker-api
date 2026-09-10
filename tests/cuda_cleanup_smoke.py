"""Run in a disposable container pinned to one GPU; allocates only 1 MiB."""
import asyncio
import json

import torch

from task_worker_api.worker import _cuda_cleanup_with_timeout


async def main():
    tensor = torch.empty(262144, dtype=torch.float32, device="cuda")
    assert not await _cuda_cleanup_with_timeout(5), "live tensor was incorrectly declared clean"
    del tensor
    assert await _cuda_cleanup_with_timeout(5), "unused CUDA storage was not released"
    print(json.dumps({"device": torch.cuda.get_device_name(),
                      "allocated": torch.cuda.memory_allocated(),
                      "reserved": torch.cuda.memory_reserved()}))


if __name__ == "__main__":
    asyncio.run(main())
