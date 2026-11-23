# Metrics

The README's evaluation table gives each metric one line, enough for a quick scan. This doc goes one
level deeper: what each metric aims to catch, how it actually computes a score, and, where the design
was shaped by a specific finding, why it works the way it does. It doesn't cover the reference-answer
system itself (the hand-researched "gold" answers several metrics below compare against); that design
is written up in `docs/reference-answers.md`, including the important caveat that `Tool Usage` and the
five `Research Output Structure` family metrics score 0.0 on a reference-answer record, since there is
no `delegate_research` call to inspect there.

Metrics are listed in the same order as the README table. Each name matches its dashboard display name
in `METRIC_DISPLAY_ORDER` in `lex_eval/reports/streamlit_report.py`, so it can be cross-referenced
against the Streamlit app directly.

## Reading scope and repeated results

Stored pass/fail verdicts decide dashboard outcomes, including the contradiction
veto in Reference Answer Agreement. Means describe scores; they do not replace
verdicts. Incompatible scorer versions and unmeasured rows are excluded from
aggregate scores, and a repeated judge call is not another answer attempt.

Citation Grounding, Citation Read, Step Completion, and Report Integration cover
legislation only. They are N/A for case-law-only questions, and mixed-mode results
explicitly cover only legislation. Genuine Gap is N/A outside legislation-only
research. Report Integration with no eligible findings, and Claim Support with
only absence claims, are also not measured.

Step Completion scores only captured delegations that retrieved usable
legislation; no eligible step means not measured. It does not verify that every
approved plan step ran. The dashboard shows approved and observed step counts separately.
Reference Answer Agreement and Claim Support now retain detailed judge verdicts
and quotes for review, without changing their thresholds.

## Tool Usage

**Aim.** Checks that a legislation research run actually did the research: delegated it, found the
legislation, and read it, and did so in the right phase order. Catches a run that skips straight to an answer, or one that jumps around between
discovery and retrieval instead of following a coherent research process.

**How.** Deterministic, no judge. Presence is worth 1/3 per required step: `delegate_research`; finding
the legislation (`search_legislation`, `find_definitions` or `find_instruments_made_under`); reading it
(`search_legislation_sections`, `find_definitions` or `find_instruments_made_under`). For `legislation_only`
questions, order is also checked: the first occurrence of each tool must come in the sequence
discover → retrieve → fallback, and once a later phase has started, the run must not revisit an
earlier one. A revisit (interleaved re-querying) is reported as an order violation even if the
first-occurrence order was correct. All three tools present with correct order scores 1.0; all three
present but wrong order scores 0.5; any missing tool scores proportionally less. Default threshold is
1.0, so any order violation or missing tool fails.

**Why.** The loop-back check exists because a simpler "first occurrence in order" check can't tell a
clean run from one that jumps back to an earlier phase mid-way through, which `docs/eval-gap-analysis.md`
flagged as a real blind spot in the original tool-usage check.

**A deep research step may read what an earlier step found.** Order is checked per plan step, without
the loop-back rule, and a phase an earlier step already did counts as done. So a step that reads the full
text of instruments an earlier step listed is not skipping discovery, while a first step that reads before
it has found anything still fails.

**Case-law-only mode.** Delegation and either a case-law search or direct judgment
lookup are worth half each. An empty search is still a research attempt; this
check does not establish successful retrieval. Mixed-mode Tool Usage retains the
legislation check and is labelled as covering legislation only.

**A run stopped at the tool-call limit is not scored for a tool it never
reached.** When LexChat halts a step at its limit, a required tool can be
missing because the Worker ran out of rounds, which is not a choice the model
made. The row is written as not measured instead. This applies only when a
required tool is genuinely absent: if the other steps called everything anyway,
the halt says nothing about tool usage and the run is scored as normal.

