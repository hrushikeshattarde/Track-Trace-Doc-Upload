# Driver Document Intake — prototype

A small, runnable version of PRD Section 18: normalize a POD/BOL file, read it with Claude into
structured JSON, match it to a load using the PRD Section 6 rules, and, when the rules cannot settle
it, ask a stronger model to adjudicate. Nothing here writes to TransportPro.

```
samples/     two real PODs (image-only PDFs) and one raw BOL photo (JPEG)
index/       loads.json — snapshot of 9 active loads exported read-only from TransportPro (stands in for FR-18)
fixtures/    hand-transcribed extractions so the matcher can be demonstrated with no API key
pod_intake/  normalize.py · schema.py · reader.py · index.py · matcher.py
run.py       command line
out/         per-document extraction + match reports (JSON)
```

## Setup

The project has its own virtualenv in `.venv` with everything the scripts need: the reader (anthropic, pymupdf,
pydantic, openai), the paystatus bot's TransportPro client (pydantic-settings, tzdata) and Gmail delegation
(google-auth). Credentials live in a local `.env` (git-ignored): copy `.env.example` to `.env` and fill it. Every
script loads that file first and falls back to the payment-bot checkout's `.env` for anything missing, so the old
two-interpreter routine is no longer needed. The scripts never print `.env` values. Precedence: shell variables win for
the PAYBOT_* settings; the `.env` wins for the model variables (ANTHROPIC_*, OPENROUTER_API_KEY), because tools that
launch the scripts inject their own Anthropic base URL and the bearer token would otherwise go to the wrong host
(seen 14 Sep 2026: 401 "invalid x-api-key" from api.anthropic.com). A blank value in `.env` means "not set".

```powershell
cd C:\Users\hrushikesh.attarde_c\Desktop\Projects\pod-intake-prototype
python -m venv .venv                                   # already done on 14 Sep 2026
.venv\Scripts\python.exe -m pip install -r requirements.txt pydantic-settings tzdata google-auth
copy .env.example .env                                 # then fill the values
.venv\Scripts\python.exe readiness.py --loads 2575200 --days 7 --read --model anthropic/claude-opus-5
```

## Run

```powershell
# 1. No model: check that the PDFs render and fingerprint correctly
python run.py --normalize-only samples\*.pdf

# 2. No model: run the matcher on saved extractions (works without a key)
python run.py --from-json fixtures\*.json

# 3. Live: read with Claude, match, adjudicate the Medium tier
python run.py samples\*.pdf

# Variants
python run.py samples\*.pdf --model claude-haiku-4-5          # PRD cascade: cheap reader, Opus 5 adjudicator
python run.py samples\*.pdf --no-adjudicate                    # reader + rules only
python run.py samples\*.pdf --sender-phone 917-559-7279        # simulate an SMS sender (identity signal)
python run.py samples\*.pdf --sender-email ross@concordeast.com --channel email
python run.py samples\circle-2577353-bol.jpg --ignore-filename          # paper-only: no load number in the name
python run.py samples\circle-2577353-bol.jpg --subject "RE: BOL load 2577353"   # simulate an email subject
```

The file name and, for email, the subject and body are matching signals too: a load number found there is
strong evidence, exactly as in PRD Section 11. `--ignore-filename` turns that off so you can see how the
rules and the adjudicator behave on the paper alone.

Each run prints, per document: the extracted numbers, parties, and signatures; every candidate load
with its signals and strengths; the tier (High / Medium / Low); the TransportPro document type; the
File History comment the service would write; and the estimated model spend. The same data lands in
`out/<file>.match.json`.

## What it does and does not do

- **Does:** page rendering at 1,568 px, difference-hash fingerprints, text-layer capture (the driver-app
  time stamps), structured extraction with a cached system prompt, closed-set matching with one-digit
  tolerance on long numbers, tiering, type classification from trip stage plus signatures, adjudication
  with the top three candidates, cost estimate from list prices.
- **Does not:** deskew, talk to TransportPro live (the index is a JSON snapshot), build the merged PDF,
  upload, write dispatch notes, or run the review queue. Those are the production pieces around this core.

## Customer-specific document requirements

`index/customer_requirements_source.xlsx` is the Account Managers' "Accounts / Customers Extra Requirements"
workbook: one sheet per Track & Trace pod with the columns Pod/Team, AM, Customer Name, BOL, POD, Notes, plus
SOP sheets and a DetentionLayovers sheet. Build the typed rules from it, then every run checks each filed
document against the load's customer:

