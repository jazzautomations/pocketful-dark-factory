"""Resumo de uso por seat e tempo por stage a partir do dump da room (API)."""
import json, sys, collections
from datetime import datetime
d=json.load(open(sys.argv[1]))["messages"]; d.sort(key=lambda m:m["inserted_at"])
use=collections.defaultdict(lambda: collections.Counter()); turns=collections.Counter()
for m in d:
    md=m.get("metadata") or {}
    bu=md.get("band_usage") if isinstance(md,dict) else None
    if bu:
        turns[m["sender_name"]]+=1
        for k,v in bu.items():
            if isinstance(v,(int,float)): use[m["sender_name"]][k]+=v
t0=datetime.fromisoformat(d[0]["inserted_at"].replace("Z","+00:00"))
print("dispatch:", d[0]["inserted_at"][:19], "| messages:", len(d))
for m in d:
    c=m["content"] or ""
    if m["message_type"]=="text" and m["sender_type"]=="Agent" and c.startswith("@[[9da0") and "ACCEPTED" in c[:120]:
        t=datetime.fromisoformat(m["inserted_at"].replace("Z","+00:00"))
        print(f"  {c.split(chr(8212))[0].strip()[-20:]:>20} at +{int((t-t0).total_seconds()//60)} min")
rej=[m for m in d if m["message_type"]=="text" and m["sender_name"]=="reviewer" and (m["content"] or "")[:200].count("REJECT")]
print("reviewer REJECT messages:", len(rej))
for s,c in use.items():
    print(f"{s:12} turns={turns[s]:3} " + " ".join(f"{k}={v:,}" for k,v in sorted(c.items())))
print("seat text messages:", collections.Counter(m["sender_name"] for m in d if m["message_type"]=="text" and m["sender_type"]=="Agent"))