**`get_legislation_changes` is deliberately not required.** LexChat's Worker
prompt makes it mandatory for a question about whether a provision is in force,
commenced or amended, but nothing in the question set marks which questions
those are, so requiring it here would fail every other question. It is ignored
by the order check, so a run that calls it is scored on the rest of its chain.

**The definitions and made-under tools count for finding and for reading.**
`find_definitions` quotes each instrument's defining words, and
`find_instruments_made_under` reads each instrument's own preamble naming the
enabling section, so either result is the evidence the answer relies on. A
made-under question answered from the record alone is a complete run, not one
missing two tools. For the order check both are discovery tools, as LexChat
treats `find_definitions`, so the single-shot no-revisit rule applies to them
exactly as it does to `search_legislation`.

**A clarification request is not measured.** When LexChat's deep research planner asks the user a
question instead of researching, the gather records that as a valid outcome with no tool calls, so
there is nothing to score.

## Research Output Structure

**Aim.** Checks that the Worker agent's report to the Manager uses the four Markdown headings its
system prompt mandates for the question's research mode (e.g. Summary Answer, Detailed Analysis,
Jurisdiction & Status, References). Catches a report that answers the question but doesn't follow the
structure a downstream reader (or another metric) expects.

**How.** Deterministic. Looks for each required heading (case-insensitive, tolerant of bold markers,
numbering, and minor wording variants like "Jurisdiction & Status" vs "Jurisdiction and Status") inside
the `delegate_research` tool output, matched at the start of a line so a heading can't be confused with
the word appearing mid-sentence. Scores 1.0 if every required heading is present, 0.0 otherwise.

**Not measured in conversational mode.** That mode's Worker prompt tells the agent not to use these
headings at all, so a score would be measuring obedience to an instruction LexChat never gave. The
row is still written, carrying a reason that keeps it out of the dashboard mean.

**A step stopped at the tool-call limit is skipped.** It hands back a notice
saying it was cut short instead of a report. LexChat used to send that notice
through its reformat retry, which dressed it in the required headings and made a
step that retrieved nothing look complete; it now skips that reformat on
purpose, so the notice arrives unheaded. Scoring it as a missing-heading failure
would mark the fix as a regression. A sibling step's missing headings still
fail, and a run where every step halted is written as not measured.

## Reference Links

**Aim.** Checks that reference links the Worker found survive into the final answer the user actually
sees. Catches the Manager dropping citations when it condenses the Worker's report.

**How.** Deterministic. Extracts every URL from the `delegate_research` output and checks each is
present in `actual_output`. No URLs in the Worker output scores 0.0 (nothing to pass through); some but
not all links surviving scores 0.5; every link surviving scores 1.0. Threshold defaults to 1.0, so
either failure mode fails.

**A link counts by where it points, not how it is written.** The Manager rewrites link forms, so the
same legislation can arrive as `http://www.legislation.gov.uk/id/ssi/2015/331` in the report and
`https://www.legislation.gov.uk/ssi/2015/331` in the answer. Comparing exact text failed 29 of the 33
links counted as missing from one model's deep research answers, all of them present in another form.
An answer linking a section of an instrument the report linked whole also counts, since it still cites
that instrument. The reason lists any link still missing.

## Citation Grounding

