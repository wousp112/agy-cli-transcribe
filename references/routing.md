# Transcription routing: AGY, ElevenLabs, Gemini

One skill, four routes. AGY is the default. The other three are API routes in `scripts/fallback.py`; each runs only after the user says yes for that recording.

The decision runs in three steps, in this order. A route that fails step 1 is out, however cheap or fast it is: a missing feature cannot be paid for with price.

## Step 1: what rules a route out

| Condition | Effect |
|---|---|
| Speaker labels needed (interview, meeting, group discussion) | Gemini Live is out |
| Chinese and English mixed in the recording (English questions with Chinese answers, Chinese with English terms in whole sentences, English group work that ends in Chinese) | Gemini Transcribe and Gemini Live are out. In the 2026-10-31 test, Gemini Transcribe on auto-detect returned **no Chinese at all** for two such clips; with a Chinese+English hint the Chinese came back in traditional characters with a "會會會…" repetition loop |
| Accurate timestamps needed (subtitles, cutting clips, finding a moment) | Prefer ElevenLabs or Gemini Transcribe: their times come from word timestamps. AGY's per-turn times are model estimates and ran up to ~15 s late in the test |
| Recording longer than ElevenLabs' remaining free minutes (`estimate` shows `fits_free_quota: false`) | ElevenLabs only if the user accepts paying ($0.22/hour, `paid_cost_usd`) or splitting the job |
| Recording needs more Gemini Transcribe requests than are left today (`fits_today: false`) | Gemini Transcribe can only finish tomorrow (it resumes) |
| Sensitive recording (interview, health, private conversation) | Every API route sends the audio to a third party. Gemini free tier: Google may train on it and humans may review it, unless the user is in the UK/EEA/Switzerland. Say so; the user may prefer to wait for AGY |

## Step 2: retry or switch

AGY failures come in two kinds, as payment systems separate soft from hard declines:

- **Retry AGY** for things that clear by themselves: `network`, `timeout`, a Keychain read timeout under concurrency (exit 5 with `expired=false` in `agy_cli.log`; see SKILL.md), single failed spans (`recover`). Switching provider here only spreads the problem.
- **Offer a switch** when AGY cannot finish in the time the user needs: exit 4 with a far-away reset, exit 5 with a really missing or unrefreshable token and the user unable to sign in now, spans still failed after one `recover`.

Also offer a route up front, before running AGY, when step 1 says AGY is a poor fit: the recording is mostly English, or mixed Chinese/English, or the user needs exact timestamps. AGY still handles these, but ElevenLabs was clearly better in the test.

## Step 3: rank what is left

| Need | Order |
|---|---|
| Chinese interview review (default case) | **AGY** → ElevenLabs → Gemini Transcribe (with `cmn-Hans-CN`) |
| Mixed Chinese/English, or mostly English | **ElevenLabs** → AGY |
| Exact timestamps / subtitles | **ElevenLabs** → Gemini Transcribe (pure Chinese) → AGY |
| Only the gist, fast, public content | Gemini Live → ElevenLabs → AGY |
| ElevenLabs free minutes used up and user will not pay | AGY → Gemini Transcribe (pure Chinese only) → Gemini Live |

## What to tell the user before asking

Run the local estimate first. It reads ElevenLabs' remaining quota and the Gemini model list; it uploads no audio:

```bash
uv run --script "$SKILL_DIR/scripts/fallback.py" estimate "$TARGET/$AUDIO_NAME"
```

Then tell the user, in Chinese: what failed or why AGY is a poor fit, which route you recommend and why, and what each candidate loses compared with AGY, with numbers from the estimate:

| | AGY (default) | ElevenLabs Scribe v2 | Gemini Transcribe | Gemini Live |
|---|---|---|---|---|
| Speaker labels | Whole recording | Whole recording (one request) | Renumbered every ~4.5-min segment | **None** |
| Timestamps | Per turn, model-estimated (up to ~15 s off) | Per word | Per word | **One per ~2 minutes** |
| Chinese/English mixed | Correct text, but output once came in traditional characters | **Best in test** | **Drops the Chinese** | Not tested on mixed |
| Pure Chinese wording | **Best** (names, terms) | Good; one phrase wrong | Terms wrong (RAG→RIG, Dify→DeFi) | Close to AGY on clear audio |
| Limit | AGY quota windows | Free plan ≈ 150 min/month (`est_minutes_left`); paid $0.22/hour | 25 requests/day ≈ 110 min/day | Unlimited requests |
| 46-min recording | ≈ 2.5 min when healthy | ≈ 6 min (est.) | ≈ 9–10 min | ≈ 5 min |

