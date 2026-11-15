---
name: agy-cli-transcribe
description: Transcription router for audio or video to text (recordings, interview audio, video soundtracks, subtitles needed for videos without captions), whether or not the user names a tool. Runs local AGY by default and can recommend ElevenLabs Scribe, Gemini Transcribe or Gemini Live after asking the user. Use when the user invokes $agy-cli-transcribe, mentions AGY, Antigravity CLI or local agy, or asks to transcribe a file with ElevenLabs or Gemini.
---

# Transcription router (AGY default)

Transcribe recordings through local AGY by default: it labels speakers across the whole recording, had the best Chinese names and terms in testing, and its quota is the widest. Three API routes sit beside it in `scripts/fallback.py`: ElevenLabs Scribe v2, Gemini Transcribe and Gemini Live. Recommend one when AGY fails or fits the recording poorly (see [routing.md](references/routing.md)), and run it only after the user says yes for that recording. When the user names ElevenLabs or Gemini for a file, that is the yes; still show the estimate if quota or a missing feature matters. Any other speech-to-text service requires explicit user approval.

For recordings without an exported file, use [the Voice Memos bridge](references/macos-voice-memos-share-copy-bridge.md) first. Otherwise use the bundled [driver](scripts/transcribe.py), with normal host execution permission. Do not modify global permissions or configuration.

Run `run` and `recover` outside the host's sandbox (in Claude Code: `dangerouslyDisableSandbox: true`; in Codex: escalated). Both start AGY processes, and AGY talks to `daily-cloudcode-pa.googleapis.com` for eligibility, models and every model request. Through the Claude Code sandbox proxy that connection drops (`loadCodeAssist: EOF`, `eligibility check failed`, `stream idle timeout`). On 2026-11-02 a 46-minute recording lost 8 of 10 chunks and then 40 of 44 recovery chunks this way; the same `recover` outside the sandbox finished all 44. These failures look like a flaky network, so retrying inside the sandbox only repeats them. `plan`, `status` and `fallback.py estimate` do not start AGY and can stay in the sandbox.

## Before you start: where files go

1. Pick the target folder. A recording that belongs to a job, application or project goes into that folder (for example the folder of the project the meeting was about). Ask once only when the owner is unclear.
2. Move the original recording from Downloads or similar into the target folder **before** `plan`. The plan records the source path and hash; moving the file afterwards breaks every later run.
3. Use `--out <target>/转写过程` for run records and `--deliver <target>` for the transcript and review list.

## Fast path

Read [workflow.md](references/workflow.md) before the first run for commands, verification, and failure handling.

1. `plan`: inspect sources locally, save hashes, exact spans and per-second loudness. Near-silent chunks are recorded as `[无人声]` without calling AGY. Defaults: **mono 16 kHz PCM WAV; 5-minute windows; 20-second overlap**.
2. `run --canary-only`: the driver first prints remaining Gemini quota (`agy /usage`, no model request) and the number of chunks it will send. Tell the user both. On `quota_low` (5-hour window under 20% or weekly under 10%), ask before the full run. The driver picks the first chunk with clear local speech. Record an actual source-content comparison using the generated review template. This is agent verification, not a new user permission request.
3. `run --deliver <target>`: by default every pending chunk runs at once (at most 20). Keep the default; lower `--workers` only after real capacity errors.
4. `recover --deliver <target>`: re-transcribe failed spans with 60-second windows and merge them back. Add `--span START-END` for a named suspect span. `--include-flagged` redoes every flagged chunk in full; use it only when the user wants that cost.
5. Report the review list honestly. Record only checks actually performed.

Use `status` for progress. Do not write ad hoc launchers or mergers, and do not poll each chunk individually.

## How AGY is called

The driver runs AGY from this skill directory with `--agent transcriber`. The bundled [agent](.agents/agents/transcriber.md) holds all transcription rules and allows only the file reader (`read_file` in AGY 1.5.3 and later, `view_file` before), so AGY cannot run commands, browse, or search. Never call AGY for transcription without this agent. The default model is the newest available Flash Medium; `--model` or `AGY_TRANSCRIPTION_MODEL` may select another listed ID.

AGY does not inline the `@file` audio; the model must open the WAV with the file reader (`read_file` or `view_file`). The driver reads AGY's stream-json events and rejects any answer given without opening that chunk's WAV (`not_listened`). Such answers were fabricated conversations in practice.