```powershell
python run.py --build-rules index\customer_requirements_source.xlsx     # -> index\customer_requirements.json
python run.py samples\*                                                 # verdict printed per document
python run.py --from-json fixtures\pod-2572081.extraction.json --customer "Win-Holt Equipment"   # what-if
```

`pod_intake/requirements.py` reads every sheet with the standard header, keeps the BOL/POD booleans as they are,
turns the free-text Notes into typed flags by keyword (pages required, signatures, seal, stamp, freight photos,
in/out times, BOL-before-leaving-shipper, POD-before-deliver-out, upload required, address match, deliver-out
with detention, document-type overrides such as Disney's), and keeps the original note verbatim for review.
`check_document()` evaluates a filed document against those rules and returns pass / fail / unknown per rule and a
deliver-out readiness verdict, which is appended to the File History comment and saved in `out/*.match.json`.

The keyword pass is a first draft for AM review, not a source of truth; the workbook stays the source of truth.
Sheet customer names are matched to TransportPro customers by name (`tpro_customers` in the rules JSON); adding
a TransportPro customer code column to the workbook would make that exact.

**Pod mapping.** `index/pod_terminals.json` holds the TransportPro terminal IDs of the Track & Trace pods (read
from the Load Management filter) and maps each workbook sheet to its terminal(s). `--build-rules` merges it into
the rules JSON. At run time the load's `terminal_id` (TransportPro `assignedTerminal`) resolves to a pod and its
sheet; a customer rule from the load's own pod wins, a rule from another pod's sheet is used only as a fallback
and is flagged `cross_pod`. Pods without a sheet (Lomont, Panama, Paper) and the unmapped "FWO POD" sheet are
listed in the file so someone can fill them in.

## Surveying the ratecon@ group mail

`mail_survey.py` reads a Google Group's traffic the same way the paystatus bot does: the service account in the
payment-bot repo impersonates the configured member user (domain-wide delegation, read-only Gmail scope) and
searches `to:<group>`. It stores headers and attachment names only, never bodies, and writes a Markdown summary
plus JSON to `out/mail-survey/`. Run it with the payment-bot virtualenv, which has google-auth:

```powershell
C:\Users\hrushikesh.attarde_c\Desktop\payment-bot-intake-policies-and-hardening\.venv\Scripts\python.exe mail_survey.py --group ratecon@circledelivers.com --days 7 --max 840
```

It counts every day in the window from message ids (cheap), samples evenly within each day, decodes
TransportPro file names (`<fileId>_<fileTypeId>.pdf`, 23 = Carrier Rate Agreement), removes repeated signature
images, and reports which candidate Gmail filters keep which share of the document-bearing mail.

## Document readiness job

`readiness.py` joins the three sources on the load number and produces one row per load: stage of the truck,
document status, pod (from the terminal), customer, what is filed in File History, what is sitting in the
ratecon@ thread unfiled, the email-to-file lag, dashboard flags, and the customer's verification steps from
the requirements workbook evaluated as far as metadata allows (the rest become "verify on the document"
items, which `--read` can fill by running the reader on the email attachments).

Scope: only Priority / OP8 loads are worked, in every mode. The service level comes from `dashboard_filter` in
`index/pod_terminals.json`; a load met through the mailbox or `--loads` that carries another level (Flexible / FCFS,
Firm Appointment, High Priority) is listed as `out_of_scope` and costs no further TransportPro or Gmail calls.
`--service-level all` disables the rule; a load with no service level on its stops is kept.

States, in work-queue order: `email_unfiled` (a document is in the thread and nothing of the type the truck's
stage calls for is filed: BOL until the truck reaches the consignee, POD after), `sms_possible_doc` (driver texted
about paperwork; photo may be in the SMS window), `missing_no_source` (nothing anywhere; ask the driver),
`filed_status_pending` (filed but status still Waiting; when the email arrived after a filing of the expected type
the row says the email copy is likely a duplicate), `review`, `not_yet_due`, `complete`. The SMS column shows
inbound paperwork-related texts / outbound requests per dispatch (text only; the API exposes no attachments).

The Markdown report opens with headline numbers (Waiting loads that already have paperwork somewhere, the bot's
work queue, filed-but-Waiting with the Driver Supplied BOL share, median email-to-file lag) and a per-pod state
table, so each pod lead can find their own queue. Loads on terminals that are not pods (Pittsburgh Office, Sales
Team) are named from `non_pod_terminals` in `index/pod_terminals.json` and have no requirements sheet.

TransportPro endpoints used (from the Public API Postman collection): `GET /load/search` (terminalId + pickupDateStart/End;
a date range is mandatory; paging is `page=N`, zero-based; the server-side documentStatus filter matches nothing for the
dashboard's own wording and everything for any other, so document status is filtered from the rows), `GET /load/{id}` (fallback
`GET /voiceai/load/{id}`), `GET /dispatch/search?loadId=`, `GET /files/search?recordType=loads&recordId=`,
`GET /dispatch/{id}/getTextMessages`, `GET /load/missing_documents`. All GET.

```powershell
# offline, no credentials: TransportPro answers from a fixture, emails from the survey JSON
python readiness.py --from-survey out\mail-survey\ratecon_20260914.json --emails-from-survey --tpro-fixture out\readiness\tpro_fixture_20260914.json

# live (payment-bot venv: TransportPro client + Gmail delegation), every load that received a document by email
C:\Users\hrushikesh.attarde_c\Desktop\payment-bot-intake-policies-and-hardening\.venv\Scripts\python.exe readiness.py --from-survey out\mail-survey\ratecon_20260914.json --days 14

# live, the Load Management filter reproduced through the API. The filter, read from the page's own filter panel on
# 14 Sep 2026 and saved as dashboard_filter in index/pod_terminals.json, is: the 16 ticked pod terminals, load status
# Dispatched (the header's In Transit), service level Priority / OP8. The job runs one /load/search per pod per 45-day
# pickup window back to ~a year (the API needs a date range, refuses wide or very old ones; windows are bisected on 400),
# drops cancelled loads and other service levels, then checks the loads still Waiting for Documents against email.
# Verified 14 Sep 2026: 539 loads in the view, 539 on the dashboard header; the 179 extra loads the API returned before the
# service-level rule were all Flexible / FCFS or Firm Appointment. --count-only prints the per-pod counts and stops.
...\python.exe readiness.py --from-dashboard --count-only
...\python.exe readiness.py --from-dashboard --days 14 --max 500
...\python.exe readiness.py --from-dashboard --terminals 1160 --dashboard-days 3 --days 7 --read --model anthropic/claude-opus-5

# live, TransportPro's own missing-documents list as the load set. Page 0 of that list is the OLDEST 200 of ~8,000
# open loads (ids ascending), i.e. the aged backlog, not today's work. --newest probes for a page parameter
# (currentPage / page / offset) and reads the last page(s); it prints which parameter worked or that none did.
...\python.exe readiness.py --from-missing-documents --newest --md-pages 2 --max 100 --days 14

# live, specific loads, with the reader verifying the unfiled attachments
...\python.exe readiness.py --loads 2573804,2578457 --read --model anthropic/claude-opus-5
```

`--read` needs the model SDKs (anthropic, pymupdf). With the project `.venv` they are present and the job calls
`read_attachments.read_files()` in-process; otherwise it looks for another interpreter that has them (`python` on PATH,
or `--reader-python <path>`) and runs `read_attachments.py` there. Both routes run the same function, so type, rule
verdict, not-a-document handling, and the seal reconciliation are identical either way (until 14 Sep 2026 the
in-process route was a separate copy that lacked the seal check). Each load's row keeps `reader_files`: per file
the numbers with their kinds, notes, seals and cost, for audit and labelling. The reader runs
one process per load; the OpenRouter/Anthropic environment variables are inherited, so set them in the same
PowerShell window first (see the OpenRouter section below); the job checks once and skips the reader with a hint
if they are missing. Per load it keeps one copy of each attachment (replies quote the earlier messages, so the same
photo repeats through a chain), ignores anything under 40 KB, downloads the rest largest first, drops images that are
smaller than 300 px on a side or 150k px in area (signatures and logos, judged from the file header), and reads at
most six per load. Attachments are saved under `out/readiness/attachments/<load>/`; the dropped images go to a
`skipped/` subfolder with their pixel size in the name, and the report lists them, so the thresholds can be tuned.

**Measured on 14 Sep 2026.** The reader was run over the 20 loads whose ratecon@ thread held an unfiled attachment
(36 files, $1.68 with Opus 5 as the reader). 9 loads had a BOL or POD to file; 1 held only the BOL that was already in
File History (the POD still missing); 1 held freight photos only; 9 held nothing but signature graphics, phone
screenshots (tracking apps, an email), a TransportPro rate confirmation, or photos of a driver's licence. So the
email work queue is about half the size the attachment count suggests, and the intake bot must never file the
personal-ID photos that drivers send in the same thread. The report now prints this split in the headline numbers and
a per-load `reader:` outcome in the Action column.

**Email threads without a document.** The first thread the reader was pointed at (Volvo load 2573804, nine messages from
the customer, Circle and the carrier) carried 111 image parts and not one document: every image was an email
signature, letterhead or certification-logo graphic, quoted again in each reply. Gmail's `has:attachment` and the
survey's attachment count therefore over-state the document-bearing mail, and the `email_unfiled` queue inherits the
same false positives. The job now (1) keeps one copy per image, (2) drops small and banner-shaped images by pixel
size, and (3) treats a reader result of `other` / `unknown` as "not a freight document" rather than filing it as a
Bill Of Lading. The intake bot needs the same three steps before a message counts as a document.

**iPhone photos.** Carriers also send HEIC files (load 2579013, 15 Sep 2026: three of them, and the run's only reader
error). PyMuPDF cannot open HEIC; the normaliser now converts it to JPEG with pillow-heif before rendering.

**Paperwork that is neither BOL nor POD.** Load 2558742 (14 Sep 2026, Spindrift, McAllen TX to Winston-Salem NC,
frozen yuzu juice) came with a Citrojugo Certificate of Analysis and a packing list, which the reader dismissed as
"not a freight document". TransportPro's document-type list (145 types) has "Shipping Documents" (369) for exactly
this, plus "Customs Document" (336), "PO" (137) and "Other" (123). The reader now has a `shipping_document` type
(packing lists, CofAs, commercial or customs invoices, temperature-recorder sheets) that files as Shipping Documents
and still transcribes lot, PO and order numbers. "other" is reserved for things that are not shipment paperwork at
all: signatures, logos, screenshots, registration cards, personal ID.

**The active dispatch, and time as a POD test.** Load 2576660 (14 Sep 2026, Expeditors LA, DM World, Carson CA to
North East MD): the load's first dispatch was a cancelled carrier, so the job read the wrong status (Dispatched instead
of At Consignee), the wrong text thread and the wrong trailer number. `active_first()` now puts cancelled dispatches
last and newest first. The same thread held the Expeditors house bill scanned on pickup day, signed "Received by" at
the origin, which the reader called a POD; with the truck now at the consignee the stage rule would have accepted it.
A file emailed more than 24 h before the delivery appointment is therefore never a POD (`--pod-not-before`, per-file
email times). Macropoint's "before departing the delivery" text now counts as arrival. Seal reconciliation treats any
paper document as the BOL side, ignores lot codes such as 720210122-03, and reports a second seal that is written on the
BOL under another label (Uline #).

**Consolidated packets.** Load 2578456 (14 Sep 2026, Davco cross-dock in Linden NJ to Thermo Fisher Florence KY) arrived as
one 11-page PDF holding seven different upstream shippers' BOLs (Decon, Millipore Sigma, Avantor, Thermo Fisher Lexington,
Eppendorf, Ricca, Dynalon), each with its own Fisher PO. The load's own reference, PR4260634, appears once, on page 8, and
no page carries the driver's signature or a Davco outbound BOL. Reading all 11 pages in one call cost $0.31 and the
returned number list did not include the page-8 reference, so for packets longer than a few pages the reader should run
per page and merge the numbers before matching. Matching such loads on paper alone needs the whole packet's numbers, not
the first page's.

