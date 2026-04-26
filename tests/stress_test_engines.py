#!/usr/bin/env python3
"""
Stress test for Synth engines with long prompts and ramping concurrency.
Tests:
- 10 long prompts (3-4 chunks) to selenium-llm-engine (Gemini)
- 10 long prompts to openrouter (ChatGPT)
- 5 random prompts to other engines

Prompts are sent with decreasing delay (ramp-up):
- First prompts: high delay between them
- Last 3 prompts: sent simultaneously to test queue handling
"""

import asyncio
import json
import time
import statistics
from typing import Optional

OLLAMA_URL = "http://localhost:11435"
LONG_PROMPT = """
Scrivi una storia dettagliata di almeno 8000 parole su un viaggio attraverso un mondo 
fantasy. La storia deve includere:
- Un protagonista con un backstory complesso
- Un mondo con geography, culture e storia specifiche
- Un conflitto principale con antagonismi motivati
- Molteplici personaggi secondari con archi narrativi propri
- Dialoghi naturali che rivelano personalità e motivazioni
- Descrizioni evocative di ambienti, battaglie e momenti emotivi
-Una struttura narrativa con climax e risoluzione
- Tensione drammatica, momenti di leggerezza, e colpi di scena
- Un tema centrale che emerge organicamente dalla trama

Il viaggio deve attraversare almeno cinque regioni distincte, ciascuna con la propria 
atmosfera e cultura. I personaggi devono affrontare prove che mettono alla prova non 
solo le loro abilità ma anche le loro credenze e relazioni. Il mondo deve avere regole 
magiche specifiche e una storia antica che influenza gli eventi attuali.

Scrivi in italiano, con prosa letteraria ma accessibile. Usa dialogo, descrizione e 
azione in modo bilanciato. La storia deve essere coinvolgente e memorabile.
"""

def generate_long_prompt(variation: int) -> str:
    """Generate a long prompt with variation for testing."""
    return f"{LONG_PROMPT}\n\n[Variazione {variation}: usa un genere diverso - fantasy urbano, high fantasy, dark fantasy, ecc.]"

def generate_short_prompt(variation: int) -> str:
    """Generate a short random prompt."""
    prompts = [
        f"Cosa pensi dell'AI? (test {variation})",
        f"Descrivi il significato della vita in 100 parole. (test {variation})",
        f"Scrivi una poesia sulla tecnologia. (test {variation})",
        f"Qual è la tua opinione sulla creatività? (test {variation})",
        f"Spiega come funziona un algoritmo di machine learning in parole semplici. (test {variation})",
    ]
    return prompts[variation % len(prompts)]

async def send_prompt(engine: str, prompt: str, timeout: int = 300) -> dict:
    """Send a single prompt and return timing info."""
    import urllib.request
    import urllib.error
    
    start = time.time()
    try:
        data = json.dumps({
            "model": engine,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False
        }).encode()
        
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/chat",
            data=data,
            headers={"Content-Type": "application/json"}
        )
        
        response = await asyncio.wait_for(
            asyncio.to_thread(lambda: urllib.request.urlopen(req, timeout=timeout).read().decode()),
            timeout=timeout + 30
        )
        
        elapsed = time.time() - start
        result = json.loads(response)
        response_text = result.get("message", {}).get("content", "")[:200]
        return {
            "engine": engine,
            "elapsed": elapsed,
            "success": True,
            "response": response_text,
            "error": None
        }
    except asyncio.TimeoutError:
        elapsed = time.time() - start
        return {
            "engine": engine,
            "elapsed": elapsed,
            "success": False,
            "response": "",
            "error": "Timeout"
        }
    except Exception as e:
        elapsed = time.time() - start
        return {
            "engine": engine,
            "elapsed": elapsed,
            "success": False,
            "response": "",
            "error": str(e)
        }

async def run_test(engine: str, prompts: list[str], delays: list[float], label: str) -> list[dict]:
    """Run test with ramp-up delays."""
    results = []
    print(f"\n{'='*60}")
    print(f"Testing {engine} - {label}")
    print(f"{'='*60}")
    
    tasks = []
    for i, (prompt, delay) in enumerate(zip(prompts, delays)):
        # Calculate actual delay - decrease from high to zero
        if i < len(delays):
            await asyncio.sleep(delay)
        
        print(f"  [{i+1}/{len(prompts)}] Sending prompt ({len(prompt)} chars)...", end="", flush=True)
        task = asyncio.create_task(send_prompt(engine, prompt))
        tasks.append((i, task))
    
    # Wait for all to complete
    for i, task in tasks:
        result = await task
        results.append(result)
        status = "✓" if result["success"] else "✗"
        print(f" {status} {result['elapsed']:.1f}s" + (f" - {result['error']}" if not result["success"] else ""))
    
    return results

