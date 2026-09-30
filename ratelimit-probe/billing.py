import json, urllib.request, time, random, tiktoken
K="sk-dtHTiUaL6WXOskf0YSeKuNH6IlF4JIiIsyWY2GKsFZdildJX"
ENC=tiktoken.get_encoding("cl100k_base")
PARA=("The distribution of computational resources across a heterogeneous cluster requires careful "
      "consideration of network topology, memory bandwidth, and scheduling policy for workloads. ")
IDS=ENC.encode(PARA*3000)

def usage():
    r=urllib.request.Request("https://api.appintheloop.com/v1/dashboard/billing/usage",
                             headers={"Authorization":f"Bearer {K}"})
    return json.loads(urllib.request.urlopen(r,timeout=30).read())["total_usage"]

def call(ntok, out=1):
    win=max(1,ntok-12); off=random.randint(0,len(IDS)-win-1)
    txt=f"[r-{random.getrandbits(40):010x}] "+ENC.decode(IDS[off:off+win])+"\nReply: ok"
    b={"model":"claude-haiku-4-5","max_tokens":out,"messages":[{"role":"user","content":txt}]}
    rq=urllib.request.Request("https://api.appintheloop.com/v1/chat/completions",
        data=json.dumps(b).encode(),
        headers={"Authorization":f"Bearer {K}","Content-Type":"application/json"})
    urllib.request.urlopen(rq,timeout=180).read()
    return len(ENC.encode(txt))

def trial(name, ntok, n, out=1):
    before=usage(); time.sleep(1)
    tot=sum(call(ntok,out) for _ in range(n))
    time.sleep(6)
    after=usage()
    d=after-before
    print(f"{name}: {n} req x ~{ntok:,} tok (实发 {tot:,} tok)  扣费Δ={d:.4f}  "
          f"每请求={d/n:.4f}  每1k-token={(d/tot*1000) if tot else 0:.6f}")
    return d

print("baseline total_usage =", usage())
trial("A 小请求(20tok)      ", 20, 10)
trial("B 大请求  ", 20000, 10)
trial("C 大请求+长输出", 20000, 5, out=300)
