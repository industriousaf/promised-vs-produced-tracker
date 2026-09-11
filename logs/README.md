# Run transcripts

Each run of `collect/all.sh` (or a stage script on its own) writes what it did
to a file here, named for the UTC time it started:

    logs/20260904T143000Z-collect.log

The database records what was collected. The transcript records **how**: which
model and effort, how many turns it took, which iterations failed, and which of
the three exits stopped it — target reached, stalled, or out of turns. For a
Scoreboard that will be cited, that is part of the claim, and none of it is
recoverable from the rows afterwards.

## What is committed

`.gitignore` ignores `logs/*.log`. A routine run is noise: large, append-only,
different every time, and committing them all would bury the diffs that matter.

The transcript of a run that produced **published** data is not routine. Keep
that one by allowlisting it in `../.gitignore` by name, beside the entry that
keeps the canonical database:

    logs/*.log
    !logs/20260904T143000Z-collect.log

Name it for what it produced when you do, in the commit message if not the
filename, so a reader can match a transcript to the rows it produced.

## Turning it off

`LOG=0` disables the transcript. `LOG=<path>` writes somewhere else.

```bash
LOG=0 N=3 bash collect/all.sh                 # no transcript
LOG=/tmp/probe.log N=3 bash collect/all.sh    # somewhere else
```

`all.sh` exports `LOG` before calling each stage, so Source and Screen append to
one transcript rather than opening two.
