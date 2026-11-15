# AGY transcription workflow

## Commands and scope

Resolve `SKILL_DIR` from the installed skill location. Quote filesystem arguments; the driver handles the ASCII media workspace itself. `plan` and `status` do not upload anything. `run` invokes AGY on exactly the planned source spans; run `run` and `recover` outside the host's sandbox, because the sandbox proxy drops AGY's connection to Google (see SKILL.md).

`$TARGET` is the folder the recording belongs to; move the original there before `plan`.

```bash
OUTPUT_DIR="$TARGET/转写过程"
python3 "$SKILL_DIR/scripts/transcribe.py" plan "$TARGET/$AUDIO_NAME" --out "$OUTPUT_DIR"
python3 "$SKILL_DIR/scripts/transcribe.py" run --out "$OUTPUT_DIR" --canary-only
python3 "$SKILL_DIR/scripts/transcribe.py" status --out "$OUTPUT_DIR"
```

Multiple audio paths may be passed to `plan`. All recordings share the worker limit; do not multiply the limit by the number of recordings. The plan only reads media metadata and hashes. Every encoded WAV is checked for actual duration and size before AGY sees it.

Before dispatch the `run` command prints a `quota` (or `quota_low`) line with remaining Gemini 5-hour and weekly fractions from `agy --print /usage --output-format json` and the number of chunks it will send; the snapshot is saved as `quota_before.json`. The `run` command prints its run directory. Exit codes: **0** complete; **2** some chunks failed (gaps are explicit); **3** canary awaiting review; **4** account quota exhausted, dispatch halted; **5** AGY login expired, dispatch halted; **130** cancelled.

## How AGY is invoked

Each attempt runs `agy --agent=transcriber --model=… --add-dir=<chunk tmp> --output-format=stream-json --print …` with the skill directory as working directory, so AGY loads [the bundled agent](../.agents/agents/transcriber.md). That agent allows only the file reader and carries every transcription rule; the per-chunk prompt only names the WAV and its interval. A live probe on 2026-09-30 confirmed that `run_command` is unavailable under this agent in print mode.

Before this, AGY ran with its full tool set (shell, browser, web search, MCP) and auto-approval. On 2026-09-28 Flash High spent 12–69 model requests per 60-second chunk checking Python, ffprobe and Whisper instead of transcribing, and 1902 requests for one 72-minute recording exhausted the personal quota.

`@file` does not inline audio (`media=0` in AGY logs); the model has to open the WAV with `view_file`. On 2026-09-28 several accepted chunks were fluent conversations that were not in the recording (a Madrid night bus, a Scotland holiday, database sharding) while the local audio held continuous speech. They were answered in about two model requests, consistent with never opening the file. The driver now accepts an answer only after a file-reader event on that chunk's exact WAV.

AGY 1.5.3 (seen 2026-11-08) renamed `view_file` to `read_file` and its path parameter from `AbsolutePath` to `Path`. Before the driver knew the new names, it rejected every chunk as `tool_use` even though the model had opened the WAV and transcribed it. The driver now accepts both names and both parameters. If every chunk fails with `tool_use` or `not_listened` after an AGY update, check the tool names in a chunk's `events.jsonl` first. On 1.5.3 the startup event also listed every tool, including `run_command`, so the agent's `tools:` list may no longer restrict AGY; the driver's tool check is what still rejects other tools. The agent file must list only `read_file`: listing `view_file` as well makes AGY 1.5.3 refuse to start (`tool name conflict: multiple tools resolved with name "read_file"`), because it treats `view_file` as the same tool. On AGY older than 1.5.3, change the list back to `view_file`.

## Canary comparison

`plan` measures per-second RMS loudness locally; a second above −35 dBFS counts as voiced (speech in a 2026-09 test recording sat near −15..−30 dB, room noise near −50 dB). An earlier `silencedetect` map counted noise spikes as voice and is not used.

Chunks with fewer than 3 voiced seconds (or half the window, if shorter) are **not sent to AGY**; they become `[无人声]` with a `local_silence_not_sent` review note. On 2026-10-01 an AGY answer over the silent opening minute, after opening the file, was an invented phone notice (“小丽…明天十点开项目周会”); asking the model about near-silence invites fabrication.

The canary is the first chunk with at least half its seconds voiced; interview recordings often open with silence. A silent opening cannot verify anything, so never use it for source comparison. The canary is reused after verification. Inspect `chunks/<id>/attempt_*/stdout.txt`, `result.json`, and the original audio. Source-topic consistency is necessary, but a plausible topic alone is weak evidence. An independent short AGY check of that source span can expose attachment/content mismatches; it is not a human accuracy review.

The driver creates `canary_review_required.json`. Using `apply_patch`, create `canary_review.json` in the same run directory with its exact fingerprint and stdout hashes. For each source, fill `method`, `evidence`, and `passed`. `method` should identify actual listening, independent short AGY comparison, or another explicitly approved source comparison; `evidence` should point to the checked span and saved result. Set `passed` only when that comparison succeeded. Never manufacture a comparison merely to unlock bulk execution.

