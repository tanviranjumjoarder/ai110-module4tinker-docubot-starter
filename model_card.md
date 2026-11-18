# DocuBot Model Card

This is my write-up of DocuBot after building the retrieval part myself and trying all three modes:

1. Naive LLM over full docs
2. Retrieval only
3. RAG (retrieval plus LLM)

---

## 1. System Overview

**What is DocuBot trying to do?**

DocuBot answers developer questions about one small project by looking at that project's own docs folder. The point is to give answers that come from the docs, and to say "I don't know" when the docs don't cover something, instead of letting a model guess.

**What inputs does DocuBot take?**

- A question typed by the developer (or the 8 sample questions in `dataset.py`).
- The `.md` and `.txt` files in `docs/` (right now: `API_REFERENCE.md`, `AUTH.md`, `DATABASE.md`, `SETUP.md`).
- A `GEMINI_API_KEY` in `.env`. This is only needed for modes 1 and 3.

**What outputs does DocuBot produce?**

- Mode 1: a written answer from Gemini.
- Mode 2: up to 3 doc sections, each labeled with the file it came from.
- Mode 3: a short written answer from Gemini that is supposed to use only the retrieved sections and name the files it used.
- In modes 2 and 3, the refusal "I do not know based on the docs I have." when nothing in the docs is a good enough match.

---

## 2. Retrieval Design

**How does your retrieval system work?**

- **Breaking docs into pieces.** I split every file at its markdown headings, so one piece is one section, like `## Token Generation` or `### GET /api/users`. If a heading has nothing under it (like `## User Data Endpoints`), I glue it onto the next section so a bare heading is never returned alone. The 4 files turn into 36 sections.
- **Cleaning up words.** Text is lowercased and split into plain words. Underscores split words too, so `generate_access_token` becomes `generate`, `access`, `token`. A tiny stemmer cuts common endings so `users` matches `user`, `generated` matches `generation`, and `connect` matches `connection`.
- **The index.** An inverted index maps each word to the sections that contain it. When a question comes in, I only score sections that share at least one word with it.
- **Scoring.** I drop filler words from the question ("where", "is", "the", "how", and so on). The score is how many of the remaining question words appear in the section. Each word counts once, so a long section that says "token" ten times doesn't beat a short one that matches more of the question.
- **Picking the top 3.** Sort by score. Ties go to the section that mentions the question words more often, and after that to the shorter section.

**What tradeoffs did you make?**

- I picked plain keyword matching because I can explain every result by looking at which words matched. The downside is it has no idea about meaning. "How long does a token last?" only works because the docs happen to say "how long". If they had said "expires after", it would miss.
- Heading-sized sections are much better than whole files (one question went from 398 printed lines to about 40). Some sections are still too broad, though. The intro paragraph of a file often matches lots of question words but holds no actual answer.
- My first version, which returned whole files, already scored 0.88 on `evaluation.py`. The section version also scores 0.88. That number only checks whether the right file shows up, so it couldn't see the real improvement, which was returning the right part of the file. That taught me the metric is measuring something different from what I care about.
- The 0.88 is the max possible here. The payment question has no correct file, so the script always counts it as a miss, even when DocuBot correctly refuses.

---

## 3. Use of the LLM (Gemini)

**When does DocuBot call the LLM and when does it not?**

- **Naive LLM mode:** calls Gemini once per question. I was surprised to find that the starter prompt doesn't actually include the docs. `naive_answer_over_full_docs` receives `all_text` and then ignores it, so Gemini just answers from general knowledge. That's why this mode can sound sure of itself while knowing nothing about this project.
- **Retrieval only mode:** never calls Gemini. It prints the sections my retriever found, or the refusal.
- **RAG mode:** runs my retriever first. If nothing passes the guardrail, it refuses without calling Gemini at all. Otherwise it sends Gemini only the top 3 sections and the question.

**What instructions do you give the LLM to keep it grounded?**

The RAG prompt in `llm_client.py` tells Gemini to:

- answer using only the snippets it was given,
- not invent functions, endpoints, or config values,
- reply exactly "I do not know based on the docs I have." if the snippets aren't enough,
- say which files it relied on when it does answer.

---