**Nothing is a POD before the truck reaches the consignee.** Load 2576409 (14 Sep 2026): the driver signed a Menzies
"CFS DELIVERY" release receipt when collecting air freight at DFW, and the reader called it a proof of delivery, which
would have satisfied Expeditors' "POD before delivering out" rule while the truck was still loaded. `classify_type`
now overrules the reader whenever the dispatch is Planned / Dispatched / At Shipper / Loaded / In Transit: a signed form
there is pickup paperwork and files as Bill Of Lading. The prompt also says a photo or screenshot of a document page is
that document (a delivery-order page had been dismissed as "other"), and Macropoint's delivery text now advances
the stage like its loaded text does, so a stale dispatch status does not block a real POD.

**Driver text photos are already captured (OQ-1).** On load 2578982 (14 Sep 2026) the driver answered Macropoint's
"send a picture of the BOL" text with two MMS photos. The API shows them as inbound texts with `message: null`
(no media field), and TransportPro filed each image one second later as "Driver Supplied BOL" (type 363, comment
"Driver Supplied Image - <load>", uploader System Admin). So the SMS channel's pictures do reach File History
today, under a type that does not clear "Waiting for Documents"; the bot's job on that channel is to read and
re-type them, not to catch them. The readiness job now counts these null texts as pictures (SMS column shows
`(n pic)`) and marks 363 files created within 10 s of one as auto-filed from MMS. `--read-all` reads the email
attachments of filed loads too, to tell a duplicate from a POD.

