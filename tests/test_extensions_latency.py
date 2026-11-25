"""How much does the process boundary cost on the message path? (milestone M3)

These tests **measure** and also act as a regression guard with deliberately
generous bounds (they must not fail on a busy machine). The numbers are printed
(run with ``-s``) and written as JSON to the file named by ``SYNTH_BENCH_OUT``
when that variable is set.

What is measured, per call:

* ``rpc_pipe``: a bare JSON-RPC round trip over an in-memory pipe, the protocol
  floor with no process and no Core work;
* ``inbound``: an extension in its own process calls ``interfaces.inbound``; the
  Core validates it and runs it through the (in-memory) pipeline;
* ``outbound``: the Core calls ``RemoteInterface.send_message`` and the extension
  process delivers it: the real reply path, minus the platform itself.

A conversational turn crosses the boundary once in each direction, so
``inbound + outbound`` is the added cost of one turn. For scale: an LLM call takes
hundreds of milliseconds to seconds.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from synth_sdk.rpc import MAX_MESSAGE_BYTES, RpcPeer

from core.extensions.api import register_core_api
from core.extensions.gateway import Gateway, RateLimit
from core.extensions.grants import MemoryGrantStore
from core.extensions.interfaces import (
    InterfacePorts,
    RemoteInterface,
    register_interface_api,
)
from core.extensions.launcher import Launcher
from core.extensions.manifest import InterfaceProvision
from core.extensions.memory_ports import (
    MemoryInterfaceHost,
    MemoryMessaging,
    memory_ports,
)
from core.extensions.sandbox import RuntimeLayout, SandboxPolicy
from core.extensions.supervisor import RestartPolicy, Supervisor
from tests.test_extensions_runtime import Harness
from tests.test_synth_sdk_rpc import PipeWriter

N = 300
RESULTS: dict[str, dict[str, float]] = {}


def summarise(
    name: str, samples_ms: list[float], total_s: float | None = None
) -> dict[str, float]:
    ordered = sorted(samples_ms)

    def pct(p: float) -> float:
        return ordered[min(len(ordered) - 1, int(p * len(ordered)))]

    summary = {
        "n": float(len(ordered)),
        "mean_ms": statistics.fmean(ordered),
        "p50_ms": pct(0.50),
        "p95_ms": pct(0.95),
        "p99_ms": pct(0.99),
        "max_ms": ordered[-1],
    }
    if total_s:
        summary["calls_per_s"] = len(ordered) / total_s
    RESULTS[name] = summary
    print(
        f"[bench] {name:9s} n={len(ordered)} mean={summary['mean_ms']:.2f}ms "
        f"p50={summary['p50_ms']:.2f}ms p95={summary['p95_ms']:.2f}ms "
        f"p99={summary['p99_ms']:.2f}ms max={summary['max_ms']:.2f}ms"
        + (f" ~{summary['calls_per_s']:.0f}/s" if total_s else "")
    )
    return summary


@pytest.fixture(scope="module", autouse=True)
def write_results() -> Any:
    yield
    out = os.environ.get("SYNTH_BENCH_OUT")
    if out:
        Path(out).write_text(
            json.dumps(RESULTS, indent=2, sort_keys=True), encoding="utf-8"
        )


@pytest_asyncio.fixture
async def rig(
    tmp_path: Path,
) -> AsyncIterator[tuple[Harness, MemoryInterfaceHost, MemoryMessaging]]:
    ports = memory_ports()
    grants = MemoryGrantStore()
    # The measurement must not be throttled by the default 100 calls/second limit.
    gateway = Gateway(grants=grants, rate_limit=RateLimit(rate=1e9, burst=1e9))
    register_core_api(gateway, ports)
    host, messaging = MemoryInterfaceHost(), MemoryMessaging()
    register_interface_api(gateway, InterfacePorts(host, messaging))
    layout = RuntimeLayout(tmp_path / "extensions")
    launcher = Launcher(gateway, layout, SandboxPolicy(drop_privileges=False))
    supervisor = Supervisor(
        launcher, RestartPolicy(hello_timeout=5.0, stop_grace=0.5, term_wait=0.3)
    )
    harness = Harness(layout, gateway, grants, ports, launcher, supervisor)
    try:
        yield harness, host, messaging
    finally:
        await supervisor.stop_all()
        await gateway.close_all()


class TestRoundTripCost:
    @pytest.mark.asyncio
    async def test_the_protocol_floor_over_an_in_memory_pipe(self) -> None:
        async def echo(method: str, params: Any, notification: bool) -> Any:
            return params

        reader_a = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES + 1)
        reader_b = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES + 1)
        a = RpcPeer(reader_a, PipeWriter(reader_b), echo, name="a")
        b = RpcPeer(reader_b, PipeWriter(reader_a), echo, name="b")
        tasks = [asyncio.create_task(a.serve()), asyncio.create_task(b.serve())]
        payload = {"text": "x" * 200, "interface_path": "chat/room1/t1"}
        try:
            samples: list[float] = []
            started = time.perf_counter()
            for _ in range(2000):
                t0 = time.perf_counter()
                await a.request("echo", payload)
                samples.append((time.perf_counter() - t0) * 1000)
            summary = summarise("rpc_pipe", samples, time.perf_counter() - started)
            # 64 calls in flight at once also works and is faster per call.
            t0 = time.perf_counter()
            await asyncio.gather(*(a.request("echo", payload) for _ in range(64)))
            assert time.perf_counter() - t0 < 5
        finally:
            await a.close()
            await b.close()
            await asyncio.gather(*tasks)
        assert summary["p95_ms"] < 50

    @pytest.mark.asyncio
    async def test_the_cost_of_a_message_through_a_real_extension_process(
        self, rig: tuple[Harness, MemoryInterfaceHost, MemoryMessaging]
    ) -> None:
        h, host, messaging = rig
        h.install(
            "bench",
            f"""
            import time
            N = {N}
            async def main(ctx):
                async def send(interface_id, payload, origin):
                    return True

                ctx.interfaces.on_send_message(send)
                await ctx.interfaces.register("chat", display_name="Chat")
                samples = []
                for i in range(N):
                    t0 = time.perf_counter()
                    await ctx.interfaces.inbound(
                        "chat", "chat/room1", text="hello " + str(i), sender_id="u1", sender_name="Alice"
                    )
                    samples.append((time.perf_counter() - t0) * 1000)
                await ctx.storage.set("inbound_ms", samples)
                await ctx.stopping.wait()
            """,
            caps=("log", "storage", "interfaces.register", "interfaces.inbound"),
            interfaces=(InterfaceProvision(id="chat", paths=("chat/{chat}",)),),
        )
        await h.supervisor.start("dev/bench")
        await h.wait_for(
            lambda: h.stored("dev/bench", "inbound_ms") is not None, timeout=30
        )
        inbound = h.stored("dev/bench", "inbound_ms")
        assert len(messaging.enqueued) == N  # every message reached the queue
        inbound_summary = summarise("inbound", inbound)

        remote = host.interfaces["chat"]
        assert isinstance(remote, RemoteInterface)
        outbound: list[float] = []
        started = time.perf_counter()
        for _ in range(N):
            t0 = time.perf_counter()
            assert await remote.send_message(
                {"text": "reply", "interface_path": "chat/room1"}
            )
            outbound.append((time.perf_counter() - t0) * 1000)
        outbound_summary = summarise(
            "outbound", outbound, time.perf_counter() - started
        )

        turn = inbound_summary["p50_ms"] + outbound_summary["p50_ms"]
        RESULTS["turn_overhead"] = {"p50_ms": turn}
        print(f"[bench] a conversational turn adds ~{turn:.2f} ms (p50 in + out)")
        # Regression guards: generous on purpose, far above the expected values.
        assert inbound_summary["p95_ms"] < 250
        assert outbound_summary["p95_ms"] < 250
