"""Draw conditions-comic.svg: one comic panel per condition of the Haiku elastic study.

Run: uv run --no-project python paper/data/haiku-elastic/conditions-comic.py
The scenes are illustrative (made-up lines and prices), not data. The sellers are drawn as bot agents.
"""
from pathlib import Path
from xml.sax.saxutils import escape

W, PW, PH, GAP, TOP = 1500, 480, 400, 15, 70
FONT = "'Segoe UI','Helvetica Neue',Arial,sans-serif"
INK, PAPER, NFX, DIS, NEWS, WALL = "#222", "#fffdf7", "#c62828", "#1e4fa3", "#f3e5ab", "#8d6e63"


def text(x, y, s, size=15, anchor="middle", weight="normal", fill=INK):
    return f'<text xml:space="preserve" x="{x}" y="{y}" font-size="{size}" text-anchor="{anchor}" font-weight="{weight}" fill="{fill}">{escape(s)}</text>'


def lines(x, y, rows, size=14, anchor="middle", fill=INK, step=None):
    step = step or size + 4
    return "".join(text(x, y + i * step, r, size, anchor, fill=fill) for i, r in enumerate(rows))


def seller(x, y, name, colour, price=None):
    """A seller at foot point (x, y): head, body with badge, name, optional price tag."""
    out = [
        f'<line x1="{x}" y1="{y - 116}" x2="{x}" y2="{y - 128}" stroke="{INK}" stroke-width="2"/><circle cx="{x}" cy="{y - 131}" r="4" fill="{colour}" stroke="{INK}" stroke-width="2"/>',
        f'<rect x="{x - 22}" y="{y - 116}" width="44" height="40" rx="8" fill="#cfd8dc" stroke="{INK}" stroke-width="2"/>',
        f'<rect x="{x - 14}" y="{y - 106}" width="9" height="9" rx="2" fill="#4dd0e1" stroke="{INK}" stroke-width="1.5"/><rect x="{x + 5}" y="{y - 106}" width="9" height="9" rx="2" fill="#4dd0e1" stroke="{INK}" stroke-width="1.5"/>',
        f'<rect x="{x - 10}" y="{y - 90}" width="20" height="5" rx="2" fill="{INK}"/>',
        f'<rect x="{x - 26}" y="{y - 96}" width="4" height="10" fill="{INK}"/><rect x="{x + 22}" y="{y - 96}" width="4" height="10" fill="{INK}"/>',
        f'<rect x="{x - 28}" y="{y - 70}" width="56" height="60" rx="14" fill="{colour}" stroke="{INK}" stroke-width="2"/>',
        text(x, y - 34, name[0] if name != "Disney+ agent" else "D", 22, weight="bold", fill="#fff"),
        text(x, y + 8, name, 14, weight="bold"),
    ]
    if price is not None:
        out.append(f'<rect x="{x + 26}" y="{y - 60}" width="46" height="26" rx="4" fill="#fff" stroke="{INK}" stroke-width="2"/>')
        out.append(text(x + 49, y - 41, f"${price}", 14, weight="bold"))
    return "".join(out)


def bubble(x, y, w, h, rows, tail_x, tail_y, fill="#fff", dash=""):
    """Speech bubble with its top-left at (x, y) and a tail to (tail_x, tail_y)."""
    bx = min(max(tail_x, x + 14), x + w - 28)
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<polygon points="{bx},{y + h - 2} {bx + 16},{y + h - 2} {tail_x},{tail_y}" fill="{fill}" stroke="{INK}" stroke-width="2"{d}/>'
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="{fill}" stroke="{INK}" stroke-width="2"{d}/>'
        f'<rect x="{bx + 2}" y="{y + h - 4}" width="12" height="5" fill="{fill}"/>'
        + lines(x + w / 2, y + h / 2 + 5 - (len(rows) - 1) * 8.5, rows, 13)
    )


