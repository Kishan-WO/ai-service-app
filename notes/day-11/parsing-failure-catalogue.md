# Day 11: parsing failure catalogue

What went wrong when I turned my files into searchable chunks, each with a real example. The commands and full outputs are in [`README.md`](README.md); the step numbers below point there.

**How far this goes:** my corpus (190 Markdown files, 10 research papers, a textbook from its saved text) and a few test files. The only PDF parser I tried is `pypdf` 6.21.4. For the difficult PDFs I compared **one page of each of 4 papers** with the page picture, so there are surely failures I have not seen. The two "scans" are pictures I made from a paper, not real scans. The "possible fix" column is an idea: **I have not tested any of them.**

## Overview
| # | Failure | Stage | Seen how often | State |
|---|---|---|---|---|
| 1 | A scanned page in a normal PDF disappears silently | parse, clean | 1 test file | open |
| 2 | A picture-only PDF fails without saying why | parse, clean | 1 test file | open |
| 3 | A table with wrapped cells becomes loose lines | parse | 1 page | open |
| 4 | Table headings on two lines are flattened | parse | 1 page | open |
| 5 | A footnote and a table cut a sentence in half | parse | 1 page | open |
| 6 | A word split across two pages stays broken | parse, clean | 1 page | open |
| 7 | Chart labels become text | parse | 37 of 90 lines on 1 page | open |
| 8 | Spaces disappear | parse | 3 of 4 pages; also seen in Step 19 | open |
| 9 | Small letters, footnote numbers, bold and formula symbols are lost or flattened | parse, clean | several pages | open |
| 10 | Words broken by a hyphen can be joined wrongly | clean | 267 joins without evidence | partly fixed |
| 11 | A table over 400 tokens is cut, without its header row | chunk | 4 of 28 Markdown tables | open |
| 12 | A chunk around a missing page claims that page | chunk | 1 test file | open |
| 13 | Code examples are not in the documentation's Markdown | source | 452 lines in 90 files | lines removed, code still missing |
| 14 | Website markup stays in the text | source, clean | see below | partly fixed |
| 15 | Titles taken from comments in code | parse | 14 old documents | fixed in the new pipeline (checked on 3) |
| 16 | A failed ingestion leaves an empty document | pipeline | any failing file | open |
| 17 | The PDF library wrote bytes of a file into the log | logging | 1 broken test file | fixed in the worker |
| 18 | The largest PDF is over the upload limit | upload | 1 book, 207.7 MB | not handled |

## PDF failures

### 1. A scanned page disappears silently (the most serious)
A three-page PDF: text, a **picture** of a page, text (Step 35).
```
parse: 3 page(s), characters per page [4736, 0, 4676]
clean: succeeded | empty_pages = [2]
'0.4235' (a value in that page's table)   in the result: False
```
- **Effect:** content is missing and the job succeeds. The cleaner notes the empty page, but no other code reads that note.
- **Possible fix:** save `empty_pages` and show a warning on the job; OCR for such pages.

### 2. A picture-only PDF fails without saying why
```
{"status": "failed", "stage": "clean", "error": "The file contains no text."}
```
- **Effect:** the message is true, but a user who sees text on the page cannot know that it is a picture. There is no OCR.
- **Possible fix:** say that the pages seem to be images; add OCR.

### 3. A table with wrapped cells becomes loose lines
`2601.05264`, page 11: 4 columns, 5 rows.
```
Primary
Limitations
DPR +
BART
Bi-encoder +
Seq2Seq
Fast retrieval,
composable
```
- **Effect:** nothing marks where a cell or row ends.
- **Possible fix:** a layout-aware parser (the plan names Docling and Unstructured).
- **For comparison:** tables with one line per row kept rows and numbers on the pages I checked. On `2605.13521` page 8, **all 12 rows matched** the picture.

### 4. Two-line headings are flattened
`2605.13521`, page 8:
```
Model Params Embed. MTEB Retr. MTEB Code MTEB Retr. LongEmbed RaR-b
(M) Size Multi (18) (12) En (10) (6) (17)
```
- **Effect:** the 8 values of a row are right, but which heading words belong to which value is not written down. The line between two groups of models is lost too.
- **Possible fix:** a layout-aware parser that returns the table as a table.

### 5. A footnote and a table cut a sentence
`2212.10528`, page 5:
```
shows the performance of our proposed reranker
2https://github.com/google-research/t5x
Model size MRR@10
... the table and its caption ...
on reranking BM25 top-1000 results.
```
- **Effect:** the sentence is split around unrelated text. The two columns themselves were read in the right order.

### 6. A word split across pages stays broken
`2407.20243`: page 7 ends with `incor-`; page 8 starts with charts and a table, and `poration ...` comes **1,512 characters** later.
- **Effect:** the word `incorporation` is in no chunk as a whole word at this place.
- **Why:** my cleaner joins hyphenated words only inside one page.

### 7. Chart labels become text
Same page: **37 of its 90 lines** are axis numbers, axis names and legends.
```
0.08
0.10Average distance
Matryoshka-Adaptor (b) nDCG@10 vs pairwise distance
```
- **Effect:** noise inside chunks that is embedded and paid for.