**What clears "Waiting for Documents" (OQ-3).** Three loads read on 14 Sep 2026 point at timing, not document type:
on the cleared load the BOL was filed 34 s *after* the dispatch was marked Delivered and the status flipped the same
second; on the two stuck loads the files (Bill Of Lading and Driver Supplied BOL alike) were uploaded *before* the
Delivered mark, and marking Delivered afterwards did not recompute the status. The job tests this on every
filed-but-Waiting load (file `dateCreated` vs. the Delivered dispatch's `lastUpdated`) and reports the count in the
headline numbers. If it holds, the intake bot must file the POD after the load is Delivered, or re-trigger the
status once it is, and BOLs filed at pickup will never clear the status by themselves.

Output: `out\readiness\readiness_<stamp>.md` (work queue + verification checklist), `.csv`, `.json`.
Read-only: nothing is uploaded and no status is changed.

## The intake service (`intake/`)

The prototype recomputes the world on every run, so anything a run does not reach is not deferred,
it is forgotten: `readiness.py` caps the load set at `--max 150` against a 539-load dashboard (and
`sorted(..., reverse=True)` keeps the *newest* 150, so the oldest and most stuck are the ones never
checked), and its attachment de-duplication lives in a Python set scoped to one load in one run.
`intake/` replaces both with state that survives a restart.

```
intake/db.py       the ledger: cursor, thread, message, part, attachment, filing, unresolved, load
intake/gmail.py    delegated Gmail with a history cursor; self-contained (no payment-bot path)
intake/filters.py  the free filters: size, rate-con filename, pixel geometry
intake/routing.py  message -> load in tiers: subject, thread binding, paper, unresolved list
intake/ingest.py   Loop A: one pass over everything that arrived since the cursor
tests/test_intake.py   offline checks; no network, no model, no credentials
```

```powershell
.venv\Scripts\python.exe tests\test_intake.py                 # 34 checks, offline
.venv\Scripts\python.exe -m intake init
.venv\Scripts\python.exe -m intake sync                       # Loop A, no model spend
.venv\Scripts\python.exe -m intake sync --read --model claude-opus-5
.venv\Scripts\python.exe -m intake status
.venv\Scripts\python.exe -m intake unresolved
.venv\Scripts\python.exe -m intake load 2560078
```

**Two rules carry the design.** `part` is one row per *occurrence* and `attachment` one row per
*unique SHA-256*, so the ledger proves the de-duplication worked instead of hoping it did. And
`attachment.extraction_json` caches what is on the paper, which is a function of the bytes alone;
the TransportPro document type is never cached, because `classify_type` depends on the dispatch
stage, the delivery appointment and the time the file was emailed, all of which move. Cache the
reading, recompute the filing.

**The cursor is written after the batch, never before.** A cursor advanced early is silent data
loss and the one failure nothing else catches. Replay is free instead: the `message_id` primary key
and `UNIQUE(load_id, sha256)` make a redone batch a no-op, which is also what makes at-least-once
Pub/Sub delivery safe. On a first run, or after the cursor outlives Gmail's ~1-week history
retention (`CursorTooOld`), it falls back to a `newer_than:Nd` search and re-seeds from
`getProfile` — reading the profile *before* the search, so anything arriving between the two calls
still lands after the new cursor.

**Thread bindings are evidence, not truth.** A load number in a message's own subject always beats
the thread's binding, because reps reuse an old thread for a new load constantly; the disagreement
sets `thread.conflict_flag` rather than being silently resolved. A subject naming several loads is
ambiguous, not decisive, and goes to the unresolved list rather than guessing the first one.

**Measured on the live mailbox, 15 Sep 2026.** First pass: 120 messages, 100% routed by subject,
185 Gmail calls, $0. Second pass off the cursor: 14 new messages in 20 calls, and 5 attachment
occurrences collapsed to 1 new unique file — 4 reads avoided that the prototype would have paid
for. Load 2560078 alone carried 4 documents each sent twice (carrier, then the rep forwarding them
back): 8 occurrences, 4 unique files. Nothing is uploaded and no status is changed; the pass ends
by setting each load's `next_check_at`, which is the hand-off to the load loop (not yet built).

## Scoring the reader against a person

`evaluate.py template` writes `out/readiness/labels_<stamp>.csv` with one row per file the last `--read` run read or
skipped, pre-filled with the agent's verdict. A Track & Trace person fills three columns per file: is it a freight
document (Y/N), its type (bol / pod / photo / other), and whether they would have filed it. `evaluate.py score
<csv>` then prints document-detection precision and recall (with the misses listed), BOL-vs-POD type accuracy, per-load
agreement on "something to file", and the model spend. Those are the numbers to improve run over run.

```powershell
python evaluate.py template
python evaluate.py score labels_20260914_1200.csv
```

## Comparing models

Send each model's run to its own folder, then line them up:

```powershell
python run.py samples\* --model anthropic/claude-opus-5   --adjudicator anthropic/claude-opus-5 --out out\opus-5
python run.py samples\* --model anthropic/claude-sonnet-5 --adjudicator anthropic/claude-opus-5 --out out\sonnet-5
python compare.py out\opus-5 out\sonnet-5 --save compare.md
```

`compare.py` pairs documents by file name and shows tier, load, type, signal counts, numbers extracted,
signature and time detection, legibility, tokens, and cost per run, plus any number one run found that the
other missed, and totals. Keep the adjudicator fixed so the comparison isolates the reader.

## Comparing against GPT or other non-Claude models

Any OpenRouter slug that is not a Claude model (for example `openai/...`, `google/...`) is routed through
`pod_intake/reader_openai.py`: the same prompts, schemas, and matcher, sent via the OpenAI-compatible API
at `https://openrouter.ai/api/v1`. It reuses the key already in `ANTHROPIC_AUTH_TOKEN`, or `OPENROUTER_API_KEY`.

```powershell
python run.py samples\* --model openai/<current-gpt-slug> --adjudicator anthropic/claude-opus-5 --out out\gpt
python compare.py out\opus-5 out\sonnet-5 out\gpt --save compare.md
```

Take the exact GPT slug from openrouter.ai/models. Cost for these runs comes from OpenRouter's own usage
accounting (requested per call), so the compare table is apples to apples. Two fairness notes: the JSON is
requested in JSON mode with the schema in the prompt rather than a server-enforced schema, and the reader
prompt was written and tuned on Claude, so a GPT result reflects an untuned prompt.

## Using an OpenRouter key instead of an Anthropic key

OpenRouter exposes an Anthropic-compatible endpoint, so the same code runs through it via environment
variables. The base URL is `https://openrouter.ai/api` (no `/v1`), the key goes in the auth-token variable,
and the API-key variable must be explicitly empty:

```powershell
$env:ANTHROPIC_BASE_URL  = "https://openrouter.ai/api"
$env:ANTHROPIC_AUTH_TOKEN = "sk-or-..."
$env:ANTHROPIC_API_KEY   = ""
python run.py samples\*.pdf --model anthropic/claude-opus-5 --adjudicator anthropic/claude-opus-5
```

Use OpenRouter's model slug (provider-prefixed, listed at openrouter.ai/models); if the slug you try is not
found, pick the closest Claude model there. Two caveats: structured outputs may not pass through the proxy,
in which case the reader automatically retries with prompt-instructed JSON and says so; and prompt caching
may not apply, so per-call cost can be a little higher than the PRD estimate. Both are prototype-only
concerns. For production the PRD's OQ-10 chooses between the Anthropic API and Microsoft Foundry, not a
third-party proxy, because customer documents and driver phone numbers would otherwise transit that proxy.

## Models

Default reader and adjudicator are `claude-opus-5`. The PRD's cost model assumes `claude-haiku-4-5` as the
reader with `claude-opus-5` adjudicating only the Medium tier; pass `--model claude-haiku-4-5` to run that
configuration. Prices in `reader.py` are list prices from the reference used on 11 Sep 2026; verify before budgeting.
