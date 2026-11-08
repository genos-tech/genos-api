"""One-sentence context per task in a report (MIP-1210).

Ken: when a task's comments or history explain its state, the report says so in ONE simple
sentence ("Task A is blocked because XYZ"). This package is the whole of that, kept out of
`origin/search_engine/` on purpose: a change in there triggers the Vertex-billed agent evals,
and nothing here is the in-app agent (no tool, no prompt of its own, no `list_my_inbox`
change: the sentence goes in the report item's typed `detail`, which the in-app model is never
shown, see `list_my_inbox._model_body`).

  * `context.py`   what a task's comments and history say, read ONLY for tasks the member can
                   open, every free-text field fenced as untrusted;
  * `prompt.py`    the one batched prompt;
  * `llm.py`       the one metered call, on the model rung the effort level names;
  * `validate.py`  what comes back is untrusted too: strict JSON, one short plain sentence per
                   task we asked about, no markup, no link, no task the member cannot open;
  * `sentences.py` the orchestration: candidates, cache, call, clean, and the cost;
  * `estimate.py`  the cost of a run per effort level, from the real prompt's size (an estimate).

A failed or skipped call never fails a report: `sentences.sentences_for` returns no sentences
and the plain item stays.
"""

from origin.services.report_ai.sentences import Sentences, sentences_for  # noqa: E402

__all__ = ["Sentences", "sentences_for"]