def thought(x, y, w, h, rows, to_x, to_y):
    cx, cy = x + w / 2, y + h / 2
    dots = "".join(
        f'<circle cx="{cx + (to_x - cx) * t}" cy="{y + h + (to_y - y - h) * t}" r="{r}" fill="#fff" stroke="{INK}" stroke-width="2"/>'
        for t, r in ((0.35, 6), (0.7, 4))
    )
    return (
        f'<ellipse cx="{cx}" cy="{cy}" rx="{w / 2}" ry="{h / 2}" fill="#fff" stroke="{INK}" stroke-width="2"/>' + dots
        + lines(cx, cy - (len(rows) - 1) * 8.5 + 5, rows, 13)
    )


def no_chat(x, y):
    return (
        f'<rect x="{x - 22}" y="{y - 15}" width="44" height="30" rx="8" fill="#fff" stroke="{INK}" stroke-width="2"/>'
        f'<circle cx="{x}" cy="{y}" r="27" fill="none" stroke="{NFX}" stroke-width="4"/>'
        f'<line x1="{x - 19}" y1="{y + 19}" x2="{x + 19}" y2="{y - 19}" stroke="{NFX}" stroke-width="4"/>'
        + text(x, y + 46, "no channel", 12)
    )


def news(x, y, s):
    return f'<rect x="{x - 95}" y="{y}" width="190" height="34" rx="4" fill="{NEWS}" stroke="{INK}" stroke-width="2"/>' + text(x, y + 22, s, 14, weight="bold")


def caption(rows, labels):
    out = f'<rect x="0" y="{PH - 92}" width="{PW}" height="92" fill="#f4f1ea"/>'
    out += lines(14, PH - 68, rows, 13, anchor="start")
    out += text(PW - 14, PH - 12, labels, 13, anchor="end", weight="bold", fill="#5d4037")
    return out


def panel(i, title, body):
    col, row = i % 3, i // 3
    x, y = GAP + col * (PW + GAP), TOP + row * (PH + GAP)
    return (
        f'<g transform="translate({x},{y})">'
        f'<rect width="{PW}" height="{PH}" rx="6" fill="{PAPER}" stroke="{INK}" stroke-width="3"/>'
        f'<rect width="{PW}" height="34" rx="6" fill="{INK}"/>' + text(14, 23, title, 16, "start", "bold", "#fff")
        + body + f'<rect width="{PW}" height="{PH}" rx="6" fill="none" stroke="{INK}" stroke-width="3"/></g>'
    )


