
- model outcomes of experiments using a bayesian or llm-based model
- let an llm define the graph of how steps of how to check an idea from start to finish
- how to reduce feature creep?
- llm as cheat-reviewer

- a note from an agent indicates it might make sense to watch the-lab-agent outputs for bash commands and send out notifications for alternative api endpoint uses, when certain commands like grep on .the_lab is detected. note from agent:
  > The framework's greatest strength (everything is a readable file on disk) is also why I under-used the *API*: when in doubt I `grep`'d the dir, which was always predictable. That's a good failure mode — but it means the higher-level API features only pay off if they're either (a) strictly easier than grepping, or (b) provide something the files don't (events, cross-run queries, variance, lineage). The items above are mostly "give me the things the raw files *can't* cheaply give me."
    -> comment: PARKED (deprioritized). We *could* pattern-match `grep .the_lab` in the PTY stream we already sample and nudge, but it's noisy and teaching-flavored. The valuable part is the thesis: the API only wins when it's strictly easier than grepping OR gives what files can't (events, variance, lineage, cross-run). That argues for investing in M3/M4 (push events, live progress) — which we're doing — rather than a nagger.
