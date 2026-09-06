"""
llm.py -- the AI half of the pipeline, in two flavours.

The Source (search) and Screen pt-1 (extract) steps are "primarily meant for AI"
but can be driven two ways, neither of which is mandatory:

  A. CLAUDE CODE (no API key needed).  render_source_prompt() /
     render_screen_prompt() return the exact operating prompt to paste into a
     web-search-capable assistant like Claude Code. You run it there, it does the
     scraping, and hands back one JSON object which you ingest with the ordinary
     manual insert (source-add --json / screen-add --json, or the web textareas).
     This is the recommended path when you don't have an ANTHROPIC_API_KEY.

  B. DIRECT ANTHROPIC API (needs a key).  collect_source_lead() /
     extract_screen_row() call the Anthropic Messages API with the **web search**
     + **web fetch** server tools, then a second call with `output_config.format`
     (JSON Schema) to return a *schema-validated* object -- the "structured
     output option" the prompt asked for.

Both flavours share the SAME prompt builders, so the instructions are identical
whether a human, Claude Code, or the API executes them. The prompt-rendering
functions have no dependency on `anthropic` and work with no key; only the
flavour-B functions import the SDK (lazily).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pipeline import models
from pipeline.schema_check import all_sectors, register_sector

# Models newer than Opus 4.6 support the _20260209 web tools with dynamic
# filtering; Opus 4.8 is the default for flavour B.
# The model name lives in pipeline/models.py, once. models.api() still lets
# PIPELINE_MODEL win -- that is the variable this path has always used -- but it
# now also answers to the global MODEL, so "change the model everywhere" no
# longer has an exception you have to know about.
MODEL = models.api()
EFFORT = os.getenv("PIPELINE_EFFORT", "high")

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"

WEB_TOOLS = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 8},
    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 8},
]

# The keys each step returns -- documented here so the Claude Code path knows the
# exact shape to hand back.
SOURCE_KEYS = ["promise_source", "status_source", "promised_date_source", "summary"]
# The keys the extractor returns. For each date it returns BOTH the normalized
# token (announced / promised_first_output / actual_first_output) AND the verbatim
# *_raw source text it was copied from. lag_years / slip_years and the *_dt columns
# are NOT here: the pipeline computes them deterministically from the tokens
# (pipeline/dates.py), so two models that agree on the tokens agree on lag/slip.
SCREEN_KEYS = [
    "project", "sector", "state",
    "announced", "announced_raw",
    "promised_capital_usd", "promised_jobs",
    "promised_first_output", "promised_first_output_raw",
    "actual_first_output", "actual_first_output_raw",
    "current_status", "notes", "promise_source",
    "status_source", "flag", "promised_date_source",
]


class LLMUnavailable(RuntimeError):
    """Raised when the direct-API flavour is requested but not configured."""


# --------------------------------------------------------------------------- #
# Prompt builders (no API, no key -- for the Claude Code path AND flavour B)   #
# --------------------------------------------------------------------------- #

# Reading a source that will not load is the same problem at both stages, so the
# procedure is written once and appended to both operating prompts. Keeping it in
# one file is the point: the last run rediscovered the same curl invocation six
# times, with six different user agents, because nothing told it the answer.
_FETCH_LADDER = "fetching.md"


def _operating_prompt(filename: str) -> str:
    """Load a *_prompt.md and return the operating prompt (text below the first
    horizontal rule; the preamble above it is guidance for us, not the model),
    with the shared fetch ladder appended."""
    text = (_PROMPT_DIR / filename).read_text(encoding="utf-8")
    marker = "\n---\n"
    if marker in text:
        text = text.split(marker, 1)[1]
    ladder = (_PROMPT_DIR / _FETCH_LADDER).read_text(encoding="utf-8").strip()
    return text.strip() + "\n\n" + ladder


def render_source_prompt(
    avoid_published: list[str] | None = None,
    avoid_unpublished: list[str] | None = None,
    year_coverage: dict[int, int] | None = None,
) -> str:
    """The full Source instructions for finding one new project.

    Paste the returned text into any assistant that can search the web; it will
    find one new qualifying project and end with a single JSON object you can
    ingest with `source-add --json` (CLI) or the Source "Add from JSON" box
    (web).

    `year_coverage` is {announcement year: rows already collected} from
    `orchestrate.announced_year_coverage`. It is rendered as its own section so
    the collector can see the shape of what it has already produced. Without it
    the exclusion lists remove individual sites but never a vintage, and the
    corpus drifts toward whichever period is most heavily reported.

    `avoid_published` is the list of projects already published;
    `avoid_unpublished` the ones collected but not published yet (see the two
    functions of those names in `orchestrate`). They are rendered as separate
    sections so the collector avoids everything already in the pipeline, not
    just what has reached verify -- which stays empty until a human publishes.
    """
    prompt = _operating_prompt("prompt_source_collected.md")
    # Always emitted, under the exact title the prompt body points at, so every
    # path that renders this prompt shows the live verify_verified state in the
    # expected place. Emitted even when the table is EMPTY: an absent section
    # would leave the collector guessing what is already covered, and on a
    # rebuild-from-empty the honest answer is "nothing is".
    prompt += "\n\n## The verify table already holds these — do not collect them\n"
    if avoid_published:
        prompt += (
            "These `project` names are the current contents of the `verify_verified` "
            "table (the authoritative, verified scoreboard). Do **not** collect any of "
            "them, nor a mere expansion / re-announcement of one:\n> "
            + " · ".join(sorted(set(avoid_published)))
        )
    else:
        prompt += (
            "The `verify_verified` table is currently **empty** — nothing has been "
            "published yet, so no project is excluded on this basis. Collect the best "
            "qualifying project you can find, including the largest and most obvious."
        )
    if year_coverage:
        total = sum(year_coverage.values())
        empty = [y for y, n in sorted(year_coverage.items()) if n == 0]
        thin = [y for y, n in sorted(year_coverage.items())
                if 0 < n <= max(1, total // (2 * max(len(year_coverage), 1)))]
        bars = "\n".join(
            f"    {y}   {n:>3}  {'#' * n}" for y, n in sorted(year_coverage.items()))
        prompt += "\n\n## Announcement years already covered — prefer the thin ones\n"
        if not total:
            # Nothing collected yet, so naming every empty year as a gap is noise.
            # Say what the window is and leave the choice open, the way the
            # exclusion section does when verify_verified is empty.
            prompt += (
                "Nothing has been collected yet, so no year is over- or "
                "under-represented. The eligible window is **January 2017 to "
                "today**, and every year in it is equally eligible — spread your "
                "picks across it rather than taking several from one year."
            )
        else:
            prompt += (
                "This is the corpus you are adding to, counted by announcement "
                "year:\n\n```\n" + bars + "\n```\n\n"
                "The eligible window is January 2017 to today and every year in it "
                "is equally eligible. Heavily reported projects cluster in a few "
                "years, so an open-ended search returns those years over and over — "
                "the exclusion lists above remove the individual sites already "
                "taken, never the vintage. **Prefer a year with few or no rows.** "
            )
            if empty:
                prompt += ("Years with **nothing at all** so far: "
                           + ", ".join(str(y) for y in empty) + ". Start there. ")
            elif thin:
                prompt += ("Thinnest so far: " + ", ".join(str(y) for y in thin) + ". ")
            prompt += (
                "\n\nThis is a preference, not a quota, and it never outranks the "
                "inclusion rules. If a year genuinely has no qualifying project you "
                "can source and verify, move to the next-thinnest rather than "
                "lowering the bar or inventing one — a corpus balanced by year but "
                "padded with weak rows is worse than an unbalanced one."
            )

    if avoid_unpublished:
        # Collected this run (or an earlier one) but not published. Excluding
        # these is what stops the collector from re-finding the same top project
        # every round while verify sits empty. Shown as Screen `project` names
        # and shortened Source summaries.
        prompt += (
            "\n\n## Already collected — not yet published; do not collect these again\n"
            "These projects have been collected already but are still waiting to "
            "be published, shown as Screen `project` names and shortened Source "
            "summaries. Do **not** collect any project they describe, nor a mere "
            "expansion / re-announcement of one — pick something new:\n> "
            + " · ".join(sorted(set(avoid_unpublished)))
        )
    prompt += (
        "\n\n## How to hand the result back\n"
        "Do the web search/scraping yourself, then end your reply with **only** "
        "the single JSON object from the Output format section above (keys: "
        f"{', '.join(SOURCE_KEYS)}), on its own, so it can be pasted straight "
        "back into the pipeline. No commentary after the JSON."
    )
    return prompt


def render_screen_prompt(lead: dict) -> str:
    """The full Screen pt-1 operating prompt for a specific Source lead.

    Paste into Claude Code; it opens the lead's links, extracts the 18-column
    row, and ends with a single JSON object you ingest with `screen-add --json`
    (CLI) or the Screen "Add from JSON" box (web).
    """
    prompt = _operating_prompt("prompt_screen_extracted.md")
    prompt += (
        "\n\n## Sector vocabulary — classify into ONE of these, exactly\n"
        "> " + " · ".join(sorted(all_sectors())) + "\n\n"
        "This list is **closed**. The `sector` value must be one of the strings "
        "above, copied exactly. Do **not** invent a sector, coin a narrower or more "
        "precise label, or adapt one of these. If the project fits none of them "
        "well, the answer is `Other` — that is what `Other` is for. If you think a "
        "genuinely new manufacturing sector is warranted, still write `Other` and "
        "name the candidate in `flag` (e.g. \"Other used; candidate new "
        "sector: Cement\") so a human can decide. Do **not** edit `SECTORS` in "
        "`schema.py`, and do **not** run `sectors-add` or `register_sector()` — "
        "extending the vocabulary is a human decision, not yours. A sector outside "
        "the list above is rejected by the checker."
    )
    prompt += (
        "\n\n## The lead to extract from\n\n"
        f"- promise_source: {lead.get('promise_source', '') or '(none)'}\n"
        f"- status_source: {lead.get('status_source', '') or '(none)'}\n"
        f"- promised_date_source: {lead.get('promised_date_source', '') or '(none)'}\n"
        f"- summary: {lead.get('summary', '') or '(none)'}\n"
    )
    if lead.get("source_collected_id"):
        prompt += f"- source_collected_id: {lead['source_collected_id']}\n"
    prompt += (
        "\nOpen these links, read them, and extract the row.\n\n"
        "## How to hand the result back\n"
        "End your reply with **only** the single JSON object from the Output "
        f"format section above (the {len(SCREEN_KEYS)} keys, including each date's "
        "*_raw verbatim partner), on its own, so "
        "it can be pasted straight back into the pipeline. Use digits only for "
        "promised_capital_usd and promised_jobs. Do not include "
        "verification_tier -- it is always P at this stage. No commentary after "
        "the JSON."
    )
    return prompt


# --------------------------------------------------------------------------- #
# Flavour B -- direct Anthropic API (needs a key)                             #
# --------------------------------------------------------------------------- #

def _client():
    try:
        import anthropic  # lazy: only needed for the API flavour
    except ImportError as e:  # pragma: no cover
        raise LLMUnavailable(
            "the API flavour needs the `anthropic` package: pip install anthropic. "
            "No key? Use the Claude Code path instead (render_*_prompt / the "
            "'-prompt' CLI commands)."
        ) from e
    try:
        return anthropic.Anthropic()
    except Exception as e:  # pragma: no cover
        raise LLMUnavailable(f"could not construct Anthropic client: {e}") from e


def _research(instruction: str, max_loops: int = 8) -> str:
    """Phase 1: run the model with web tools until it produces a final answer."""
    client = _client()
    import anthropic

    messages = [{"role": "user", "content": instruction}]
    last_text = ""
    for _ in range(max_loops):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=8000,
                thinking={"type": "adaptive"},
                output_config={"effort": EFFORT},
                tools=WEB_TOOLS,
                messages=messages,
            )
        except anthropic.APIError as e:  # pragma: no cover
            raise LLMUnavailable(f"Anthropic API error during research: {e}") from e

        if resp.stop_reason == "refusal":
            raise LLMUnavailable(
                "model refused the request "
                f"({getattr(resp.stop_details, 'category', None)})"
            )

        last_text = "".join(b.text for b in resp.content if b.type == "text")

        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        return last_text
    return last_text


def _schema_too_complex(err: Exception) -> bool:
    """True for the API's 'Schema is too complex.' 400 on output_config.format."""
    return "schema is too complex" in str(err).lower()