## 4. Experiments and Comparisons

I ran the same questions, with the same wording, in all three modes.

| Query | Naive LLM: helpful or harmful? | Retrieval only: helpful or harmful? | RAG: helpful or harmful? | Notes |
|------|---------------------------------|--------------------------------------|---------------------------|-------|
| Where is the auth token generated? | Not helpful. Asked me which project and what kind of token. | Helpful. #1 is the exact `## Token Generation` section. | Helpful. "`generate_access_token` in `auth_utils.py`", cites `AUTH.md`. | RAG gave the one-line answer retrieval was hiding inside a paragraph. |
| How do I connect to the database? | Harmful. Made up `DB_HOST`, `DB_PORT`, `DB_USER`, etc. and pointed to folders like `src/config/` that don't exist. | Helpful. #1 is `## Connection Configuration`. | Helpful. `DATABASE_URL`, SQLite default `app.db`, the PostgreSQL format, and `db.py`. Cites `DATABASE.md`. | Clearest case of naive mode sounding sure and being wrong. |
| Which endpoint lists all users? | Partly right by luck. Guessed `GET /users`, listed `/api/v1/users` as a maybe, then asked which API. | Mixed. The right section was only #3. | Helpful. `GET /api/users`, cites `API_REFERENCE.md`. | RAG picked the right section out of the three, even though my ranking put it last. |
| How does a client refresh an access token? | Harmful. A long, confident OAuth answer (`/oauth/token`, `grant_type=refresh_token`, refresh tokens) that has nothing to do with this app. | Helpful. `## Client Workflow` and `### POST /api/refresh` are #1 and #2. | Helpful. `POST /api/refresh` with the current token in `Authorization: Bearer`. Cites both files. | This app has no refresh tokens at all, so the naive answer would send someone the wrong way. |
| Is there any mention of payment processing in these docs? | Not helpful. Said I forgot to include the docs, which proves it never saw them. | Helpful. Correct refusal. | Helpful. Correct refusal, and Gemini was never called. | |
| What environment variables are required for authentication? | Harmful. Listed variables for JWT apps, Auth0, Firebase and NextAuth. None of them exist here. | Mixed. #1 is the guide's intro with no answer; the real list is #2. | Mostly helpful, but incomplete. Named `AUTH_SECRET_KEY` and `TOKEN_LIFETIME_SECONDS` from `AUTH.md`. Didn't mention that `SETUP.md` calls the second one optional. | The first RAG try returned a 403 error from the API. The retry worked. |

**What patterns did you notice?**

- **When naive LLM looks impressive but can't be trusted:** the refresh-token and environment-variable answers. Both were long, well formatted and full of real-looking code, and both described some other system. The database answer was worse because it named specific variables (`DB_HOST`, `DB_PASSWORD`) as if they came from this project. Since the starter's naive prompt never includes the docs, Gemini has nothing to go on except general knowledge. On the other questions it just asked me which project I meant.
- **When retrieval only is clearly better:** whenever the topic isn't in the docs. Retrieval refuses fast and costs nothing. It's also the only mode where I can see the exact evidence, which made it the easiest mode to debug. The downside is that it's hard to read. You get three chunks of markdown, and sometimes the useful one is last.
- **When RAG is better than both:** almost every answerable question. It turned three snippets into one or two sentences, named the right file, and fixed my retriever's ordering mistake on the "all users" question. Nearly all of the improvement came from feeding it the right snippets, not from the model being smart.
- **Where RAG still fails:** it can only be as complete as what I retrieve. For the environment-variable question it never saw the `SETUP.md` section that disagrees with `AUTH.md`, so it gave one side with full confidence. It also depends on the API. One call failed with a 403, and DocuBot just printed the error instead of falling back to retrieval-only output.

---

## 5. Failure Cases and Guardrails

**Failure case 1: the right section loses to a heading.**
Question: "Which endpoint lists all users?" Retrieval put `## User Data Endpoints` (which is really the `GET /api/users/<user_id>` endpoint) at #1 and `DATABASE.md ## Query Helpers` at #2. The correct `### GET /api/users` section came in #3. All three matched 2 question words, so the order came down to tie-breaks. It should have put `GET /api/users` first, since that section literally says "Returns a list of all users."

