#!/usr/bin/env python3
"""���转站限流探测：并发(concurrency) + 速率(RPM) 两个维度。
用最便宜的请求 (haiku, max_tokens=1) 探测，检测 429 / 5xx 出现的阈值。"""
import asyncio, time, argparse, collections, json
import httpx

BASE = "https://api.appintheloop.com/v1"
KEY  = "sk-dtHTiUaL6WXOskf0YSeKuNH6IlF4JIiIsyWY2GKsFZdildJX"
MODEL_DEFAULT = "claude-haiku-4-5"

HEADERS = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

def payload(model):
    return {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}

async def one(client, model):
    t0 = time.perf_counter()
    try:
        r = await client.post(f"{BASE}/chat/completions", json=payload(model),
                              headers=HEADERS, timeout=60)
        dt = time.perf_counter() - t0
        ra = r.headers.get("retry-after")
        return r.status_code, dt, ra
    except Exception as e:
        return f"ERR:{type(e).__name__}", time.perf_counter() - t0, None

def summarize(results, label, elapsed):
    codes = collections.Counter(str(c) for c, _, _ in results)
    lat = [d for _, d, _ in results if isinstance(d, float)]
    ra = [x for _, _, x in results if x]
    lat.sort()
    n = len(lat)
    p50 = lat[n//2] if n else 0
    p95 = lat[int(n*0.95)] if n else 0
    print(f"\n=== {label} ===")
    print(f"  requests={len(results)}  wall={elapsed:.2f}s  achieved≈{len(results)/elapsed:.1f} req/s ({len(results)/elapsed*60:.0f}/min)")
    print(f"  status: {dict(codes)}")
    if lat: print(f"  latency p50={p50:.2f}s p95={p95:.2f}s max={max(lat):.2f}s")
    if ra:  print(f"  Retry-After seen: {collections.Counter(ra)}")
    limited = sum(v for k, v in codes.items() if k.startswith('4') or k.startswith('5') or k.startswith('ERR'))
    return codes, limited

async def concurrency_burst(model, n):
    """同时打出 n 个请求，测并发上限。"""
    limits = httpx.Limits(max_connections=n+10, max_keepalive_connections=n+10)
    async with httpx.AsyncClient(limits=limits) as client:
        t0 = time.perf_counter()
        results = await asyncio.gather(*[one(client, model) for _ in range(n)])
        return summarize(list(results), f"CONCURRENCY burst n={n}", time.perf_counter()-t0)

async def rpm_test(model, target_rpm, duration):
    """按固定速率持续发 duration 秒，测 RPM 限制。"""
    interval = 60.0 / target_rpm
    limits = httpx.Limits(max_connections=500, max_keepalive_connections=500)
    async with httpx.AsyncClient(limits=limits) as client:
        tasks = []
        t0 = time.perf_counter()
        i = 0
        while time.perf_counter() - t0 < duration:
            tasks.append(asyncio.create_task(one(client, model)))
            i += 1
            nxt = t0 + i * interval
            sleep = nxt - time.perf_counter()
            if sleep > 0:
                await asyncio.sleep(sleep)
        results = await asyncio.gather(*tasks)
        return summarize(list(results), f"RPM test target={target_rpm}/min for {duration}s", time.perf_counter()-t0)

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["conc","rpm","both"], default="both")
    ap.add_argument("--model", default=MODEL_DEFAULT)
    ap.add_argument("--conc-steps", default="5,10,20,40,60,80")
    ap.add_argument("--rpm", type=int, default=120)
    ap.add_argument("--duration", type=int, default=15)
    ap.add_argument("--gap", type=float, default=3.0, help="并发step之间间隔秒")
    a = ap.parse_args()
    print(f"Model={a.model}  Base={BASE}")

    if a.mode in ("conc","both"):
        for step in [int(x) for x in a.conc_steps.split(",")]:
            codes, limited = await concurrency_burst(a.model, step)
            if limited:
                print(f"  >>> 在并发 {step} 出现非200 ({limited}个)，可能触及并发/速率上限")
            await asyncio.sleep(a.gap)

    if a.mode in ("rpm","both"):
        await rpm_test(a.model, a.rpm, a.duration)

asyncio.run(main())