Proceed only after an explicit yes for this recording. If the user says no, stay on AGY: fix the cause (sign in once for a real login problem, wait for the reset for exit 4, `recover` for exit 2) and retry with the same driver command.

## Running a route

```bash
uv run --script "$SKILL_DIR/scripts/fallback.py" elevenlabs        "$TARGET/$AUDIO_NAME" --deliver "$TARGET"
uv run --script "$SKILL_DIR/scripts/fallback.py" gemini-transcribe "$TARGET/$AUDIO_NAME" --deliver "$TARGET"
uv run --script "$SKILL_DIR/scripts/fallback.py" gemini-live       "$TARGET/$AUDIO_NAME" --deliver "$TARGET"
```

`uv` installs `google-genai`, `httpx` and `numpy` into its own cache; the system Python is untouched.

Output: `<录音名>_机器转写_ElevenLabs.md`, `_Gemini-Transcribe.md` or `_Gemini-Live.md` in the target folder, status `machine_checked_unreviewed`, header with model, method and failed parts. Raw API results stay under `转写过程_ElevenLabs/` and `转写过程_Gemini-Transcribe/<录音名>/`; rerunning the same command reuses them, so a Gemini daily 429 resumes from the first unfinished segment and an ElevenLabs rerun costs nothing.

Language: ElevenLabs auto-detects by default and handled mixed speech that way; `--language zho` / `eng` forces one. Gemini routes default to `cmn-Hans-CN`; use `--language en-US` for an English recording. Do not use Gemini auto-detect.

## Models

- Gemini: every run lists the key's models and picks the newest `gemini-<version>-transcribe` / `-transcribe-live`; a stable ID beats a `-preview` of the same version, so 3.8 or 4.x is picked up without code changes. `fallback.py models` shows the choice; `--model` overrides. A brand-new model can have zero free quota at first; on an immediate 429, pass the previous version.
- ElevenLabs: `scribe_v2` unless `--model` or `$ELEVENLABS_STT_MODEL` names another. ElevenLabs has no model-list endpoint for speech-to-text that the script can rely on, so a newer Scribe is a one-flag change.

## API keys

- ElevenLabs: `$ELEVENLABS_API_KEY` (for example set in your shell profile), else Keychain service `elevenlabs-api-key`.
- Gemini: Keychain service `gemini-api-key`, `$GEMINI_API_KEY` overrides. Replace with:

```bash
security add-generic-password -U -a "$USER" -s gemini-api-key -l "Gemini API key (AI Studio, transcribe fallback)" -w "$(pbpaste | tr -d '[:space:]')"
```

Never print a key or write it to a file.

## Evidence (2026-10-31)

Three 4.5-minute test recordings, all routes on auto-detect, no human word-by-word reference (judged by cross-comparison and context): a Chinese phone call, a conversation that switches from English to Chinese, a 4-person English group discussion that ends in Chinese.

- ElevenLabs: ~33 s per clip; mixed speech complete; three English names and the speakers right. 13.5 audio minutes cost 900 of 10,000 free credits.
- AGY: 94–269 s per chunk; best Chinese terms (Dify, 拿证); mixed clip in traditional characters; turn times up to ~15 s late against the two word-timestamp routes, which agreed with each other.
- Gemini Transcribe: lost the Chinese in both mixed clips; English terms wrong in the Chinese clip; one dropped connection (now retried).
- Earlier the same day on 46–47-minute recordings: Live ≈ 5 min, Transcribe ≈ 10 min; Live has no speakers; Transcribe renumbers speakers per segment (说话人1 was the host at 00:00 and the guest at 22:25).
- Live accepts audio up to about 5× realtime; at 10× it dropped sentences; one session at 3× disconnected after ~8 minutes of audio. Hence ~2-minute segments cut at pauses, 3×, 4 parallel sessions.
- Elsewhere: ElevenLabs' **realtime** model scored 75% mixed-token error on Mandarin-English ([Speko](https://benchmarks.speko.ai/stt-codeswitch)); this route uses the batch model. On the English-only [Artificial Analysis](https://artificialanalysis.ai/speech-to-text/non-streaming) board Scribe v2 is 2.2% WER, Gemini 3.5 Transcribe 2.6%.

Three clips are a small sample. Re-check the ranking when a route changes model or after a surprising result, and record what was found here.