def _extract_json_object(text: str) -> dict:
    """Parse the first JSON object out of a model reply.

    The strict json_schema path returns bare JSON, but the schema-less fallback
    (below) can wrap it in prose or ```json fences, so tolerate both: strip a
    leading/trailing code fence, then fall back to the outermost {...} span.
    """
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


def _structure(research: str, schema: dict, instruction: str) -> dict:
    """Phase 2: turn the research into a JSON object.

    Prefer the strict `output_config.format` (json_schema) path. If the API
    rejects the schema as too complex, retry WITHOUT the schema and parse the
    reply ourselves -- the deterministic `screen_check` (schema_check.check_row)
    verifies the row downstream, so dropping the schema loses no validation.
    """
    client = _client()
    import anthropic

    prompt = f"{instruction}\n\n---\n{research}"

    def _create(use_schema: bool):
        kwargs = dict(
            model=MODEL,
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        if use_schema:
            kwargs["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
        return client.messages.create(**kwargs)

    try:
        resp = _create(use_schema=True)
    except anthropic.APIError as e:
        if _schema_too_complex(e):
            try:
                resp = _create(use_schema=False)
            except anthropic.APIError as e2:  # pragma: no cover
                raise LLMUnavailable(
                    f"Anthropic API error during structuring (fallback): {e2}"
                ) from e2
        else:  # pragma: no cover
            raise LLMUnavailable(f"Anthropic API error during structuring: {e}") from e

    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    try:
        return _extract_json_object(text)
    except json.JSONDecodeError as e:  # pragma: no cover
        raise LLMUnavailable(f"model did not return valid JSON: {text[:200]}") from e


_SOURCE_SCHEMA = {
    "type": "object",
    "properties": {
        "promise_source": {"type": "string"},
        "status_source": {"type": "string"},
        "promised_date_source": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["promise_source", "status_source"],
    "additionalProperties": False,
}

_SCREEN_SCHEMA = {
    "type": "object",
    "properties": {
        "project": {"type": "string"},
        "sector": {"type": "string"},
        "state": {"type": "string"},
        # Each date is a pair: the normalized token the parser consumes, plus the
        # verbatim source text (*_raw) it was copied off the page.
        "announced": {"type": "string"},
        "announced_raw": {"type": "string"},
        "promised_capital_usd": {"type": "integer"},
        "promised_jobs": {"type": "integer"},
        "promised_first_output": {"type": "string"},
        "promised_first_output_raw": {"type": "string"},
        "actual_first_output": {"type": "string"},
        "actual_first_output_raw": {"type": "string"},
        "current_status": {"type": "string"},
        "notes": {"type": "string"},
        "promise_source": {"type": "string"},
        "status_source": {"type": "string"},
        "flag": {"type": "string"},
        "promised_date_source": {"type": "string"},
    },
    "required": ["project", "sector", "state", "announced", "current_status"],
    "additionalProperties": False,
}


def collect_source_lead(
    avoid_published: list[str] | None = None,
    avoid_unpublished: list[str] | None = None,
    year_coverage: dict[int, int] | None = None,
) -> dict:
    """[API flavour] Find ONE new qualifying project and return its lead dict."""
    research = _research(render_source_prompt(avoid_published, avoid_unpublished,
                                             year_coverage))
    if _looks_like_no_result(research):
        raise LLMUnavailable(
            "the model reported it could not find a qualifying new project"
        )
    lead = _structure(
        research,
        _SOURCE_SCHEMA,
        "From the research below, output the single source_collected lead as JSON "
        f"with keys {', '.join(SOURCE_KEYS)} (promised_date_source and summary "
        "optional). If no qualifying project was found, return empty "
        "promise_source and status_source.",
    )
    if not (lead.get("promise_source") and lead.get("status_source")):
        raise LLMUnavailable("no qualifying project found (empty sources)")
    return lead


def extract_screen_row(lead: dict) -> dict:
    """[API flavour] Extract one 18-column row from a Source lead."""
    research = _research(render_screen_prompt(lead))
    row = _structure(
        research,
        _SCREEN_SCHEMA,
        "From the extraction below, output the single screen_extracted row as JSON. "
        "Digits only for promised_capital_usd and promised_jobs. For EACH date give "
        "TWO cells: the normalized token (announced, promised_first_output, "
        "actual_first_output) that the pipeline can parse, AND its *_raw partner "
        "(announced_raw, promised_first_output_raw, actual_first_output_raw) holding "
        "the EXACT source text you copied it from, verbatim -- no cleanup. Do NOT "
        "include lag_years, slip_years, the *_dt columns, or verification_tier: the "
        "pipeline computes those deterministically. Extraction problems go in `flag` "
        "('None' if clean).",
    )
    row.setdefault("promise_source", lead.get("promise_source", ""))
    row.setdefault("status_source", lead.get("status_source", ""))
    if lead.get("promised_date_source"):
        row.setdefault("promised_date_source", lead["promised_date_source"])

    # Sector standardization (API path): if the extracted sector isn't in the
    # live vocabulary, FLAG it and ADD it onto the schema via the function --
    # register_sector() persists it to the registry, distinct from the Claude
    # Code path which edits the SECTORS set in code.
    sector = (row.get("sector") or "").strip()
    if sector and sector not in all_sectors():
        if register_sector(sector):
            note = (f"new sector {sector!r} was not in the vocabulary -- "
                    "registered via register_sector()")
            prior = (row.get("flag") or "").strip()
            row["flag"] = (
                note if not prior or prior.lower() == "none" else f"{prior}; {note}"
            )
    return row


def _looks_like_no_result(text: str) -> bool:
    low = text.lower()
    return (
        "could not find" in low
        or "no qualifying" in low
        or "unable to find" in low
    ) and "http" not in low
