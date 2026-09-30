#!/usr/bin/env python3
"""中转站 TPM 限流探测 (本地精确计token, 因为该站服务端 usage 恒为1/1不可信)。
用 tiktoken 从英文语料切出精确 token 数的窗口作为 prompt, max_tokens=1 压低输出成本。"""
import asyncio, time, argparse, collections, random, sys
import httpx, tiktoken

BASE = "https://api.appintheloop.com/v1"
KEY  = "sk-dtHTiUaL6WXOskf0YSeKuNH6IlF4JIiIsyWY2GKsFZdildJX"
HEADERS = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
ENC = tiktoken.get_encoding("cl100k_base")

PARA = ("The distribution of computational resources across a heterogeneous cluster requires "
        "careful consideration of network topology, memory bandwidth, and the scheduling policy "
        "applied to incoming workloads. In practice, operators observe that tail latency dominates "
        "the user experience far more than median throughput, which motivates admission control at "
        "the edge of the system rather than deep within the request pipeline. ")
BASE_IDS = ENC.encode(PARA * 400)   # 语料 token 池

def prompt_of(n_tokens):
    """精确 n_tokens 的唯一 prompt(随机窗口+随机前缀, 规避任何缓存)。"""
    win = max(1, n_tokens - 12)
    off = random.randint(0, max(0, len(BASE_IDS) - win - 1))
    body = ENC.decode(BASE_IDS[off:off+win])
    return f"[req-{random.getrandbits(48):012x}] {body}\nReply with exactly: ok"

STATE = {"tokens": 0, "err_shown": 0}

async def one(client, model, n_tokens, out_tokens):
    text = prompt_of(n_tokens)
    ntok = len(ENC.encode(text))
    body = {"model": model, "max_tokens": out_tokens,
            "messages": [{"role": "user", "content": text}]}
    t0 = time.perf_counter()
    try:
        r = await client.post(f"{BASE}/chat/completions", json=body, headers=HEADERS, timeout=300)
        dt = time.perf_counter() - t0
        if r.status_code == 200:
            STATE["tokens"] += ntok
        elif STATE["err_shown"] < 3:
            STATE["err_shown"] += 1
            print(f"    [body {r.status_code}] {r.text[:300]}", flush=True)
        return r.status_code, dt, ntok, r.headers.get("retry-after")
    except Exception as e:
        return f"ERR:{type(e).__name__}", time.perf_counter()-t0, 0, None

def report(results, label, elapsed):
    codes = collections.Counter(str(c) for c,_,_,_ in results)
    ok_tok = sum(x[2] for x in results if str(x[0]) == "200")
    lat = sorted(x[1] for x in results if isinstance(x[1], float))
    ra  = [x[3] for x in results if x[3]]
    print(f"\n=== {label} ===")
    print(f"  requests={len(results)} wall={elapsed:.1f}s  status={dict(codes)}")
    print(f"  成功投递 input tokens={ok_tok:,}  => 实测 ≈ {ok_tok/elapsed*60:,.0f} TPM  ({len(results)/elapsed*60:.0f} RPM)")
    if lat: print(f"  latency p50={lat[len(lat)//2]:.2f}s p95={lat[int(len(lat)*0.95)]:.2f}s max={lat[-1]:.2f}s")
    if ra: print(f"  Retry-After: {collections.Counter(ra)}")
    return {k:v for k,v in codes.items() if not k.startswith('2')}

async def level(client, model, target_tpm, per_req, duration, out_tokens):
    interval = 60.0 / (target_tpm / per_req)
    tasks = []; t0 = time.perf_counter(); i = 0
    while time.perf_counter() - t0 < duration:
        tasks.append(asyncio.create_task(one(client, model, per_req, out_tokens)))
        i += 1
        s = (t0 + i*interval) - time.perf_counter()
        if s > 0: await asyncio.sleep(s)
    res = await asyncio.gather(*tasks)
    return report(list(res), f"TPM target={target_tpm:,}/min ({per_req:,} tok/req, {duration}s)", time.perf_counter()-t0)

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="claude-haiku-4-5")
    ap.add_argument("--levels", default="500000,1500000,4000000")
    ap.add_argument("--per-req", type=int, default=20000)
    ap.add_argument("--duration", type=int, default=30)
    ap.add_argument("--out-tokens", type=int, default=1)
    ap.add_argument("--budget", type=int, default=3_000_000)
    ap.add_argument("--gap", type=float, default=5.0)
    a = ap.parse_args()
    print(f"Model={a.model}  per_req={a.per_req:,} tok  预算护栏={a.budget:,} tokens")
    limits = httpx.Limits(max_connections=300, max_keepalive_connections=300)
    async with httpx.AsyncClient(limits=limits) as client:
        for lv in [int(x) for x in a.levels.split(",")]:
            bad = await level(client, a.model, lv, a.per_req, a.duration, a.out_tokens)
            print(f"  [累计已投递 {STATE['tokens']:,} tokens]")
            if bad:
                print(f"  >>> 出现非2xx {bad} —— 疑似触及 TPM 限流, 停止加压"); break
            if STATE["tokens"] >= a.budget:
                print(f"\n!! 触及预算护栏 {a.budget:,}, 停止"); break
            await asyncio.sleep(a.gap)

asyncio.run(main())