**Failure case 2: the intro paragraph wins but has no answer.**
Question: "What environment variables are required for authentication?" #1 was the opening of `AUTH.md`, which only says the guide "covers ... required environment variables". It names none of them. The real answer (`AUTH_SECRET_KEY` and `TOKEN_LIFETIME_SECONDS`) was in the #2 section. A person reading retrieval-only output has to skip past a useless snippet.

**Failure case 3: the docs disagree with each other.**
`AUTH.md` says both `AUTH_SECRET_KEY` and `TOKEN_LIFETIME_SECONDS` "must be configured before starting the server". `SETUP.md` lists `TOKEN_LIFETIME_SECONDS` under optional variables, and lists `DATABASE_URL` as required. Retrieval returns sections from both files, and nothing in DocuBot notices the conflict. A good answer would point out that the docs disagree instead of picking one.

**Failure case 4: RAG gives a confident one-sided answer.**
Question: "What environment variables are required for authentication?" RAG answered `AUTH_SECRET_KEY` and `TOKEN_LIFETIME_SECONDS` and cited `AUTH.md`. That's accurate for `AUTH.md`. But `SETUP.md` says `TOKEN_LIFETIME_SECONDS` is optional, and that section never reached Gemini, so the answer sounds settled when it isn't. It should have said the docs disagree and pointed to both files.

**When should DocuBot say "I do not know based on the docs I have"?**

- When the topic isn't in the docs at all, like payment processing, deploying to Kubernetes, or "What is the capital of France?" All three are refused right now.
- When the only overlap is one generic word. "docs" or "api" by itself isn't evidence.
- When the retrieved sections are on the right topic but don't contain the specific fact asked for, like a value or a function name. My retriever can't catch this one. Only the LLM step in RAG can.

**What guardrails did you implement?**

- **Minimum evidence rule:** a section has to match at least 2 meaningful question words to count (or 1, if the question only has one meaningful word). If no section passes, `retrieve` returns an empty list and both modes 2 and 3 refuse.
- **Refuse before calling the LLM:** in RAG mode the refusal happens in my code, so Gemini is never asked to answer from nothing.
- **Limit on context:** at most 3 sections go to Gemini, so it can't wander through the whole corpus.
- **Same refusal text everywhere:** modes 2 and 3 both use "I do not know based on the docs I have." so it's easy to spot and test for.

---

## 6. Limitations and Future Improvements

**Current limitations**

1. It only matches exact words (after stemming). Synonyms like "expire" vs "lifetime", or "sign in" vs "login", don't match.
2. My stemmer is crude. "mention" becomes "ment", for example. It works on these docs, but it would make odd matches on a bigger corpus.
3. The 2-word threshold is a blunt rule. A long, off-topic question can still pass by sharing two common words, and a short, on-topic question can get refused.
4. It can't tell when two docs contradict each other, and it doesn't know which doc is newer or more trustworthy.

**Future improvements**

1. Weight rare words higher than common ones (TF-IDF style), so "refresh" counts more than "token", which is everywhere.
2. Include the parent heading with each section (for example "API Reference > User Data Endpoints > GET /api/users"), so both the retriever and the LLM know where a snippet sits.
3. Add an evaluation that checks which *section* was retrieved and counts a correct refusal as a pass, so the score reflects what actually matters.

---

## 7. Responsible Use

**Where could this system cause real world harm if used carelessly?**

The worst case is a confident wrong answer about security or setup. If naive mode makes up an environment variable name or an auth flow, a developer could ship a config that looks right but leaves tokens unsigned or endpoints unprotected. Even RAG can repeat whatever the docs say, so outdated or conflicting docs (like the `TOKEN_LIFETIME_SECONDS` mismatch) turn into wrong answers that look well sourced.

**What instructions would you give real developers who want to use DocuBot safely?**

- Use RAG mode, not naive mode. Check the file it cites before you act on the answer.
- Treat "I do not know" as useful. It means the docs have a gap, so go ask a person or fix the docs.
- Double-check anything about security, secrets, or production settings against the actual code.
- Keep the docs up to date. DocuBot is only as right as the files it reads.
