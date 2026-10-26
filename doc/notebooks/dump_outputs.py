"""Print the output of every code cell of an executed notebook, under the heading of the markdown cell before it.

    python dump_outputs.py <executed.ipynb> [max_chars_per_output]

Read the whole print after each execution: an exit code of 0 does not show that a cell is right.
"""
import json
import sys

sys.stdout.reconfigure(encoding="utf-8")  # outputs hold "−", "Δ", "≤"; a Windows console codepage cannot print them
nb = json.load(open(sys.argv[1], encoding="utf-8"))
lim = int(sys.argv[2]) if len(sys.argv) > 2 else 1500
title = ""
for i, c in enumerate(nb["cells"]):
    if c["cell_type"] == "markdown":
        title = "".join(c["source"]).splitlines()[0]
        continue
    print(f"=== [{i}] {title}")
    if not c.get("outputs"):
        print("  (no output)")
    for o in c.get("outputs", []):
        if o["output_type"] == "error":
            print("  ERROR", o["ename"], o["evalue"])
            continue
        data = o.get("data", {})
        t = "".join(o.get("text", "")) or "".join(data.get("text/markdown") or data.get("text/plain", ""))
        print("  " + t[:lim].replace("\n", "\n  "))
