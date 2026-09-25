"""charts.py -- the dashboard's one figure, drawn as inline SVG.

Why SVG built here rather than a plotting library: this interface has two
dependencies, FastAPI and uvicorn, and the README says so. A chart library would
be a third, pulled in for one figure. The shapes below are a few dozen lines of
arithmetic, and the output is a single self-contained file a reader can save.

Why a dot plot and not a bar or a box: the measure exists for 52 projects spread
over 11 sectors, and five of those sectors have three observations or fewer. A
bar of means would show a confident number standing on one plant, and a box plot
of n=1 draws a box around a point. Every observation is therefore drawn, and the
median appears only where `quality.MEDIAN_MIN_N` observations support one -- the
statistic's absence is the small-n warning, which is sturdier than a footnote.

Colour is resolved to literal hex here, not `var(--teal)`. The saved file has no
stylesheet behind it, so a custom property would render as black in anything that
opened it. The token each literal comes from is named beside it; if the design
system moves, these move with it.
"""

from __future__ import annotations

import html

# --------------------------------------------------------------------------- #
# The design-system tokens this figure uses, resolved for a standalone file.   #
# --------------------------------------------------------------------------- #
CARD = "#E4EADD"        # --ground-card, the surface the figure sits on
INK = "#2E2B2A"         # --type-1, labels and the median tick
INK_2 = "#4F4B47"       # --type-2
INK_3 = "#6E6962"       # --type-3, axis ticks and the n badges
RULE_SOFT = "#CFD5C5"   # --rule-soft, the hairline grid
RULE = "#B8C0B0"        # --rule, the baseline
TEAL = "#1F4E52"        # --teal, every observation

# One series, so no categorical palette and nothing to keep apart by hue. The
# median is a different SHAPE -- a vertical tick -- and not a second shade,
# because --teal against --teal-dark measures ΔE 6.3 where the readable floor is
# 15: side by side on the same row they are one colour.
FONT_MONO = "'IBM Plex Mono', ui-monospace, 'Courier New', monospace"
FONT_SANS = "'IBM Plex Sans', system-ui, -apple-system, sans-serif"

# Geometry. The viewBox is fixed and the element scales; the container grows with
# the row count rather than fixing a height that would crop the axis band.
W = 760
PAD_R = 26
PAD_T, PAD_B = 34, 46
ROW_H = 26
DOT_R = 4.5             # >= 8px across, per the mark spec
RING = 2.0              # surface ring, so overlapping observations stay countable

LABEL_PT = 11.5         # sector names, sans
BADGE_PT = 10.0         # the n= badges, mono
GAP_EDGE = 6            # canvas edge -> sector name
GAP_LABEL = 12          # sector name -> n badge
GAP_PLOT = 12           # n badge -> the plot

# The left gutter is MEASURED, not chosen. It was a constant 196, and at that
# width "Chemicals and Plastics" hung 2.7px off the left edge while the widest
# badge overlapped the longest name by 2px -- neither visible at a glance, both
# certain. The sector vocabulary is configurable, so the next name added could
# be longer than any here; computing the gutter is what stops that from silently
# clipping. Mono is exactly 0.6em per character; the sans stack is measured
# conservatively at 0.58em, which over-reserves rather than under-reserves.
def _text_w(s: str, size: float, mono: bool) -> float:
    return len(s) * size * (0.60 if mono else 0.58)


def _gutter(rows: list[dict]) -> tuple[float, float]:
    """(plot left edge, sector-name right edge) for this set of rows."""
    label_w = max([_text_w(d["sector"], LABEL_PT, False) for d in rows] or [0])
    badge_w = max([_text_w("no data" if not d["n"] else f'n={d["n"]}',
                           BADGE_PT, True) for d in rows] or [0])
    label_right = GAP_EDGE + label_w
    return label_right + GAP_LABEL + badge_w + GAP_PLOT, label_right


def _nice_max(hi: float) -> float:
    """An axis top at a round half-year, never below the largest observation."""
    import math
    return max(1.0, math.ceil(hi * 2) / 2)