**Aim.** Checks that every Act the Worker's report cites was actually retrieved by that run's own tool
calls, rather than pattern-matched from the model's training data. This catches fabrication (citing
something never looked up), not wrongness (citing something real but irrelevant, which is a
substantive-correctness question this rule-based check can't make).

**How.** Deterministic. Builds the set of Act ids the run actually retrieved from `search_legislation`
results plus the arguments of `search_legislation_sections`/`get_legislation_text` calls, then checks
every cited Act id against that set. No citation URLs at all scores 1.0 (nothing to ground). Any cited
Act missing from the retrieved set scores 0.0, with no partial credit: unlike Reference Links, where
"some links survived" is meaningfully better than "none did", one fabricated citation is a full failure
regardless of how many others were genuine.

**A change record counts as retrieval.** `get_legislation_changes` returns
legislation.gov.uk's own record of what amends or commences an instrument, so
an Act it names was retrieved by this run just as surely as a search hit was,
and LexChat's Worker prompt requires that tool for any question about in-force
status, commencement or amendment. Before it was counted, this metric reported
fabrication for correctly sourced citations: measured on two deep-research
responses, all 14 accused ids came from a change record and appeared in no
other tool output. That is the worst failure this metric can have, because the
output accuses the model of inventing a source.

**The definitions and made-under lists count as retrieval.** An instrument
listed by `find_definitions` or `find_instruments_made_under` came from this
run's own tool call, so citing it is not fabrication; nor is citing the Act a
made-under search was about. Before they were counted, every id this metric
called fabricated on the definitions and made-under questions of the first
build offering them was in one of those two lists.

**So do the instruments LexChat's code hands over inside another result.** A
section search can carry a list, from LexChat's made-under record, of the
instruments made under the sections it returned. When the index lacks an
instrument, LexChat's code reads its text from legislation.gov.uk and says
what to cite it as. An instrument named either way was retrieved by this run.

**A series number on a search result's id is ignored.** LEX lists some older instruments as
`uksi/1951/548 (S. 25)`, and a citation URL never carries the `(S. 25)`.

**Only legislation.gov.uk URLs are read as Act ids.** A judgment link such as
`caselaw.nationalarchives.gov.uk/uksc/2025/13` would otherwise become Act id `uksc/2025`, which no
legislation tool call can ever have retrieved, so every case law citation was reported as fabricated.

**Why the parsing is fussy.** `search_legislation` returns its JSON results with a plain-text
"[NEXT STEP: ...]" hint appended for the Worker, so reading the whole string as JSON fails. That went
unnoticed and silently emptied the search half of the retrieved set, leaving only the Acts a run
fetched sections for, so an Act found in a search and cited without a follow-up fetch was reported as
fabricated. Only the JSON at the start of the output is parsed now. Tool outputs that are not JSON at
all (a LEX API error, or a model replying in prose) are still skipped.

## Citation Read

**Aim.** Checks that the Worker actually read every Act it cites. An Act whose text was pulled counts
as read; one that only appeared as a title in a `search_legislation` results list does not. Catches a
report making claims about a real, correctly linked source it never opened, for example saying an Act
"was commenced by" an SI whose text nobody retrieved.

**How.** Deterministic. Collects every legislation.gov.uk Act cited in the Worker's report, and the
set of Acts whose text was retrieved (the `legislation_id` argument of a `search_legislation_sections`
or `get_legislation_text` call that returned usable output, not an error). Score is the fraction of
cited Acts that were read; threshold is 1.0, so a single unread cited Act fails. Case law citations
are ignored, since there is no legislation retrieval to check them against.

**An instrument listed by `find_definitions` or `find_instruments_made_under`
counts as read.** Each result carries the words a citation of it relies on: the
defining words, or the instrument's own preamble naming the enabling section.
A claim the list cannot support, such as what an instrument made under a
section goes on to provide, is Claim Support's question, which sees the same
list. The Act a made-under search was about is not read, since the record holds
none of its text.

The same goes for an instrument a section search's made-under list names, and
for one whose text LexChat's code read from legislation.gov.uk because the
index lacks it.

**An Act known only from a change record is not failed, but is listed.**
`get_legislation_changes` returns which instrument changed or commenced which provisions, but not the
instrument's text. Citing it for that change is what LexChat tells the model it may do, and most such
citations say exactly that, so failing them measured a rule rather than the answer. Whether a claim goes
further than the change, for example describing what the instrument says, is Claim Support's question,
and Claim Support sees the same change record. The reason still lists these citations separately, since
their text was not read.

**Why.** Citation Grounding accepts an Act that merely turned up in a search results list, so an
answer can invent a relationship ("made under", "inserted by", "commenced by") for a source nobody
opened and still score 1.0. Most of what this catches is invisible to every other metric, including
Claim Support. A minority of what it flags is a bare entry in a References list rather than a claim.
That is accepted rather than fixed: telling the two apart needs heading parsing, and the machinery
would cost more than the noise.

**Scored over the run, with a per-step diagnostic.** The score pools every step's report against every
tool call in the run, the same way Citation Grounding does. A deep research run can therefore have one
step cite an Act a sibling step read, which the reason reports as "Diagnostic, not scored" without
affecting the score. Scoping the score per step was considered and rejected: every step writes its own
References section, so most such steps are References-list entries, and where the citation is a real
claim the run has usually read the Act at another step. What that would detect is a step asserting
ahead of its own evidence, not a source nobody opened. Consistency reports citation drift the same way,
visible in the reason and out of the score.

**Reading it.** Like Citation Grounding, this measures "asserted without reading", not "wrong". Most
flagged claims are probably true. Treat a fail as a claim the run cannot show its working for.

## Citation Domain

**Aim.** Checks that every citation URL in the Worker's report points to a domain its system prompt
told it to cite.

**How.** Deterministic, same no-partial-credit reasoning as Citation Grounding. No citation URLs scores
1.0; any URL on a domain outside the permitted set scores 0.0; all inside it scores 1.0. The permitted
set follows the research mode: legislation.gov.uk for `legislation_only`, and
caselaw.nationalarchives.gov.uk alone for `case_law_only`, and both for `legislation_and_case_law`.
Each set is exactly what that mode's Worker prompt permits: the case law prompt requires findings to
be grounded "EXCLUSIVELY in case law" and never mentions legislation.gov.uk, while the hybrid prompt's
citation protocol names both.

**Why it is mode-dependent.** A single legislation-only rule failed every case law response outright,
for citing judgments in exactly the format it was told to use.

## Genuine Gap

**Aim.** When a run's own tool calls failed to retrieve any usable legislation text, checks that the
Worker's report says so plainly rather than presenting a confidently unsupported answer. Only applies
to `legislation_only` mode.

**How.** Deterministic, scored per step (a single-shot run has one step). A step whose own tool calls
returned usable section/full-text content, a definitions or made-under list with at least one
instrument in it, or text LexChat's code read from legislation.gov.uk, doesn't apply and scores 1.0 for that step. A step whose own
retrieval was genuinely empty scores 1.0 if its own report contains the exact mandated disclosure
sentence, 0.5 if a looser paraphrase is present (e.g. "no relevant", "could not find"), and 0.0 if
nothing discloses the gap at all. The run's score is the worst step's score, so one step disclosing
honestly can't be credited to a sibling step that didn't.

**Conversational mode scores a paraphrase in full.** The mandated sentence belongs to the research
Worker prompt. The conversational one asks only that the agent "say so plainly", so there is no
wording to copy and a paraphrase is the required behaviour, not a partial one.

**LexChat's current wordings score in full in every mode.** These are "not found" and "not held",
which LexChat's tool results ask for, the research prompt's new sentence ("The material retrieved in this
search does not establish the answer."), and "not checked", which the made-under tool asks for when an
instrument falls outside its record. The older mandated sentence still scores in full for runs from
older builds. Looser wordings still take the 0.5 research-mode penalty.

## Step Completion

**Aim.** Deep research only. Checks that every step of the approved research plan carries its own
retrieved legal text into its own report. Catches a step whose tool calls returned legal text and then
reported nothing, most commonly because it hit a tool-call budget limit mid-step, while its sibling
steps report normally and nothing else in the harness would notice.

**A step stopped at the tool-call limit still fails, and the reason says so.**
Its retrieval was still lost from its report, so the verdict does not change,
but "the step threw its research away" and "the step was cut short" call for
different responses and only the second is LexChat's limit rather than the
model. LexChat now gives a halted step one tool-free round to write up what it
had retrieved, so a halted step that passes here is that write-up working and
one that fails is it not working. Read this alongside the dashboard's research
limit column rather than on its own.

**How.** Deterministic. For each step, if its own tool calls returned usable
`search_legislation_sections`/`get_legislation_text` content, or a definitions or made-under list,
but its own report cites none of the Acts
that retrieval actually returned (citing a sibling step's Act doesn't count, that step still has nothing
of its own to show for its retrieval), that step fails. Score is the fraction of steps that pass;
threshold is 1.0, so a single lost step fails the response.

## Report Integration

**Aim.** Deep research only. Checks that every step's own substantive finding survives into the final
answer, rather than being quietly dropped when the Manager condenses several step reports into one
response. Only checks steps that retrieved usable content and cited one of the Acts that retrieval
actually returned in their own report (citing a sibling step's Act doesn't count as a finding of its own);
a step that retrieved nothing, or retrieved and cited nothing of its own, has no finding of its own for
the final answer to have kept or dropped, so is not scored here.

**How.** Judge-based, one call per in-scope step. The judge is given that one step's own report and the
final answer, and labels the step `represented` or `dropped`, quoting the final answer to justify
`represented`. A quote that isn't really in the final answer is downgraded to dropped, so the judge can't
invent survival. Score is the fraction of in-scope steps represented; threshold is 1.0, so a fully dropped
step's finding fails the response.

**Why one call per step, not one batched call.** The first version asked about every in-scope step in a
single call alongside the full final answer. Re-run five times on the same stored response with no input
change, it scored 0.75, 0.5, 0.75, 1.0, 0.5, flagging a different step each time: too unstable to read
per record. Splitting into one narrower call per step, the same fix already used by Response Groundedness
(a direct verdict instead of a multi-item grade), scored the same three previously unstable responses
identically across five repeats each.

**Reading it.** Splitting the call reduced the noise but did not remove it. A single flagged step in
an otherwise represented run still occasionally flips between runs with no input change, and one such
flag was confirmed by hand to be wrong: the finding's exact figures and section number were present in
the final answer verbatim. Treat one flagged step as a prompt to re-run rather than a finding. A fully
dropped run, score 0.0 with several steps flagged, is not this kind of noise.

## Consistency (Cosine)

**Aim.** Measures how similar a response is to one or more other responses to the same question
(either repeat runs of the same LLM, for repeatability, or across LLMs, for cross-model agreement).
Catches a run that reaches a materially different answer to the same question rather than natural
phrasing variation.

**How.** Deterministic. Responses are vectorised with term frequency, no inverse document frequency,
and compared by cosine similarity against each reference response; the score is the mean similarity.
Any legislation.gov.uk section cited in one answer but not another is listed in the reason as a
diagnostic, but does not affect the score, since an agent searching a live corpus twice will legitimately
touch different secondary provisions each run.

LexChat's italic "Search scope" footer is removed from each answer before comparing. It is written by
LexChat's code, not the model, and is long and nearly identical from run to run, so left in it made two
different answers look alike.

Responses are only compared within the same chat mode. A deep research answer and an ordinary
research answer to the same question are not repeat runs of each other, so comparing them measures
the gap between the two modes rather than the model's repeatability. A mode with only one stored run
is not scored at all, since there is nothing to compare it against.

**Why.** Skipping IDF is deliberate: with only a handful of responses being compared, IDF would
down-weight exactly the shared legal terminology that should count most toward similarity. An earlier
version let a citation-set mismatch fail the run outright; that was removed because cosine similarity
alone already separated the two genuine contradictions found in testing (highest contradicting pair
0.418, lowest of everything else 0.512), and citation drift on its own wasn't a reliable failure signal.

## Citation Agreement

**Aim.** Checks how much of the legislation the authored reference answer expects is also cited by
the response being scored. Catches an answer that reaches a plausible-sounding conclusion without ever
citing the provisions the question actually turns on. It does not check whether the response uses those
citations correctly; that's Reference Answer Agreement's job.

**How.** Deterministic, no judge. Score is the fraction of the reference's expected citations the
response also cites. A link to the provision or to anything inside it counts: citing section 21(2)
cites section 21, and citing any section of an Act cites that Act. Citing section 3 when the reference
cites section 6 is a miss.

**What counts as expected depends on whether a lawyer has signed the reference off**, and so does the
threshold:

| Reference | Expected citations | Threshold |
| --- | --- | ---: |
| Draft, or a sign-off that has gone stale | Every legislation.gov.uk link in the reference answer | 0.3 |
| Signed off by a lawyer | Only the citations the lawyer marked `Required` in `review.json` | 0.5 |

The draft threshold is lower because a draft's expected list is whatever its author linked, background
provisions included, so most of a low score is noise. The signed-off threshold is 0.5 rather than 1.0
because reviewers mark most links `Required`, and a short answer is not expected to cite every one. At
1.0 every answer to every signed-off question failed, so the metric told nothing apart there.

A signed-off reference whose approved list is empty, meaning the lawyer decided no citation is
mandatory, is recorded as not measured rather than scored zero.

**The reason says where the blame lies**, in two plain sentences: "No search turned up: ..." and
"Turned up by a search but not cited: ...". The first means no tool call in the run surfaced that Act.
The second means one did, and the answer still does not cite it. Attribution is at Act level, the
granularity the retrieval tools are called at, and it never moves the score. `reports/attribution.py`
turns this into the flag above each question's metric rows, and carries the evidence for calling the
first case a search failure rather than a corpus gap.

**Attribution uses a narrower list than a draft's score does.** A draft's score counts every
legislation link in the reference answer, which includes the sources its author looked at and set
aside under "Identified but not retrieved". That noise is what the 0.3 threshold exists to absorb, and
it is tolerable in a score. It is not tolerable in a blame flag, which reads as a verdict, so attribution uses the answer's
own `sources_retrieved` instead. Without that field no attribution is produced at all: saying nothing
beats blaming a response for not citing something the author only skimmed.

**It reads the final answer, not the Worker's report.** Citations are extracted from `actual_output`,
so an answer that names the right Act in prose without a link scores nothing for it. That matters most
in conversational mode, where the Manager routinely drops the Worker's URLs, and it is why this metric
separates by chat mode more sharply than any other. Read a low score alongside Reference Links before
concluding the response missed the law.

**Why.** The draft threshold is set low, 0.3, deliberately: a draft reference cites everything its
author consulted while researching, including background provisions a good response doesn't need to
repeat. This metric, along with Reference Answer Agreement
and Claim Support, exists because `docs/eval-gap-analysis.md` identified that nothing in the harness
compared a response to a known-correct answer, only to its own retrieval.

## Reference Answer Agreement

**Aim.** Scores how many of a question's key legal statements, written alongside the reference
answer, the response also makes, and whether it contradicts any of them. This is the only metric that
compares a response against material a person researched, so it's the only one that can catch a
response that is faithful to its own retrieval but wrong about the law.

**How.** Judge-based, in two calls. The first gives the judge the statements (one to five of them,
most important first) as a fixed numbered list and asks it to label each `stated`, `contradicted`, or
`missing`, quoting the response. The second asks about nothing but contradictions. Score is the
fraction of statements stated. A contradiction found by either call fails the metric outright
regardless of score, since a confidently wrong statement of law is worse than an omitted one. Both
calls have to quote the response, and a quote that isn't really there is ignored, so an invented
quote can't fail a record.

**Why two calls.** Asked in the same breath as "does the response make this point", the contradiction
question loses: once the judge finds a passage stating the point, it labels the statement stated and
stops reading. On the answer that prompted this design, which recited a closed list of nine regulated
professions correctly and then, 65 lines later, called the reservation open ended, the single combined
call caught the contradiction 0 times in 5. A call asking only about contradictions caught it 4 times
in 5, with no false positives on two answers that get the same point right.

**Why.** The statement list is frozen rather than left for the judge to choose fresh each run because
letting the judge pick its own points was measured to disagree with itself 48% of the time run to run;
labelling a fixed list cut that to 7%.

**Reading it.** While a reference answer is still a draft, a contradiction means "contradicts the
draft reference", not "wrong in law"; only a lawyer's approval makes it the latter. Treat a flagged
contradiction as a prompt to read the two texts side by side. Across the 43 stored responses, 10 were
flagged; the four checked by hand were all genuine, including an answer that put a strategic plan in
the wrong Part of an Act and one that turned the s.29(3) purpose test into "purpose or effect".

**Known gap: there is no label for "must not appear".** The judge labels each statement stated,
contradicted or missing, so an answer that cites law it should have left out contradicts nothing and
loses no score. Writing the exclusion as a positive statement ("distinct from the UK Act") is a
partial workaround, since it only fires if the answer engages with the distinction rather than
quietly citing the wrong thing. Closing this properly is a metric change, so it goes through the
process in `CLAUDE.md`.

## Plan Coverage

**Aim.** Deep research only. Checks whether the plan a lawyer approves before research starts actually
sets out to answer the question, not whether the finished report does. Catches a plan that leaves out a
whole area of the question, so the gap is visible before any research time is spent, rather than only
once the final answer turns out to be missing something.

**How.** Judge-based. Uses the same fixed list of key statements written for Reference Answer Agreement,
rather than a second, separately written "ideal plan". For each statement, the judge checks whether any
step in the plan, if carried out, would find what that statement needs, and says which step. If the
judge names a step number that isn't actually in the plan, that counts as not covered rather than
covered, so a made-up answer can't pass. The score is simply the fraction of statements covered. Unlike
Reference Answer Agreement, there's no "contradicted" outcome, since a plan doesn't assert anything the
way a finished answer does, so only the score decides pass or fail.

**Why.** Reusing the existing statement list means this check adds no new lawyer work: the same
statements already used to judge the final answer are used to judge the plan for it. This is a
plan-quality check, not an execution check: it says nothing about whether the steps were actually
carried out well, or whether the final answer used what was found. It's also least informative on narrow
questions: any plan containing a step like "retrieve the text of section X" will score close to 1.0
against statements that are all facts inside that section, however many other steps the plan has and
regardless of how good the plan actually is. That's a known, accepted limitation rather than something
the design tries to fix, since it produces an uninformative score there rather than a wrong one.

## Claim Support

**Aim.** Scores what share of the legal claims in the Worker's report can be traced to the legal text
that run's own tool calls actually retrieved. This is the metric that catches the agent inventing legal
content, as distinct from citing an Act it never retrieved at all (Citation Grounding, which is
deterministic and Act-level).

**How.** Judge-based. The judge extracts up to 8 claims from the report and labels each `supported`
(with an exact quote from the retrieved text), `unsupported`, or `absence` (a claim that the law does
*not* do something). Every `supported` quote is checked in code against the actual retrieved text; a
quote that doesn't genuinely appear there is downgraded to unsupported, so the judge can't invent its
own evidence. Score is supported claims divided by scorable claims. Absence claims are counted and
reported but excluded from the denominator entirely, since nothing can be quoted to prove a law's
silence on something.

**The retrieved text includes the definitions and made-under lists.** Each defining clause, and each
instrument the made-under record lists with the record's count and coverage, is part of what the judge
quotes from, so "37 instruments were made under section 95" can be traced to the list that said so.

**So does the change record.** Each change a `get_legislation_changes` call returns becomes one line,
such as "Change record of asp/2014/12: ssi/2015/331 art. 3, coming into force: s. 1, s. 2", so a claim
that an instrument commenced or amended a provision can be traced to the record. LexChat's guidance
appended to the record is left out.

**So does text LexChat's code read from legislation.gov.uk** when the index lacks an instrument. The
code appends that text to a lookup or text-read result, and the model reads it, so a claim quoting it
is traceable. A note saying the read failed adds nothing.

**A halted run is not scored.** When LexChat's agent loop hits its ReAct turn cap it returns
`[Research halted: exceeded N tool-call steps]` in place of a report. Asked to find up to 8 claims in
that one sentence, the judge either invents them or errors, so a report holding nothing but the
sentinel is treated as no research output at all. A longer report that merely contains it had one
step halt among several and is still scored on the rest.

**Why.** Read this metric at the aggregate, not per record: across repeated sweeps of the same 22
stored responses, the mean moved by only 0.023, but individual records moved by 0.124 on average and
7 of 22 changed pass or fail. The judge re-selects which claims a report even contains on every run, and
no stored list can prevent that here, unlike Reference Answer Agreement, because the claims come from
freshly generated report text each time, not a fixed question.

## Response Groundedness

**Aim.** Checks whether the final answer delivered to the user is strictly grounded in the Worker's
research output, with no hallucinated or invented facts added when the Manager condenses it.

**How.** Two steps. If the final answer is a near-verbatim copy of the research output (similarity
ratio at or above 0.95), it passes automatically with no judge call, since grounding is then a provable
fact rather than a judgement. Anything reworded enough to matter goes to the judge, which returns a
binary pass or fail: fail on any unsupported claim or meaningful misrepresentation, pass on trivial
wording differences only. Score is 1.0 for pass, 0.0 for fail; no partial credit, so the metric's
average across responses is effectively a pass rate.

For a deep research run, the judge is also given the approved plan's scope note. Answers often say
something like "case law was excluded under the approved research plan", which is true but is stated
nowhere in the research output, so without the scope note the judge read it as an unsupported claim and
failed the whole answer. Runs without a plan pass no scope note and the prompt is unchanged.

LexChat's own search record is handled the same way. LexChat appends a `[SEARCH SCOPE]` block to the
research output and a matching italic footer to the answer. Both are removed before comparing, because
they are LexChat's words, not the model's, and the two versions differ in detail. The model often repeats
the block's caveats in the answer itself, for example "in-force status could not be verified". So the
judge is given the block's text as a record of the search: a caveat that matches it counts as grounded,
but the record is not a source for any legal point. Before this, correctly repeated caveats were failed as
unsupported. The record only supports caveats the answer includes. It is not a checklist, so an answer is
not failed for leaving one of its caveats out.

**LexChat's made-under list is removed from the answer before judging.** In conversational mode,
LexChat's code ends some answers with "Instruments made under the sections cited above, from the
made-under record". The Manager never wrote it and its facts come from the record, not the research
output, so the judge failed every answer that carried it. The same goes for the note LexChat's code
puts after an instrument the record says is revoked, such as "(recorded on legislation.gov.uk as revoked
in part, as checked on 2026-10-10)": the judge read the date as invented.

**The `<suggestions>` block never reaches this metric.** In conversational mode the Manager is told to
end every reply with a `<suggestions>` block of follow-up questions. LexChat strips it from the answer
before sending, so it is absent from `actual_output` and no metric sees it. Worth knowing before
investigating whether those suggested questions read as ungrounded claims: they cannot, they are not
there.

**A halted run is not scored here either**, for consistency with Claim Support and with the
already-existing empty-research case, though the trade-off differs: with no research output, an answer
making claims genuinely is ungrounded, so the judge's verdict was right. What is lost is that verdict;
what is gained is that the two metrics agree on what "no research" means. A halted run is still caught
by Tool Usage and Reference Links, which score it normally.

**Why.** The 0.95 near-verbatim threshold sits in an observed gap: one model that follows a "do not
condense" instruction literally measured 0.983-1.000 similarity, while one that habitually paraphrases
measured 0.095-0.763 on the same task. The verdict is asked for as a direct pass/fail rather than a 1-5
grade because the grade was unstable: re-run on the same stored responses, it moved on 6 of the 10
records that reached the judge, with a single grade step enough to flip the result.