## Cost and speed

AGY quota is metered by token cost in a 5-hour and a weekly window per model group, not by parallelism. Each chunk request carries about 34k input tokens of fixed AGY overhead regardless of audio length, so short windows cost far more per audio minute (2026-10-01 measurement: 5-minute ≈ 6.7k tokens/min, 60-second ≈ 24k, 20-second ≈ 82k). Keep the 5-minute default for full runs. A batch waits for its slowest chunk, so workers beyond the chunk count add nothing.

## Failure and resume rules

- Retry `not_listened`, `tool_use`, empty, format, timeout and transient network failures once by default.
- A 'no speech' answer over audio that local loudness calls voiced is `hollow_voiced` and retried.
- **Account quota** (`Individual quota reached`, `Resets in …`): the driver stops all dispatch and exits 4 with the reset time and a resume command. Do not retry, and do not switch models: they share the same quota. Resume after the reset with the same command.
- **Sign-in prompt** (`Authentication required`): usually the account is still signed in. AGY keeps its token in the macOS Keychain and refreshes it when it is close to expiry. Each `agy` process reads the Keychain and refreshes on its own, and gives up after 10 seconds (`keyringAuth: timed out after 10s`). It then falls back to browser sign-in, which opens a Google sign-in tab in the user's Chrome. On 2026-10-31 the token had 5 minutes left; 19 processes started at once (two drivers side by side) all timed out together and opened 19 tabs, while one of the refreshes in fact succeeded 25 seconds later. A single canary process 10 minutes earlier authenticated in 4 seconds. Two driver safeguards since then: it starts **one** AGY process first and releases the rest only after that process logs a successful sign-in (`silent auth succeeded` / `authenticated successfully` in `agy_cli.log`) or finishes cleanly, so only one process ever refreshes the token; and it stops all dispatch on the first sign-in prompt and exits 5, so retries do not open more tabs. A live check on 2026-10-31 started the other two processes 2 seconds after the first one signed in; all three signed in within 3 seconds. Run one driver at a time (pass several recordings to one `plan`). After exit 5, check `agy_cli.log` of a failed chunk: `expired=false` plus the 10-second timeout means nothing is wrong with the account; resume with the same command. Ask the user to sign in only when the log shows no Keychain token or a failed refresh, and then one tab is enough.
- Per-minute capacity errors halve concurrency. Diagnose media/configuration failures before a changed rerun.
- Keep immutable attempts and fingerprinted resume. Never overwrite failed logs with retry text. Cancel only owned processes.

## Choosing another route (ask first)

Read [routing.md](references/routing.md) when AGY fails in a way that will not clear soon (exit 4 with a far reset, a real login problem, spans still failed after one `recover`), or before starting when the recording is mostly English, mixes Chinese and English, or needs exact timestamps. Run `fallback.py estimate` (no upload), then tell the user what failed or why AGY fits poorly, which route you recommend and why, and what each loses: Live has **no speaker labels** and **one timestamp per ~2 minutes**; Gemini Transcribe **drops the Chinese in mixed recordings** and renumbers speakers every ~4.5 minutes, 25 requests per day; ElevenLabs has about **150 free minutes per month**, then $0.22/hour; every API route sends the audio to a third party, and Gemini's free tier may train on it unless the user is in the UK/EEA/Switzerland. Wait for an explicit yes. If the user declines, stay on AGY: fix the cause (sign in, wait for the reset, `recover`) and retry.

Known AGY weaknesses from the 2026-10-31 comparison: per-turn timestamps are model estimates and ran up to ~15 s late; a mixed Chinese/English clip came back in traditional characters.

## Output contract

Deliver the transcript and review list. Preserve language, false starts, uncertainty, and local speaker labels. Do not invent names or endpoint utterances. Deduplicate only supported identical overlap text; never cut unique utterances by timestamps.

Overlap mismatch and language mismatch between neighbouring chunks are flagged in the transcript and review list; they are never deleted automatically. Offer `recover --include-flagged` for them.

Use `machine_checked_unreviewed` for machine drafts. Structure checks do not prove acoustic accuracy. Human-reviewed means a human reviewed it. Expose failed and unresolved spans.

## Maintenance

Run [offline regressions](tests/test_transcribe.py) after changing the driver or the agent file. Throughput and accuracy claims require live evidence.