def lag_by_sector_svg(rows: list[dict], *, elem_id: str = "lagfig") -> str:
    """One horizontal dot plot: a row per sector, a dot per project.

    `rows` is `quality.lag_by_sector()` output, already ordered. Each dot carries
    a <title>, which is the tooltip in the page and survives into the saved file.
    """
    hi = max([d["max"] for d in rows if d["max"] is not None] or [1.0])
    top = _nice_max(hi)
    PAD_L, label_right = _gutter(rows)
    plot_w = W - PAD_L - PAD_R
    plot_h = ROW_H * len(rows)
    h = PAD_T + plot_h + PAD_B

    def x(v: float) -> float:
        return PAD_L + (v / top) * plot_w

    parts: list[str] = []
    add = parts.append

    add(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {h}" '
        f'width="100%" role="img" id="{html.escape(elem_id)}" '
        f'aria-labelledby="{html.escape(elem_id)}-t {html.escape(elem_id)}-d">')
    add(f'<title id="{html.escape(elem_id)}-t">Gestation lag by sector</title>')
    n_total = sum(d["n"] for d in rows)
    add(f'<desc id="{html.escape(elem_id)}-d">Years from announcement to first '
        f'output for {n_total} published projects, one dot per project, grouped '
        f'by sector. A vertical tick marks the median where a sector has at '
        f'least three.</desc>')
    # The saved file needs its own ground; the page's html background is not in it.
    add(f'<rect width="{W}" height="{h}" fill="{CARD}"/>')

    # --- x grid: solid hairlines, one shade off the surface, never dashed ---
    step = 0.5 if top <= 3 else 1.0
    v = 0.0
    while v <= top + 1e-9:
        gx = round(x(v), 2)
        add(f'<line x1="{gx}" y1="{PAD_T}" x2="{gx}" y2="{PAD_T + plot_h}" '
            f'stroke="{RULE_SOFT}" stroke-width="1"/>')
        add(f'<text x="{gx}" y="{PAD_T + plot_h + 18}" text-anchor="middle" '
            f'font-family="{FONT_MONO}" font-size="10" fill="{INK_3}">{v:g}</text>')
        v += step
    add(f'<line x1="{PAD_L}" y1="{PAD_T + plot_h}" x2="{W - PAD_R}" '
        f'y2="{PAD_T + plot_h}" stroke="{RULE}" stroke-width="1"/>')
    add(f'<text x="{PAD_L + plot_w / 2}" y="{PAD_T + plot_h + 38}" '
        f'text-anchor="middle" font-family="{FONT_MONO}" font-size="10" '
        f'letter-spacing="1.2" fill="{INK_3}">YEARS FROM ANNOUNCEMENT TO FIRST OUTPUT</text>')

    # --- one row per sector -------------------------------------------------
    for i, d in enumerate(rows):
        cy = PAD_T + i * ROW_H + ROW_H / 2
        label = html.escape(d["sector"])
        thin = d["median"] is None and d["n"] > 0
        empty = d["n"] == 0
        colour = INK_3 if (thin or empty) else INK

        add(f'<text x="{label_right:.1f}" y="{cy + 3.5}" text-anchor="end" '
            f'font-family="{FONT_SANS}" font-size="{LABEL_PT}" fill="{colour}">{label}</text>')
        badge = "no data" if empty else f'n={d["n"]}'
        add(f'<text x="{PAD_L - GAP_PLOT:.1f}" y="{cy + 3.5}" text-anchor="end" '
            f'font-family="{FONT_MONO}" font-size="{BADGE_PT:g}" fill="{INK_3}">{badge}</text>')

        if empty:
            # Drawn as an absence, not left out. A sector missing from the figure
            # and a sector with nothing produced yet are different facts.
            add(f'<line x1="{PAD_L}" y1="{cy}" x2="{PAD_L + 26}" y2="{cy}" '
                f'stroke="{RULE}" stroke-width="1"/>')
            continue

        # The median first, so the observations sit on top of it.
        if d["median"] is not None:
            mx = round(x(d["median"]), 2)
            add(f'<line x1="{mx}" y1="{cy - 9}" x2="{mx}" y2="{cy + 9}" '
                f'stroke="{INK}" stroke-width="2">'
                f'<title>{label} — median {d["median"]:.1f} years, n={d["n"]}</title></line>')

        for value, project in d["observations"]:
            dx = round(x(value), 2)
            add(f'<circle cx="{dx}" cy="{cy}" r="{DOT_R}" fill="{TEAL}" '
                f'stroke="{CARD}" stroke-width="{RING}">'
                f'<title>{html.escape(project)} — {value:.1f} years</title></circle>')

    add("</svg>")
    return "".join(parts)


def lag_by_sector_table(rows: list[dict]) -> str:
    """The same figure as text. A tooltip cannot be the only way to read a value,
    and this is also what a screen reader and a printed page get."""
    body = ""
    for d in rows:
        med = f'{d["median"]:.1f}' if d["median"] is not None else "—"
        rng = (f'{d["min"]:.1f} – {d["max"]:.1f}' if d["n"] else "—")
        body += (f'<tr><td>{html.escape(d["sector"])}</td><td>{d["n"]}</td>'
                 f'<td>{med}</td><td>{rng}</td></tr>')
    return (
        '<table class="figtable"><thead><tr><th>Sector</th><th>n</th>'
        '<th>Median</th><th>Range</th></tr></thead>'
        f'<tbody>{body}</tbody></table>'
    )