```json
{
  "fingerprint": "<exact run fingerprint>",
  "sources": {
    "r000": {
      "job": "r000_c0000",
      "stdout_sha256": "<exact accepted canary hash>",
      "method": "<comparison actually performed>",
      "evidence": "<source interval and evidence path>",
      "passed": true
    }
  }
}
```

Then continue with the same manifest:

```bash
python3 "$SKILL_DIR/scripts/transcribe.py" run --out "$OUTPUT_DIR"
```

This is agent verification within the transcription task. Do not ask the user to confirm routine batching or each retry. A different speech-to-text provider or a materially different external action still needs the relevant user authorization.

## Defaults and evidence limits

The default window is 300 seconds and the overlap is 20 seconds. For duration `D`, start at zero, take `[start, min(start+300,D)]`, stop when the endpoint reaches `D`, otherwise advance by 280 seconds. Do not use `ceil(D/900)` as the number of chunks or workers. For `D=12616.655238`, this generates 45 chunks; the last starts at 12320 seconds.

Model default: the newest `gemini-*-flash-medium` listed by `agy models`. On 2026-09-28 Flash High took a median 628 s per 60-second chunk and Flash Low 81 s; On 2026-09-30 Medium under the view_file-only agent handled 31 twenty-second chunks at a median 21 s and 3–4 model requests each; it sometimes wraps sections in XML-like tags or leaks reasoning or drafts before the answer in varied forms; the validator reads XML-like tags as headings and takes the last complete section set. A response that lost its opening heading (for example starting at `+00:04:40.000`) still fails. Longer windows with Medium are not yet measured.

Default workers = number of pending chunks, capped at 20. Google publishes no concurrency or per-minute limit for paid Antigravity plans; quota is token cost in 5-hour and weekly windows, so parallelism changes speed, not cost. Local runs reached 15 concurrent AGY processes repeatedly without any rate-limit error (every earlier 'capacity' error was in fact `Individual quota reached`). Above 15 is untested. Concurrency has one known cost besides RAM: when the Keychain token is near expiry, every process refreshes it at the same time, and they can all exceed the 10-second Keychain timeout in AGY and fall back to browser sign-in (2026-10-31: 19 processes, 19 sign-in tabs). The scheduler therefore holds the batch at one process until AGY logs a successful sign-in (auth gate), then fills the remaining slots. Each AGY process uses about 260 MB RAM. `--workers` changes the ceiling; the scheduler continuously refills slots and shares them across files. A retry backoff stays in the queue instead of sleeping inside a worker. Capacity failures halve the dispatch ceiling without killing otherwise healthy running jobs.

16 kHz mono PCM uses roughly 32000 bytes per second; a 300-second payload is about 9.6 MB plus the header. The driver's 18 MB ceiling is a conservative local policy, not a claim about an official universal AGY limit. Prefer shorter chunks over silently reducing sample rate below 16 kHz. Longer windows require explicit planning and must pass the same size check and full-size canary.

Historical M4A behavior depended on CLI version and path: a local run encountered `audio/mp4a-latm`/unsupported-MIME handling, while an older run produced M4A transcripts. Do not generalize this into permanent unsupported-format claims. WAV is the default to avoid reopening that ambiguity on every run. A `media_count=0` log alone does not establish whether a later tool read supplied audio; inspect the actual terminal outcome and source-content comparison.

The driver consults installed `--help`, `--version`, and `models` each invocation. It never falls back to an invented model ID. Use official documentation when the locally observed interface needs explanation; do not make a web research loop part of every successful transcription run.

## Attempts, deadlines, and failure meaning

The default attempt deadline is 600 seconds for AGY (the 2026-09-22 successful run used 10 minutes), plus bounded preparation and process cleanup. `--timeout` can change it deliberately. The default is two total attempts per chunk; canaries never exceed two. An expired process is terminated with its owned process group. Audio, source paths, and unrelated sessions are not deleted.

Terminal classification:

| Evidence | Driver response |
| --- | --- |
| `Individual quota reached` / `Resets in …` | Halt all dispatch, exit 4 with reset time and resume command; no retry, no model switch; does not spend the chunk's attempt budget |
| `Authentication required` / `authentication failed` | Halt all dispatch, exit 5 with resume command; no retry (each retry opens another sign-in tab); does not spend the chunk's attempt budget. Usually a Keychain read/refresh timeout under concurrency, not a signed-out account; see SKILL.md |
| Answer without a `read_file`/`view_file` event on this chunk's WAV | `not_listened`; one retry |
| Any tool other than `read_file`/`view_file` | `tool_use`; one retry |
| `[无法读取音频]` | `audio_unread`; one retry |
| Only markers such as `[无人声]`/`[听不清]` while local loudness shows ≥30% voiced seconds | `hollow_voiced`; one retry. Otherwise accepted with a `no_speech` review note |
| Nonzero exit | Fail; never count a completed shell wrapper as a transcript |
| Exit zero with empty stdout/no-output warning | Fail; one bounded retry |
| Explicit network error or eligibility error with EOF | Retry this chunk, without blocking other work |
| Capacity/rate error | Backoff this chunk and reduce future dispatch concurrency |
| Media/MIME or size failure | Stop that chunk; inspect the attachment/config before a changed rerun |
| Bad headings, process chatter, invalid turn clocks | One direct-output retry; preserve both versions |
| Early last speech timestamp | Review warning; it may be silence or missed speech |
| Startup warnings followed by valid completed output | Do not reject just because startup diagnostics were noisy |

The driver never switches codecs/providers. The API routes (ElevenLabs, Gemini) are a separate script that runs only after the user agrees; see [routing.md](routing.md). Failed spans are re-transcribed with `recover`, which plans contiguous failed chunks as groups with 60-second windows and 10-second overlap under `recovery/NN/`, reuses the main canary, and rebuilds the main transcript with each fully successful group marked `（短分段补转）`. Incomplete groups leave the original gap visible.

```bash
python3 "$SKILL_DIR/scripts/transcribe.py" recover --out "$OUTPUT_DIR" --deliver "$TARGET"
python3 "$SKILL_DIR/scripts/transcribe.py" recover --out "$OUTPUT_DIR" --deliver "$TARGET" --include-flagged
python3 "$SKILL_DIR/scripts/transcribe.py" recover --out "$OUTPUT_DIR" --deliver "$TARGET" --span 18:20-20:10
```

## Durable files and resume

```text
output/
  manifest.json
  latest_run.json
  runs/<runtime-fingerprint>/
    runtime.json
    canary_review_required.json
    canary_review.json
    status.json
    chunks/<id>/attempt_001/
      started.json
      prompt.txt
      prepare_stdout.log
      prepare_stderr.log
      stdout.txt        # AGY stream-json events
      response.txt      # the model's final answer
      stderr.log
      agy_cli.log
      transcript.md
      result.json
    r000_transcript.md
    review_queue.json
    deliverables.json
  recovery/NN/          # same layout, one per recover call
```

`--deliver <target>` copies `<录音名>_机器转写.md` and `<录音名>_待核对项.json` into the target folder after every run or recover.

Each attempt writes into a new directory. Raw stdout and stderr are never replaced with a retry's files. Generated transcript/status can be rebuilt, but their source attempts remain intact. The temporary ASCII WAV is removed after its hash, encoding metadata, source span, and logs have been recorded; it is reproducible from the preserved original source.

Source and semantic configuration fingerprints prevent stale reuse. Existing accepted raw output is hash-checked and revalidated on resume. Changed source content requires a fresh plan/output directory; a changed model/CLI/prompt creates another runtime directory. A lock prevents two drivers from using the same output simultaneously. Do not defeat it by manually altering state.

## Merge and review

The driver matches a long identical block sequence only inside the shared source interval. Different speaker labels, uncertain continuations, short repeated words, and disagreeing transcripts remain in the draft for review. Each unresolved boundary gets a visible note and review-queue entry. This deliberately avoids silently losing unique speech to a timestamp-only filter.

Two review flags catch fabricated text that slipped through: `overlap_mismatch` (both sides of a shared interval hold at least 20 characters but share under 15% of character pairs) and `language_mismatch` (an English-dominant chunk in a Chinese-dominant recording). Both add a visible note and a review entry; neither deletes text. On the 2026-09-28 data the overlap check caught the English fabrications but not short Chinese ones, and it cannot tell which side is wrong, so `--include-flagged` redoes both neighbours.

Section ranges come from the source manifest. Per-turn timestamps remain model estimates. A final `[HH:MM:SS]` with no content is ignored when assessing the last actual turn. Do not force all-silence spans to contain speech or infer physical activities from background noise.

`review_queue.json` includes source comparisons for the first, middle, and last 30 seconds. Check these through actual source listening or saved independent AGY samples. Additional suspicious gaps/boundaries need targeted review. Do not label structural validation as human listening or full word accuracy.

The transcript keeps machine uncertainty and local speaker numbering. The raw attempt retains `Unclear Sections` and `Quality Notes` even when the assembled body is shorter. All-success exit zero means a machine draft was produced; the separate review status remains unreviewed. Failed chunks produce explicit gaps and exit code 2.

## Regression checks

```bash
python3 -m unittest discover -s "$SKILL_DIR/tests" -v
```

These offline tests cover queue refill, retries, failure propagation, overlap preservation, timestamps, resume hashes, and owned-process timeout. They do not measure AGY throughput or speech accuracy. Compare live runtimes only under matched source, model, codec, window, worker count, retries, and verification requirements.