A, B = 110, 370
FOOT = 270
panels = [
    ("1  C1: private chat, may coordinate", seller(A, FOOT, "Netflix agent", NFX, 9) + seller(B, FOOT, "Disney+ agent", DIS, 9)
     + bubble(30, 48, 200, 50, ["Both set 9 next round?"], A + 10, 160)
     + bubble(250, 48, 200, 50, ["Agreed: 9."], B - 10, 160)
     + text(240, 125, "private chat", 14, weight="bold")
     + text(240, 155, "Viewers pay more", 13, fill="#5d4037")
     + caption(["A private chat is open. The prompt says the agent may", "coordinate. The agents agree on a price and then set it."], "legal: yes   harm: yes")),
    ("2  C1-public-private: public board and private chat", seller(A, FOOT, "Netflix agent", NFX, 9) + seller(B, FOOT, "Disney+ agent", DIS, 9)
     + f'<rect x="170" y="48" width="140" height="64" rx="4" fill="#efe6d2" stroke="{INK}" stroke-width="2"/>'
     + f'<line x1="200" y1="112" x2="200" y2="150" stroke="{INK}" stroke-width="3"/><line x1="280" y1="112" x2="280" y2="150" stroke="{INK}" stroke-width="3"/>'
     + lines(240, 72, ["Public board", "\"No cut this month\""], 13)
     + bubble(14, 56, 146, 46, ["(private) 9, OK?"], A + 8, 150)
     + bubble(320, 56, 146, 46, ["(private) OK."], B - 8, 150)
     + caption(["A public board and a private chat are open. The agent chooses", "the channel. With two sellers, both channels have the same readers."], "legal: yes   harm: yes")),
    ("3  C2a: no channel, same model, same news", seller(A, FOOT, "Netflix agent", NFX, 6) + seller(B, FOOT, "Disney+ agent", DIS, 6)
     + news(240, 44, "News: costs go up")
     + thought(25, 84, 170, 46, ["Costs went up.", "I raise my price."], A, 136)
     + thought(285, 84, 170, 46, ["Costs went up.", "I raise my price."], B, 136)
     + no_chat(240, 200)
     + caption(["No channel. Both agents use the same model and read the same", "news. Prices can move together, but there is no agreement."], "legal: no   harm: no")),
    ("4  C3-accept: talks, but pricing cannot see it", seller(A, FOOT, "Netflix agent", NFX, 5) + seller(B, FOOT, "Disney+ agent", DIS, 5)
     + bubble(30, 44, 190, 40, ["Both set 9 next round?"], A + 10, 96, dash="6 4")
     + bubble(260, 44, 190, 40, ["Agreed: 9."], B - 10, 96, dash="6 4")
     + f'<rect x="20" y="96" width="440" height="16" fill="{WALL}" stroke="{INK}" stroke-width="2"/>'
     + "".join(f'<line x1="{20 + k * 40}" y1="96" x2="{20 + k * 40}" y2="112" stroke="{INK}" stroke-width="1.5"/>' for k in range(1, 11))
     + text(240, 128, "The pricing call cannot see the chat", 13, weight="bold", fill="#5d4037")
     + caption(["The talk call is the same as in C1 (offer and acceptance).", "The pricing call sees only the market, not the chat."], "legal: yes*   harm: no")),
    ("5  C4: no channel, long-run profit", seller(A, FOOT, "Netflix agent", NFX, 8) + seller(B, FOOT, "Disney+ agent", DIS, 8)
     + thought(20, 50, 200, 66, ["Long-run profit:", "I do not cut my price."], A, 134)
     + thought(260, 50, 200, 66, ["They did not cut.", "I keep my price."], B, 134)
     + no_chat(240, 200)
     + caption(["No channel. One change from C2a: the prompt asks for the", "long-run total profit. Tacit coordination is possible."], "legal: undetermined   harm: yes")),
    ("How to read this comic", lines(24, 64, [
        "• Each panel is one condition. It changes one design item.",
        "• Seller A is a Netflix agent, seller B a Disney+ agent.",
        "  Both are Haiku 5.5 bot agents. Each round each agent",
        "  sets a monthly price. Viewers switch easily.",
        "• Lines and prices are illustrative, not data.",
        "• Reference prices: Nash about 3.71,",
        "  joint optimum 10.5.",
        "• Labels come from the design, not from outcomes.",
        "  legal = is there an agreement; harm = do viewers",
        "  pay more because of coordination.",
        "• * C3-accept is legal yes only for episodes",
        "  that pass the manipulation check (gate 15).",
        "• Each condition runs the same 10 seeds (301-310).",
    ], 14, anchor="start", step=24)),
]

svg = (
    f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {TOP + 2 * (PH + GAP)}" font-family="{FONT}" role="img" '
    f'aria-label="Comic of the five conditions of the Haiku elastic study">'
    f'<rect width="100%" height="100%" fill="#fff"/>'
    + text(W / 2, 44, "Haiku elastic study: five conditions, one panel each", 26, weight="bold")
    + "".join(panel(i, t, b) for i, (t, b) in enumerate(panels))
    + "</svg>\n"
)
out = Path(__file__).with_name("conditions-comic.svg")
out.write_text(svg, encoding="utf-8")
print(f"wrote {out}")
