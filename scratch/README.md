# Scratch

Working files, none of them data. Nothing here is committed, and deleting any of
it loses nothing that cannot be downloaded or written again.

Both collect prompts build a row as JSON before handing it to the pipeline,
because writing the file first lets the run re-read and correct it if
`screen-check` rejects something:

    scratch/lead.json    a Source lead, on its way to `source-add`
    scratch/row.json     a Screen row, on its way to `screen-add`

They are overwritten by the next lead or row and are meaningless once the
command that consumed them has returned — the database has the content, and
these are only the envelope it arrived in.

The web app keeps its saved copies of cited pages here too:

    scratch/pages/       one JSON file per page the review pane has downloaded

Each is used for a week and then downloaded again. Delete the directory and
every page downloads the next time it opens. They are copies of other people's
articles, so they stay out of the repository. See `webapp/page_cache.py`.

`.gitignore` ignores everything here except this README, which is tracked so
the directory exists in a fresh clone and the prompts have somewhere to write.