### 8. Spaces disappear
```
pas-sagesinnoisy-retrievalsettings[39]. However, thisintroducescomputational
RETRIEVALPERFORMANCE
trainDE0
```
In `2601.05264` I counted 20 pieces with 30 or more letters in a row.
- **Effect:** words that a reader or a search may not recognise. It happens when the PDF is read; cleaning does not repair it. The effect on search is not measured.

### 9. Formatting that carried meaning is lost
- `T5` with a small `3B` becomes `T53B`; `RoBERTa` with a small `Large` becomes `RoBERTaLarge`.
- Footnote numbers join words: `toolkit 3 with`.
- A caption says "the best performance is in **bold**"; bold does not exist in plain text.
- My cleaner removes unreadable font symbols (28 in the 10 papers), which are parts of formulas.

## Cleaning

### 10. Hyphens
My first rule produced `inthe`, `totext` and `Referencefree`. I added rules for compounds, a few prefixes and endings (Step 19). Of 1,377 broken words, **267 are still joined without any evidence** in the document (counted with cleaner version 1). In my sample most were ordinary words, but some compounds will be wrong. I did not check all 267.

## Chunking

### 11. A large table is cut without its header row
The Markdown chunker keeps a table whole if it fits in 400 tokens. Four of the 28 Markdown tables do not (Step 36). The later chunks are **rows without column names**.

What search did (Step 37), on all 4,683 chunks:
| Test | First 5 results | First 10 results (looked at afterwards) |
|---|---|---|
| FastAPI metadata, 7 rows in 3 chunks | none of the table | 5 rows; **both dictionary fields missing** |
| Pydantic migration, 22 rows in 3 chunks | none of the table | 8 rows; **0 of the 11 `pydantic.v1` rows** |

- **Effect:** an answer from those results would be confidently incomplete.
- **Possible fix:** repeat the header row in every part; return neighbouring chunks or the whole section (parent-child retrieval). Whether either helps is not tested.
- PDF tables have no structure the chunker knows, so it cannot keep them whole on purpose. The ones on my checked pages happened to stay in one chunk where I looked (`2212.10528`, `2605.13521`).

### 12. A chunk claims a page it does not have
In the mixed test file, one chunk joins the end of page 1 to the start of page 3 and is labelled **pages 1 to 3**.
- **Effect:** a citation can point to a page whose text was never read.
- **Possible fix:** do not let a chunk cross an empty page, or keep the list of pages instead of a range.

## The source files

### 13. Code examples are not there
FastAPI's Markdown only **includes** its code from other files:
```
{* ../../docs_src/first_steps/tutorial001.py *}
```
452 such lines in 90 of the loaded files (420 chunks before cleaning). I did not download the code. I remove the lines, as Day 9 did.
- **Effect:** sentences like "could look like this:" now have nothing after them.
- **Possible fix:** download the code at the same commit and put it in.

### 14. Website markup
Cleaner version 2 removes front matter, comments, include lines, `:::` lines and heading ids, and rewrites `///` and `!!!` boxes (Step 32). What can stay:
- `???` boxes (42 lines) and `////` tab lines (40), and one `!!!` line with an unquoted title.
- **HTML formatting tags** in terminal examples (`<font color=...>`, `<span style=...>`): **31 chunks in 13 documents**.
- A risk the other way: a line that is only `{* ... *}` or `--8<--` is removed from **any** uploaded Markdown file, and so is an HTML comment in an indented code block.

### 15. Titles taken from code comments
My Day 9 loader took the first line starting with `# ` without skipping code blocks, so `returns a dictionary:` became a title (14 old documents, Step 8). The new pipeline skips code blocks. I checked three of them in `Day 11 Corpus`: `Serialization`, `Models`, `Resolving Annotations`. I did not check the other 11 one by one.

## Around the parser

### 16. A failed ingestion leaves an empty document
`POST /ingest` creates the document before the worker reads the file. After the scan failed, the list had 235 documents instead of 234, and the new one had 0 characters (I deleted it).
- **Possible fix:** hide or mark documents whose first ingestion failed.

### 17. File content in the log
For a broken PDF, `pypdf` logged the **first bytes of the file**. With a real file that is a user's content. I silenced it in the worker (Step 24). **The old upload route in the API also uses `pypdf`, and I did not change its logging.**

### 18. A PDF too large to upload
The textbook is 207.7 MB; the limit is 20 MB. Splitting it by chapter did not help: 13 of 34 parts were still over the limit (Step 13). I reused the text that Day 9 had extracted, so **its extraction mistakes are still there, and its file is not stored**.

## What I take from this
- **A job that "succeeded" does not mean that everything was read.** The silent cases (1, 11, 12) worry me more than the ones that fail loudly (2).
- Look at real output next to the real page. My own `pdftotext` view showed empty cells in a table that has none (Step 35), and one of my display queries once made stored text look damaged when it was not (Step 33).
- Plain text extraction was good enough for prose and simple tables on the pages I checked, and not for wrapped tables, charts or scans.
