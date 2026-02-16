import asyncio
import pytest

from cortex.selenium_engine.selenium_llm_base import SeleniumLLMBase


@pytest.mark.asyncio
async def test_selenium_queue_serialization():
    results = []

    class Dummy(SeleniumLLMBase):
        pass

    inst = Dummy()

    async def task_fn(n, delay=0.05):
        results.append(("start", n))
        await asyncio.sleep(delay)
        results.append(("end", n))
        return n

    # Enqueue two tasks concurrently
    t1 = asyncio.create_task(inst.enqueue_selenium_task(task_fn, 1, delay=0.05))
    t2 = asyncio.create_task(inst.enqueue_selenium_task(task_fn, 2, delay=0.01))

    r1 = await t1
    r2 = await t2

    assert r1 == 1 and r2 == 2

    # Ensure serialization: first 'start' for 1 must occur before 'start' for 2
    starts = [r for r in results if r[0] == "start"]
    assert starts[0][1] == 1
    assert starts[1][1] == 2

    # Ensure ends are in the same overall order
    assert results.index(("end", 1)) < results.index(("end", 2))
