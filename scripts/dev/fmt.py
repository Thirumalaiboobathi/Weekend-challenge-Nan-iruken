import json, sys
def un(v):
    (k, x), = v.items()
    if k == "S": return x
    if k == "N": return int(x)
    if k == "BOOL": return x
    if k == "NULL": return None
    if k == "L": return [un(i) for i in x]
    if k == "M": return {a: un(b) for a, b in x.items()}
    return x
it = {k: un(v) for k, v in json.loads(open(sys.argv[1],encoding="utf-8-sig").read())["Item"].items()}
for k in ("outcome","resolvedBy","role","stageReached","claimedBy","confirmedBy","reescalations","helpAt"):
    print(f"  {k}: {it.get(k)}")
print("  openMsgs:", len(it.get("openMsgs", [])))
print("  timeline:", [(t["stage"], t.get("status") or t.get("Error")) for t in it.get("timeline", [])])
ev = it.get("events", []); t0 = ev[0]["t"] if ev else 0
print("  events:")
for e in ev:
    print(f"   +{e['t']-t0:>3}s {e['type']:<10} stage={e['stage']:<10} actor={e['actor']:<9} role={e['role']:<7} via={e['via']:<7} {e.get('detail','')}")