async def main():
    print("=" * 70)
    print("SYNTH ENGINES STRESS TEST")
    print("=" * 70)
    print(f"Target: {OLLAMA_URL}")
    print(f"Started: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Ramp-up delays: start high, decrease to 0 for last 3
    # 17 prompts total = 10 (Gemini) + 10 (OpenRouter) - 3 overlap for simultaneous
    # Actually let's do separate runs to be clear
    
    all_results = {}
    
    # ============================================================
    # TEST 1: 10 long prompts to selenium-llm-engine (Gemini)
    # ============================================================
    print("\n" + "="*70)
    print("TEST 1: Selenium-llm-engine (Gemini Web) - 10 long prompts")
    print("="*70)
    
    gemini_prompts = [generate_long_prompt(i) for i in range(10)]
    # Ramp-up delays: first 7 with decreasing delay, last 3 simultaneous
    gemini_delays = [30, 25, 20, 15, 10, 8, 5, 0, 0, 0]
    
    gemini_results = await run_test("selenium-llm-engine", gemini_prompts, gemini_delays, "Gemini Web")
    all_results["selenium-llm-engine"] = gemini_results
    
    # ============================================================
    # TEST 2: 10 long prompts to openrouter (ChatGPT)
    # ============================================================
    print("\n" + "="*70)
    print("TEST 2: OpenRouter (ChatGPT) - 10 long prompts")
    print("="*70)
    
    openrouter_prompts = [generate_long_prompt(i + 10) for i in range(10)]
    openrouter_delays = [30, 25, 20, 15, 10, 8, 5, 0, 0, 0]
    
    openrouter_results = await run_test("openrouter", openrouter_prompts, openrouter_delays, "OpenRouter (ChatGPT)")
    all_results["openrouter"] = openrouter_results
    
    # ============================================================
    # TEST 3: 5 random prompts to other engines
    # ============================================================
    print("\n" + "="*70)
    print("TEST 3: Other engines - 5 random prompts each")
    print("="*70)
    
    other_engines = ["anthropic", "gemini_api", "openapi"]
    
    for engine in other_engines:
        print(f"\n--- Testing {engine} ---")
        short_prompts = [generate_short_prompt(i) for i in range(5)]
        # Small delays to not overwhelm
        delays = [5, 3, 2, 1, 0]
        
        results = await run_test(engine, short_prompts, delays, f"{engine} short prompts")
        all_results[engine] = results
    
    # ============================================================
    # REPORT
    # ============================================================
    print("\n" + "="*70)
    print("RESULTS REPORT")
    print("="*70)
    
    for engine, results in all_results.items():
        successes = [r for r in results if r["success"]]
        failures = [r for r in results if not r["success"]]
        
        print(f"\n--- {engine} ---")
        print(f"  Total: {len(results)} | Success: {len(successes)} | Failed: {len(failures)}")
        
        if successes:
            times = [r["elapsed"] for r in successes]
            print(f"  Avg time: {statistics.mean(times):.1f}s")
            print(f"  Min/Max: {min(times):.1f}s / {max(times):.1f}s")
            print(f"  Median: {statistics.median(times):.1f}s")
            if len(times) > 1:
                print(f"  Stdev: {statistics.stdev(times):.1f}s")
        
        if failures:
            print(f"  FAILURES:")
            for f in failures:
                print(f"    - Prompt {results.index(f)+1}: {f['error']} ({f['elapsed']:.1f}s)")
        
        # Show timing per prompt
        print(f"  Per-prompt times:")
        for r in results:
            status = "✓" if r["success"] else "✗"
            print(f"    [{status}] {r['elapsed']:.1f}s" + (f" - {r['error']}" if r['error'] else ""))
    
    print("\n" + "="*70)
    print("SUMMARY")
    print("="*70)
    
    total_success = sum(1 for r in sum(all_results.values(), []) if r["success"])
    total_fail = sum(1 for r in sum(all_results.values(), []) if not r["success"])
    total = total_success + total_fail
    
    print(f"Total: {total} | Success: {total_success} ({100*total_success/total:.1f}%) | Failed: {total_fail}")
    print(f"Finished: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Detailed timing comparison
    print("\n" + "="*70)
    print("ENGINE COMPARISON (average response time)")
    print("="*70)
    
    timing_summary = {}
    for engine, results in all_results.items():
        successes = [r for r in results if r["success"]]
        if successes:
            timing_summary[engine] = statistics.mean([r["elapsed"] for r in successes])
    
    for engine, avg_time in sorted(timing_summary.items(), key=lambda x: x[1]):
        print(f"  {engine}: {avg_time:.1f}s")

if __name__ == "__main__":
    asyncio.run(main())