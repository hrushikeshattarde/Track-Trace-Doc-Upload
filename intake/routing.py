"""Message -> load, in tiers.

Measured on the 14 Sep 2026 survey (600 sampled messages, 126 of them carrying a document):

  tier 1  load number in this message's own subject      90%    free
  tier 2  the load an earlier message in the thread      most of the rest    free (a DB lookup)
          resolved to
  tier 3  reference numbers on the paper, scored         the remainder       costs one read
          against the load index
  tier 4  the unresolved list                            ~1% of threads      a person

Every document-bearing message in the sample was a reply inside an existing thread, which is why
tier 2 works at all: the chain almost always starts with Circle's own rate confirmation, and that
subject carries the load number.

The conflict rule is the important one. A thread binding is EVIDENCE, not truth: reps reuse an old
thread for a new load constantly, so a load number in this message's own subject always beats the
thread's binding, and the disagreement is flagged rather than silently resolved.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Circle load ids are 7 digits and currently run 24xxxxx-26xxxxx. The negative lookarounds stop a
# 7-digit run inside a longer number (a BOL or a phone number) from being read as a load.
LOAD_RE = re.compile(r"(?<!\d)(2[4-6]\d{5})(?!\d)")

TIER_SUBJECT = "subject"
TIER_THREAD = "thread"
TIER_PAPER = "paper"
TIER_UNRESOLVED = "unresolved"


@dataclass
class Routing:
    load_id: int | None
    tier: str
    subject_loads: list[int]
    conflict: bool = False
    reason: str = ""


def loads_in(text: str | None) -> list[int]:
    return [int(x) for x in LOAD_RE.findall(text or "")]


def resolve(subject: str | None, snippet: str | None, thread_load_id: int | None) -> Routing:
    """Tiers 1 and 2. Tier 3 needs the document read, so ingest calls resolve_from_paper() after.

    A subject naming several loads is ambiguous, not decisive: consolidated or batched mail does
    happen, and guessing the first one would file a POD against the wrong load. Those go to the
    thread binding, then to the list.
    """
    subject_loads = loads_in(subject)

    if len(subject_loads) == 1:
        lid = subject_loads[0]
        if thread_load_id is not None and thread_load_id != lid:
            # The reply carries its own load number and it is not the thread's. Trust the message.
            return Routing(lid, TIER_SUBJECT, subject_loads, conflict=True,
                           reason=f"subject says {lid}, thread was bound to {thread_load_id}: trusting the message")
        return Routing(lid, TIER_SUBJECT, subject_loads)

    if thread_load_id is not None:
        why = "no load number in this subject" if not subject_loads else \
              f"subject names {len(subject_loads)} loads ({subject_loads}); using the thread binding"
        return Routing(thread_load_id, TIER_THREAD, subject_loads, reason=why)

    # Last free signal: the first lines of the body. Weaker than the subject - a quoted earlier
    # message can carry someone else's load number - so it only applies when nothing else did.
    body_loads = loads_in(snippet)
    if len(set(body_loads)) == 1:
        return Routing(body_loads[0], TIER_THREAD, subject_loads, reason="load number in the message body")

    if subject_loads:
        return Routing(None, TIER_UNRESOLVED, subject_loads,
                       reason=f"subject names {len(subject_loads)} loads and the thread is unbound: ambiguous")
    return Routing(None, TIER_UNRESOLVED, [], reason="no load number in the subject, thread or body")


def resolve_from_paper(extraction, index) -> Routing:
    """Tier 3: let the matcher decide from the reference numbers on the document itself.

    Reuses pod_intake.matcher unchanged - this is exactly what reference_exact and
    reference_one_edit were written for. In the sample, the messages that reached this tier were
    airline and forwarder paperwork (an AIRLINE DELIVERY NOTE, a DHL delivery order, a Ryder
    chain): no load number anywhere in the mail, but a reference number on the page.

    Only a High-tier match routes automatically. Medium and Low go to a person: filing a POD
    against the wrong load is worse than filing it late.
    """
    from pod_intake.matcher import decide, score_candidates

    result = decide(score_candidates(extraction, index))
    if result.tier == "High" and result.load_id:
        return Routing(result.load_id, TIER_PAPER, [], reason=f"matched on the paper: {result.reason}")
    return Routing(None, TIER_UNRESOLVED, [],
                   reason=f"paper match was {result.tier}, not High: {result.reason}")


def sender_domain(from_header: str) -> str:
    from email.utils import parseaddr
    addr = (parseaddr(from_header or "")[1] or "").lower()
    return addr.split("@")[-1] if "@" in addr else ""


def original_sender(headers: dict[str, str], group: str | None) -> str:
    """Google Groups rewrites From to the group address and hides the real sender in
    X-Original-Sender. Without this every external carrier looks like an internal message.

    No group is a legitimate configuration - a mailbox that is not behind a Google Group has nothing
    to unwrap - and it must not be a crash. It was one until 22 Sep 2026: `intake cycle` passed
    group=None and every message in the collect step died on `None.lower()`, which the step wrapper
    reported as one failed step rather than 500 lost messages.
    """
    frm = headers.get("from", "")
    if group and group.lower() in frm.lower() and headers.get("x-original-sender"):
        return headers["x-original-sender"]
    return frm
