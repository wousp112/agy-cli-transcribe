#!/usr/bin/env python3
"""AGY-only transcription driver. Python stdlib + installed ffmpeg/ffprobe/agy.

plan/status are local. run sends the selected normalized audio to AGY.
Machine validation never asserts that generated words match the audio.
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import datetime as dt
import hashlib
import heapq
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

VERSION = '3.2.0'
STAMP = re.compile(r'\[(\d{2}):(\d{2}):(\d{2})\]')
RETRYABLE = {'network', 'eligibility_transient', 'capacity', 'timeout', 'empty', 'format', 'timing',
             'not_listened', 'tool_use', 'audio_unread', 'hollow_voiced'}
SKILL_DIR = Path(__file__).resolve().parents[1]
AGENT = 'transcriber'
AGENT_FILE = SKILL_DIR/'.agents'/'agents'/f'{AGENT}.md'
ALLOWED_TOOLS = {'view_file', 'read_file'}  # AGY 1.5.3 renamed view_file to read_file
NO_SPEECH, UNREAD = '[无人声]', '[无法读取音频]'
# The rules live in the bundled agent file, which also limits AGY to view_file.
PROMPT = '''Transcribe this audio file: {audio}
Chunk {id}. Absolute source interval: {start} to {end}.
'''


def utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='milliseconds')


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def save_json(path: Path, value: object) -> None:
    """Atomic status updates; attempts themselves have unique immutable paths."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.state_', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def clock(seconds: float) -> str:
    n = max(0, int(seconds))
    return f'{n // 3600:02d}:{n % 3600 // 60:02d}:{n % 60:02d}'


def spans(duration: float, window: float = 300, overlap: float = 20) -> list[tuple[float, float]]:
    if not all(math.isfinite(v) for v in (duration, window, overlap)) or not duration > 0 or not 0 <= overlap < window:
        raise ValueError('Require finite duration > 0 and 0 <= overlap < window.')
    result, start = [], 0.0
    while start < duration:
        end = min(duration, start + window)
        result.append((start, end))
        if end >= duration:
            break
        start += window - overlap
    return result


def checked(cmd: list[str], timeout: float = 45, include_stderr: bool = False) -> str:
    result = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'{Path(cmd[0]).name} failed ({result.returncode}): {result.stderr[-600:]}')
    return result.stdout + (result.stderr if include_stderr else '')


VOICE_DB = -35.0      # per-second RMS; speech here sits near -15..-30 dB, room noise near -50 dB
SILENT_SECONDS = 3    # chunks with fewer voiced seconds are not sent to AGY


def loudness(path: Path) -> list[float]:
    """Per-second RMS level in dBFS. Local only; nothing is uploaded."""
    text = checked(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-i', str(path), '-vn', '-af',
                    'aresample=16000,asetnsamples=n=16000:p=0,astats=metadata=1:reset=1,'
                    'ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file=-', '-f', 'null', '-'], timeout=900)
    return [float(v) if v not in ('-inf', 'inf', 'nan') else -120.0 for v in re.findall(r'RMS_level=(\S+)', text)]


def voiced(start: float, end: float, levels: list[float]) -> int:
    """Seconds in [start, end) whose RMS is above VOICE_DB."""
    return sum(1 for level in levels[int(start):int(math.ceil(end))] if level > VOICE_DB)


def plan(sources: list[str], out: Path, window: float, overlap: float,
         ranges: list[tuple[float, float]] | None = None, parent: dict | None = None) -> dict:
    """ranges limits jobs to source intervals (recovery); parent links a recovery to its main output."""
    config = dict(window=window, overlap=overlap, sample_rate=16000, channels=1,
                  codec='pcm_s16le', max_upload_bytes=18_000_000)
    records, jobs = [], []
    for ri, name in enumerate(sources):
        path = Path(name).expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValueError(f'Not a file: {path}')
        duration = float(checked(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                                  '-of', 'default=nw=1:nk=1', str(path)]).strip())
        groups = [(0.0, duration)] if ranges is None else [(max(0.0, a), min(duration, b)) for a, b in ranges]
        windows = [(gi, a+s, a+e) for gi, (a, b) in enumerate(groups) for s, e in spans(b-a, window, overlap)]
        if max(e-s for _, s, e in windows) * 32000 + 1024 > config['max_upload_bytes']:
            raise ValueError('Planned PCM attachment exceeds the conservative 18 MB budget. Use shorter --window.')
        levels = loudness(path)
        record = dict(id=f'r{ri:03d}', path=str(path), sha256=digest(path), duration=duration,
                      bytes=path.stat().st_size)
        records.append(record)
        for ci, (group, start, end) in enumerate(windows):
            jobs.append(dict(id=f'r{ri:03d}_c{ci:04d}', source=record['id'], path=str(path),
                             source_sha256=record['sha256'], index=ci, group=group, start=start, end=end,
                             voiced_seconds=voiced(start, end, levels)))
    core = dict(schema=3, sources=records, config=config, jobs=jobs)
    if parent:
        core['parent'] = parent
    core['fingerprint'] = fingerprint(core)
    target = out/'manifest.json'
    if target.exists():
        if json.loads(target.read_text()) != core:
            raise ValueError('This output directory has a different manifest. Select a new --out directory.')
    else:
        save_json(target, core)
    return core


def select_model(model_output: str, requested: str | None) -> str:
    ids = [line.split('\t')[0].strip() for line in model_output.splitlines() if '\t' in line]
    if requested:
        if requested not in ids:
            raise ValueError('Requested model ID was not returned by agy models.')
        return requested
    available = []
    for value in ids:
        match = re.fullmatch(r'gemini-(\d+(?:\.\d+)*)-flash-medium', value)
        if match:
            available.append((tuple(map(int, match[1].split('.'))), value))
    if not available:
        raise ValueError('No available Flash Medium ID found; pass --model explicitly.')
    return max(available)[1]


def classify(text: str, rc: int, empty: bool) -> str | None:
    lower = text.lower()
    if 'quota reached' in lower or 'quota exceeded' in lower or re.search(r'resets? in \d', lower):
        return 'quota'
    if any(s in lower for s in ('model_capacity_exhausted', 'too many requests', 'resource_exhausted')):
        return 'capacity'
    if 'authentication required' in lower or 'authentication failed' in lower:
        return 'auth'
    if 'eligibility check failed' in lower:
        return 'eligibility_transient' if 'eof' in lower else 'auth'
    if any(s in lower for s in ('audio_not_read', 'unsupported mime type', 'no inline audio data', 'cannot read binary audio')):
        return 'media'
    if 'exceeds the 20mb' in lower or 'file too large' in lower:
        return 'size'
    if rc == 124 or 'print timeout' in lower:
        return 'timeout'
    if any(s in lower for s in ('broken pipe', 'connection reset', 'unexpected eof')):
        return 'network'
    if rc:
        return 'process'
    if empty or 'no output and no recorded error' in lower:
        return 'empty'
    return None


def parse_blocks(body: str) -> list[dict]:
    blocks = []
    for line in body.splitlines():
        match = STAMP.search(line)
        if match:
            h,m,s = map(int, match.groups())
            if m >= 60 or s >= 60:
                raise ValueError('Invalid clock value')
            blocks.append(dict(time=3600*h+60*m+s, text=line))
        elif blocks:
            blocks[-1]['text'] += '\n' + line
    return blocks


def meaningful(block: dict) -> bool:
    return bool(re.sub(r'[\s*`\-]+', '', STAMP.sub('', block['text'])))


def spoken(block: dict) -> bool:
    """True when a block holds words, not only markers such as [听不清] or [silence]."""
    text = re.sub(r'(?i)speaker\s*\d+\s*[:：]', '', STAMP.sub('', block['text']))
    text = re.sub(r'\[[^\]]*\]|\([^)]*\)|（[^）]*）', '', text)
    return bool(re.sub(r'[\W_]+', '', text))


def validate(raw: str, stderr: str, rc: int, job: dict) -> dict:
    """Checks format, terminal failures and timing; not acoustic truth."""
    # Models sometimes wrap the three sections in XML-like tags; read them as headings.
    raw = re.sub(r'</(?:Chunk Transcript|Unclear Sections|Quality Notes)>', '', raw)
    raw = re.sub(r'<(Chunk Transcript|Unclear Sections|Quality Notes)>', r'\n# \1\n', raw)
    if '# Chunk Transcript' in raw:
        # Models leak reasoning or drafts before the answer in varied forms; the answer is
        # the last full section set. Tool use is judged from AGY events, not from this text.
        raw = raw[raw.rfind('# Chunk Transcript'):]
    pos = raw.find('# Chunk Transcript')
    # A recording can legitimately discuss error messages. Classify diagnostics
    # outside the transcript, not quoted words inside a timestamped utterance.
    diagnostic = raw if pos < 0 else raw[:pos]
    failure = classify(stderr + '\n' + diagnostic, rc, not raw.strip())
    if failure:
        return dict(ok=False, reason=failure, warnings=[])
    if pos < 0 or not re.fullmatch(r'\s*(?:```(?:markdown|md|text)?\s*)?[^\w\s]{0,3}\s*(?:\d+)?\s*',raw[:pos]):
        return dict(ok=False, reason='format', warnings=[])
    clean = raw[pos:].strip().removesuffix('```').rstrip()
    if '# Unclear Sections' not in clean or '# Quality Notes' not in clean or clean.index('# Unclear Sections') > clean.index('# Quality Notes'):
        return dict(ok=False, reason='format', warnings=[])
    body = clean.split('# Chunk Transcript',1)[1].split('# Unclear Sections',1)[0].strip()
    if re.search(r'\[\d{1,2}:\d{2}\]', body) or re.search(r'(?im)^(?:tool_call|function_call|error executing tool|I will (?:run|use|call|wait))', body):
        return dict(ok=False, reason='format', warnings=[])
    try:
        blocks = parse_blocks(body)
    except ValueError:
        return dict(ok=False, reason='timing', warnings=[])
    if not blocks or not any(meaningful(b) for b in blocks):
        return dict(ok=False, reason='empty', warnings=[])
    stamps = [b['time'] for b in blocks]
    if any(b<a for a,b in zip(stamps,stamps[1:])) or min(stamps) < job['start']-1 or max(stamps) > job['end']+2:
        return dict(ok=False, reason='timing', warnings=[])
    actual = [b['time'] for b in blocks if meaningful(b)]
    if UNREAD in body:
        return dict(ok=False, reason='audio_unread', warnings=[])
    warnings = []
    if not any(spoken(b) for b in blocks):
        # A 'nothing audible' answer over audio that the local energy map calls voiced is suspect.
        if job.get('voiced_seconds', 0) >= max(10, 0.3*(job['end']-job['start'])):
            return dict(ok=False, reason='hollow_voiced', warnings=[])
        warnings.append('no_speech')
    if actual[0] > job['start']+30:
        warnings.append('late_first_turn_check_audio')
    if actual[-1] < job['end']-45:
        warnings.append('early_last_turn_check_audio_not_proof_of_missing_speech')
    return dict(ok=True, reason=None, warnings=warnings, clean=clean, blocks=blocks,
                first_content_timestamp=actual[0], last_content_timestamp=actual[-1])


def judge(stdout: str, stderr: str, rc: int, job: dict, attachment: str) -> tuple[dict, str]:
    """Read AGY stream-json: the answer must come after view_file opened this chunk's WAV."""
    events, other = [], []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            other.append(line)
            continue
        (events if isinstance(event, dict) else other).append(event if isinstance(event, dict) else line)
    result = next((e['result'] for e in reversed(events)
                   if e.get('event') == 'result' and isinstance(e.get('result'), dict)), {})
    tools = []
    for event in events:
        step = event.get('step_update') or {}
        if step.get('step_type') == 'tool' and step.get('state') == 'DONE':
            tools.append((step.get('tool_name'), (step.get('tool_info') or {}).get('parameters') or {}))
    response = result.get('response') or ''
    info = dict(tools=[name for name, _ in tools], agy_status=result.get('status'))
    diagnostic = '\n'.join([stderr, *other, json.dumps({k: v for k, v in result.items() if k != 'response'})])
    failure = classify(diagnostic, rc, not response.strip())
    if failure:
        detail = next((line.strip()[:300] for line in diagnostic.splitlines()
                       if re.search(r'(?i)quota|error|authentication', line)), '')
        return dict(ok=False, reason=failure, warnings=[], detail=detail, **info), response
    if any(name not in ALLOWED_TOOLS for name, _ in tools):
        return dict(ok=False, reason='tool_use', warnings=[], **info), response
    if not any(name in ALLOWED_TOOLS and str(params.get('AbsolutePath') or params.get('Path') or '').endswith('/'+attachment)
               for name, params in tools):
        return dict(ok=False, reason='not_listened', warnings=[], **info), response
    verdict = validate(response, '', 0, job)
    verdict.update(info)
    return verdict, response


def near_silent(job: dict) -> bool:
    return job.get('voiced_seconds', SILENT_SECONDS) < min(SILENT_SECONDS, (job['end']-job['start'])/2)


def silent_verdict(job: dict) -> dict:
    line = f'[{clock(job["start"])}] {NO_SPEECH}'
    return dict(ok=True, reason=None, warnings=['local_silence_not_sent'], clean=f'# Chunk Transcript\n{line}\n',
                blocks=[dict(time=int(job['start']), text=line)], first_content_timestamp=int(job['start']),
                last_content_timestamp=int(job['start']))


def overlap_merge(previous: list[dict], current: list[dict], start: float, end: float) -> tuple[list[dict], dict]:
    """Delete only a long, identical block sequence in the shared interval.

    Speaker text is retained in the comparison; disagreement goes to review.
    No timestamp-only cutoff can delete a unique utterance.
    """
    def canonical(block):
        return re.sub(r'[\s*`]+', '', STAMP.sub('', block['text']))
    left = [b for b in previous if start-15 <= b['time'] <= end and meaningful(b)]
    right = [b for b in current if b['time'] <= end and meaningful(b)]
    matched = 0
    for n in range(1, min(len(left),len(right))+1):
        a, b = [canonical(x) for x in left[-n:]], [canonical(x) for x in right[:n]]
        if a == b and sum(map(len,b)) >= 24:
            matched = n
    kept = list(current)
    if matched:
        ids = {id(b) for b in right[:matched]}
        kept = [b for b in current if id(b) not in ids]
    unresolved = [b for b in kept if b['time'] < end and meaningful(b)]
    return kept, dict(start=start,end=end,removed_exact_blocks=matched,
                      review_needed=bool(unresolved))


AUTH_OK_MARKERS = ('silent auth succeeded', 'authenticated successfully')


def auth_passed(log: Path) -> bool:
    """AGY writes these lines once it holds a usable token (from the Keychain, refreshed if needed)."""
    try:
        text = log.read_text(errors='replace')
    except OSError:
        return False
    return any(marker in text for marker in AUTH_OK_MARKERS)


def run_process(cmd: list[str], cwd: Path, stdout: Path, stderr: Path,
                timeout: float, cancelled: threading.Event,
                auth_log: Path | None = None, authed: threading.Event | None = None) -> int:
    """Bound the process tree we create; never kill unrelated AGY sessions.

    With auth_log/authed, sets `authed` as soon as AGY logs a successful sign-in."""
    with stdout.open('wb') as out, stderr.open('wb') as err:
        proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out,
                                stderr=err, start_new_session=True)
        stop = time.monotonic()+timeout
        while proc.poll() is None and not cancelled.is_set() and time.monotonic() < stop:
            cancelled.wait(0.2)
            if authed is not None and not authed.is_set() and auth_passed(auth_log):
                authed.set()
        if proc.poll() is not None:
            return proc.returncode
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return proc.wait()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        return 130 if cancelled.is_set() else 124


def attempt(job: dict, number: int, root: Path, runtime: dict, timeout: float,
            cancelled: threading.Event, authed: threading.Event | None = None) -> dict:
    folder = root/'chunks'/job['id']/f'attempt_{number:03d}'
    folder.mkdir(parents=True, exist_ok=False)
    begin = time.monotonic()
    meta = dict(job=job['id'],number=number,started_at=utc(),fingerprint=runtime['fingerprint'])
    if near_silent(job):
        # Models invent plausible speech over near-silence even after opening the file; do not ask.
        meta.update(local_silence=True,exit_code=0,validation=silent_verdict(job),ended_at=utc(),elapsed_seconds=0)
        save_json(folder/'result.json',meta)
        return meta
    # This marker makes a killed/interrupted attempt visible on resume.
    save_json(folder/'started.json', meta)
    tmp = Path(tempfile.mkdtemp(prefix='agy_tx_', dir='/private/tmp' if Path('/private/tmp').is_dir() else None))
    if not str(tmp).isascii() or any(c.isspace() for c in str(tmp)):
        raise ValueError('AGY temporary workspace must be an ASCII path without spaces.')
    attachment = f'{job["id"]}_{runtime["fingerprint"][:10]}_{number:03d}.wav'
    audio = tmp/attachment
    try:
        rc = run_process(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y',
                          '-ss',str(job['start']),'-i',job['path'],'-t',str(job['end']-job['start']),
                          '-vn','-ac','1','-ar','16000','-c:a','pcm_s16le',str(audio)],
                         tmp,folder/'prepare_stdout.log',folder/'prepare_stderr.log',60,cancelled)
        if rc or not audio.is_file() or audio.stat().st_size <= 44:
            raise RuntimeError(f'Audio preparation failed, exit={rc}')
        if audio.stat().st_size > 18_000_000:
            raise RuntimeError('Prepared audio exceeds 18 MB policy budget; shorten the window.')
        media_info = json.loads(checked(['ffprobe','-v','error','-show_entries','format=duration:stream=sample_rate,channels', '-of','json',str(audio)]))
        if abs(float(media_info['format']['duration'])-(job['end']-job['start'])) > .1:
            raise RuntimeError('Prepared audio duration does not match the selected span.')
        stream=media_info['streams'][0]
        if int(stream['sample_rate']) != 16000 or int(stream['channels']) != 1:
            raise RuntimeError('Prepared audio encoding differs from the planned profile.')
        meta.update(audio_sha256=digest(audio),audio_bytes=audio.stat().st_size,attachment=attachment,media=media_info)
        prompt = PROMPT.format(id=job['id'],start=clock(job['start']),end=clock(job['end']),audio=audio)
        (folder/'prompt.txt').write_text(prompt)
        # Run from the skill directory so AGY loads the bundled view_file-only agent.
        cmd = [runtime['binary'],f'--agent={AGENT}',f'--model={runtime["model"]}',f'--add-dir={tmp}',
               '--disable-slash-commands',f'--log-file={folder / "agy_cli.log"}','--output-format=stream-json',
               f'--print-timeout={int(timeout)}s','--print',prompt]
        meta['agy_started_at'] = utc()
        agy_begin = time.monotonic()
        rc = run_process(cmd,SKILL_DIR,folder/'stdout.txt',folder/'stderr.log',timeout+5,cancelled,
                         folder/'agy_cli.log',authed)
        meta['agy_seconds'] = time.monotonic()-agy_begin
        raw = (folder/'stdout.txt').read_text(encoding='utf-8',errors='replace')
        err = (folder/'stderr.log').read_text(encoding='utf-8',errors='replace')
        result, response = judge(raw,err,rc,job,attachment)
        (folder/'response.txt').write_text(response)
        meta.update(exit_code=rc,validation=result,stdout_sha256=digest(folder/'stdout.txt'))
        if authed is not None and result.get('reason') not in ('auth','quota') and rc == 0:
            authed.set()
        if result['ok']:
            (folder/'transcript.md').write_text(result['clean']+'\n')
    except Exception as exc:
        meta.update(exit_code=1,validation=dict(ok=False,reason='local',warnings=[]),error=str(exc))
    finally:
        # tmp is the exact directory just created by this call.
        shutil.rmtree(tmp)
    meta.update(ended_at=utc(),elapsed_seconds=time.monotonic()-begin)
    save_json(folder/'result.json',meta)
    return meta


def schedule(jobs: list[dict], states: dict, worker, workers: int, max_attempts: int,
             publish, cancelled: threading.Event, backoff: float = 5,
             auth_gate: threading.Event | None = None) -> str | None:
    """A retry's backoff never occupies a worker or blocks other queued jobs.

    An exhausted account quota or an expired AGY login halts all dispatch and
    returns 'quota' or 'auth'; neither spends a chunk's attempt budget. Every
    AGY process that hits an expired login opens its own Google sign-in tab,
    so retrying would only open more tabs.

    With auth_gate, only one AGY process runs until the gate opens (AGY logged
    a successful sign-in, or a process finished cleanly). The first process
    refreshes a near-expiry Keychain token; the rest then read the fresh one
    instead of all refreshing at once and timing out together.
    """
    queue, serial, running, halted = [], 0, {}, None
    for job in jobs:
        state = states[job['id']]
        if state['status'] != 'machine_checked' and state['attempts'] < max_attempts:
            heapq.heappush(queue,(time.monotonic(),serial,job))
            serial += 1
    limit = max(1,workers)
    last_heartbeat = time.monotonic()
    with futures.ThreadPoolExecutor(max_workers=limit) as pool:
        while (queue or running) and not cancelled.is_set():
            now = time.monotonic()
            if now-last_heartbeat >= 30:
                publish()
                last_heartbeat=now
            ceiling = limit if auth_gate is None or auth_gate.is_set() else 1
            while queue and queue[0][0] <= now and len(running) < ceiling:
                _,_,job = heapq.heappop(queue)
                state = states[job['id']]
                state.update(status='running',attempts=state['attempts']+1,started_at=utc(),
                             folder_number=state.get('folder_number',state['attempts'])+1)
                running[pool.submit(worker,job,state['folder_number'])] = job
                publish()
            if not running:
                cancelled.wait(min(.2,max(0,queue[0][0]-time.monotonic())))
                continue
            done,_ = futures.wait(running,timeout=.2,return_when=futures.FIRST_COMPLETED)
            for future in done:
                job = running.pop(future)
                state = states[job['id']]
                try:
                    result = future.result()
                except Exception as exc:
                    result = dict(validation=dict(ok=False,reason='local',warnings=[]),error=str(exc))
                validation = result['validation']
                state.update(status='machine_checked' if validation['ok'] else 'failed',result=result,ended_at=utc())
                reason = validation.get('reason')
                if reason == 'capacity':
                    limit = max(1,limit//2)
                if reason in ('quota', 'auth'):
                    halted = halted or reason
                    queue.clear()
                    state.update(status=f'{reason}_wait',attempts=state['attempts']-1)
                    publish()
                    continue
                if not validation['ok'] and reason in RETRYABLE and state['attempts'] < max_attempts:
                    ready = time.monotonic() + backoff * 2**(state['attempts']-1) + (serial % 4)*backoff/4
                    heapq.heappush(queue,(ready,serial,job))
                    serial += 1
                    state['status'] = 'retry_queued'
                publish()
    # Executor shutdown waited for owned workers to return after cancellation.
    # Do not leave completed/cancelled children falsely displayed as running.
    for future,job in running.items():
        state=states[job['id']]
        try:
            result=future.result()
            state.update(result=result,status='machine_checked' if result['validation']['ok'] else 'interrupted',ended_at=utc())
        except Exception as exc:
            state.update(status='interrupted',error=str(exc),ended_at=utc())
    if running:
        publish()
    return halted


def restore(jobs: list[dict], root: Path, runtime_fingerprint: str) -> dict:
    states = {}
    for job in jobs:
        state = dict(status='queued',attempts=0,queued_at=utc())
        folders = sorted((root/'chunks'/job['id']).glob('attempt_*'))
        for folder in folders:
            state['folder_number'] = max(state.get('folder_number',0),int(folder.name.rsplit('_',1)[1]))
            result_file = folder/'result.json'
            if not result_file.is_file():
                state['attempts'] += 1
                state['status'] = 'interrupted'
                continue
            result = json.loads(result_file.read_text())
            if result.get('fingerprint') != runtime_fingerprint:
                raise ValueError('Attempt/runtime fingerprint mismatch.')
            if result.get('local_silence'):
                state.update(result=result,status='machine_checked',accepted_attempt=int(folder.name.rsplit('_',1)[1]))
                state['attempts'] += 1
                break
            if result['validation'].get('reason') == 'quota':
                state.update(result=result,status='quota_wait')
                continue
            state['attempts'] += 1
            state.update(result=result,status='failed')
            raw = folder/'stdout.txt'
            intact = raw.is_file() and result.get('stdout_sha256') == digest(raw)
            if result['validation']['ok'] and not intact:
                raise ValueError('Accepted raw output changed; refusing to resume from it.')
            if intact and 'attachment' in result:
                # Saved output is re-judged with the current rules, so a validator fix needs no new AGY request.
                again,_ = judge(raw.read_text(),(folder/'stderr.log').read_text(),result['exit_code'],job,result['attachment'])
                state['result'] = dict(result,validation=again)
                if again['ok']:
                    state.update(status='machine_checked',accepted_attempt=int(folder.name.rsplit('_',1)[1]))
                    break
        states[job['id']] = state
    return states


def summary(states: dict) -> dict:
    counts = {}
    for s in states.values():
        counts[s['status']] = counts.get(s['status'],0)+1
    return counts


def flat(blocks: list[dict], lo: float, hi: float) -> str:
    text = ''.join(re.sub(r'(?i)speaker\s*\d+\s*[:：]', '', STAMP.sub('', b['text']))
                   for b in blocks if lo <= b['time'] <= hi and spoken(b))
    return re.sub(r'[\W_]+', '', text.lower())


def similarity(a: str, b: str) -> float:
    left, right = {a[i:i+2] for i in range(len(a)-1)}, {b[i:i+2] for i in range(len(b)-1)}
    return len(left & right) / max(1, min(len(left), len(right)))


def scripts(blocks: list[dict]) -> tuple[int, int]:
    text = ''.join(b['text'] for b in blocks if spoken(b))
    return len(re.findall(r'[一-鿿]', text)), len(re.findall(r'[A-Za-z]{2,}', STAMP.sub('', re.sub(r'(?i)speaker\s*\d+', '', text))))


MISMATCH_NOTE = '> 与上一段重叠部分的内容对不上，其中一段可能不是录音原文，请听音核对。'
LANGUAGE_NOTE = '> 本段语言与整份录音不一致，可能不是录音原文，请听音核对。'


def assemble(manifest: dict, root: Path, states: dict, runtime: dict, recoveries: list | None = None) -> list[str]:
    """Build transcripts. Suspicion checks only flag for review; they never delete text."""
    replacement = {}
    for rec_manifest, rec_states in recoveries or []:
        for gi, group in enumerate(rec_manifest['parent']['groups']):
            jobs = [j for j in rec_manifest['jobs'] if j['group'] == gi]
            if jobs and all(rec_states[j['id']]['status'] == 'machine_checked' for j in jobs):
                for pid in group['replaces']:
                    replacement[pid] = (group, jobs, rec_states)
    review, paths = [], []
    for source in manifest['sources']:
        jobs = [j for j in manifest['jobs'] if j['source'] == source['id']]
        accepted = [states[j['id']]['result']['validation']['blocks'] for j in jobs
                    if states[j['id']]['status'] == 'machine_checked' and j['id'] not in replacement]
        cjk, latin = map(sum, zip(*[scripts(b) for b in accepted])) if accepted else (0, 0)
        chinese_source = cjk > 3 * latin
        output = ['---','model: agy_cli / '+runtime['model'],'review_status: machine_checked_unreviewed',
                  'source_sha256: '+source['sha256'],'---','', '# Transcript','',
                  '机器转写稿，未经人工听音校对；说话人编号只在各段内有效。标有“待核对”的位置请对照录音。','']
        prior, prior_job, rendered = [], None, set()
        for job in jobs:
            if job['id'] in replacement:
                group, rec_jobs, rec_states = replacement[job['id']]
                if id(group) in rendered:
                    continue
                rendered.add(id(group))
                output.extend([f'## {clock(group["start"])}–{clock(group["end"])}（短分段补转）',''])
                rec_prior, rec_prior_job = [], None
                for rj in rec_jobs:
                    validation = rec_states[rj['id']]['result']['validation']
                    blocks = validation['blocks']
                    if rec_prior_job and rec_prior_job['end'] > rj['start']:
                        blocks, boundary = overlap_merge(rec_prior, blocks, rj['start'], rec_prior_job['end'])
                        if boundary['review_needed']:
                            review.append(dict(job=rj['id'], recovery=True, reason='overlap', **boundary))
                    output.extend(b['text'].strip()+'\n' for b in blocks if meaningful(b))
                    review.extend(dict(job=rj['id'], recovery=True, reason=w) for w in validation['warnings'])
                    rec_prior, rec_prior_job = validation['blocks'], rj
                prior, prior_job = [], None
                continue
            state = states[job['id']]
            if state['status'] != 'machine_checked':
                output.extend([f'## {clock(job["start"])}–{clock(job["end"])}','', '[此段转写失败，待补齐]',''])
                review.append(dict(job=job['id'],reason='failed',start=job['start'],end=job['end']))
                prior,prior_job = [],None
                continue
            validation = state['result']['validation']
            blocks = validation['blocks']
            notes, boundary = [], None
            if prior_job and prior_job['end'] > job['start']:
                left, right = flat(prior, job['start']-3, prior_job['end']), flat(blocks, 0, prior_job['end'])
                if min(len(left), len(right)) >= 20 and similarity(left, right) < .15:
                    notes.append(MISMATCH_NOTE)
                    review.append(dict(job=job['id'], previous=prior_job['id'], reason='overlap_mismatch',
                                       start=prior_job['start'], end=job['end']))
                blocks,boundary = overlap_merge(prior,blocks,job['start'],prior_job['end'])
                if boundary['review_needed']:
                    notes.append('> 重叠段待核对；以下开头可能与上一段重复。')
                    review.append(dict(job=job['id'],reason='overlap',**boundary))
            zh, en = scripts(blocks)
            if chinese_source and en >= 15 and en > zh:
                notes.append(LANGUAGE_NOTE)
                review.append(dict(job=job['id'], reason='language_mismatch', start=job['start'], end=job['end']))
            output.extend([f'## {clock(job["start"])}–{clock(job["end"])}',''])
            for note in notes:
                output.extend([note,''])
            output.extend(b['text'].strip()+'\n' for b in blocks if meaningful(b))
            for warning in validation['warnings']:
                review.append(dict(job=job['id'],reason=warning))
            prior,prior_job = validation['blocks'],job
        path=root/f'{source["id"]}_transcript.md'
        path.write_text('\n'.join(output)+'\n')
        paths.append(str(path))
        for label,start in [('first',0),('middle',max(0,source['duration']/2-15)),('last',max(0,source['duration']-30))]:
            review.append(dict(source=source['id'],reason='source_comparison_required',sample=label,start=start,end=min(start+30,source['duration']),status='not_reviewed'))
    save_json(root/'review_queue.json',review)
    save_json(root/'deliverables.json',dict(transcripts=paths,review_queue=str(root/'review_queue.json'),review_status='machine_checked_unreviewed'))
    return paths


def load(out: Path) -> tuple[dict, Path, dict]:
    manifest = json.loads((out/'manifest.json').read_text())
    core = {k:v for k,v in manifest.items() if k!='fingerprint'}
    if fingerprint(core) != manifest['fingerprint']:
        raise ValueError('Manifest integrity check failed.')
    latest = out/'latest_run.json'
    if not latest.exists():
        return manifest, None, None
    root = Path(json.loads(latest.read_text())['path'])
    return manifest, root, json.loads((root/'runtime.json').read_text())


def recoveries_of(out: Path) -> list:
    found = []
    for folder in sorted((out/'recovery').glob('[0-9][0-9]')):
        manifest, root, runtime = load(folder)
        if root:
            found.append((manifest, restore(manifest['jobs'], root, runtime['fingerprint'])))
    return found


def deliver(out: Path, target: Path | None) -> None:
    """Rebuild the main transcript with every successful recovery; copy it to the target folder."""
    manifest, root, runtime = load(out)
    states = restore(manifest['jobs'], root, runtime['fingerprint'])
    paths = assemble(manifest, root, states, runtime, recoveries_of(out))
    if target:
        target.mkdir(parents=True, exist_ok=True)
        stems = [Path(s['path']).stem for s in manifest['sources']]
        for stem, path in zip(stems, paths):
            shutil.copyfile(path, target/f'{stem}_机器转写.md')
        name = f'{stems[0]}_待核对项.json' if len(stems) == 1 else '待核对项.json'
        shutil.copyfile(root/'review_queue.json', target/name)
        print(json.dumps(dict(delivered=str(target)), ensure_ascii=False))


def canary_jobs(jobs: list[dict]) -> list[dict]:
    """First chunk per source with clear local speech; the recording's opening is often silence."""
    chosen = {}
    for job in jobs:
        if job['source'] not in chosen or (not chosen[job['source']][1] and
                                           job.get('voiced_seconds', 0) >= .5*(job['end']-job['start'])):
            chosen[job['source']] = (job, job.get('voiced_seconds', 0) >= .5*(job['end']-job['start']))
    return [job for job, _ in chosen.values()]


def parent_canary_passed(manifest: dict) -> bool:
    parent = manifest.get('parent')
    if not parent:
        return False
    _, root, _ = load(Path(parent['out']))
    review = root and root/'canary_review.json'
    if not review or not review.exists():
        return False
    sources = json.loads(review.read_text()).get('sources', {})
    return bool(sources) and all(item.get('passed') is True for item in sources.values())


def quota_snapshot(binary: str) -> dict | None:
    """Remaining Gemini quota from `agy /usage`; no model request. None when unavailable."""
    try:
        data = json.loads(checked([binary, '--print', '/usage', '--output-format', 'json'], timeout=30))
        groups = data['command']['data']['groups']
        gemini = next(g for g in groups if 'gemini' in g['name'].lower())
        return {b['window']: dict(remaining=round(b['remaining_fraction'], 3), reset=b['reset_time'])
                for b in gemini['buckets']}
    except Exception:
        return None


def quota_message(states: dict) -> str:
    for state in states.values():
        detail = (state.get('result') or {}).get('validation', {}).get('detail', '')
        if state['status'] == 'quota_wait' and detail:
            return detail
    return 'AGY account quota exhausted.'


HALT_EXIT = dict(quota=4, auth=5)


def execute(out: Path, args) -> int:
    manifest, _, _ = load(out)
    for source in manifest['sources']:
        if digest(Path(source['path'])) != source['sha256']:
            raise ValueError('Source changed or moved since planning. Create a new plan/output directory.')
    if not str(SKILL_DIR).isascii() or any(c.isspace() for c in str(SKILL_DIR)) or not AGENT_FILE.is_file():
        raise RuntimeError(f'Bundled agent missing or skill path not ASCII without spaces: {AGENT_FILE}')
    binary = shutil.which('agy')
    if not binary:
        raise RuntimeError('agy not found')
    version = checked([binary,'--version']).strip()
    help_text = checked([binary,'--help'],include_stderr=True)
    for required in ('--add-dir','--print-timeout','--disable-slash-commands','--log-file','--agent','--output-format'):
        if required not in help_text:
            raise RuntimeError(f'Installed AGY lacks {required}; diagnose before dispatch.')
    listing = checked([binary,'models'])
    model = select_model(listing,args.model or os.environ.get('AGY_TRANSCRIPTION_MODEL'))
    runtime = dict(driver=VERSION,binary=binary,agy_version=version,model=model,
                   plan_fingerprint=manifest['fingerprint'],prompt_sha256=fingerprint(PROMPT),
                   agent_sha256=digest(AGENT_FILE))
    runtime['fingerprint'] = fingerprint(runtime)
    root = out/'runs'/runtime['fingerprint'][:16]
    root.mkdir(parents=True,exist_ok=True)
    import fcntl
    with (out/'.run.lock').open('a') as lock:
        try:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another driver is already running for this output directory.')
        save_json(root/'runtime.json',runtime)
        save_json(out/'latest_run.json',dict(path=str(root),fingerprint=runtime['fingerprint']))
        states = restore(manifest['jobs'],root,runtime['fingerprint'])
        cancelled = threading.Event()
        previous = {}
        for sig in (signal.SIGINT,signal.SIGTERM):
            previous[sig] = signal.signal(sig,lambda *_:cancelled.set())
        started = utc()
        t0 = time.monotonic()
        def publish():
            save_json(root/'status.json',dict(started_at=started,updated_at=utc(),invocation_seconds=time.monotonic()-t0,
                      fingerprint=runtime['fingerprint'],counts=summary(states),jobs=states,review_status='unreviewed'))
        authed = threading.Event()
        worker = lambda job, n: attempt(job,n,root,runtime,args.timeout,cancelled,authed)
        pending = [j for j in manifest['jobs'] if states[j['id']]['status'] != 'machine_checked' and not near_silent(j)]
        # Quota is metered by tokens, not by parallelism, so run every pending chunk at once (capped).
        workers = args.workers or max(1, min(20, len(pending)))
        quota = quota_snapshot(binary)
        if quota is not None:
            save_json(root/'quota_before.json', dict(checked_at=utc(), gemini=quota, pending_agy_chunks=len(pending)))
        low = quota and any(quota.get(w, {}).get('remaining', 1) < limit for w, limit in (('5h', .2), ('weekly', .1)))
        print(json.dumps(dict(status='quota_low' if low else 'quota', gemini=quota, pending_agy_chunks=len(pending),
                              workers=workers), ensure_ascii=False))
        def halt(reason: str) -> int:
            publish()
            resume = f'python3 {Path(__file__).resolve()} run --out {out}'
            if reason == 'auth':
                print(json.dumps(dict(status='auth_required',
                                      detail='AGY login expired. Sign in once in one of the opened Google tabs (or run agy interactively), close the rest, then resume.',
                                      resume=resume),ensure_ascii=False))
            else:
                print(json.dumps(dict(status='quota_exhausted',detail=quota_message(states),resume=resume),ensure_ascii=False))
            return HALT_EXIT[reason]
        try:
            if not parent_canary_passed(manifest):
                canaries = canary_jobs(manifest['jobs'])
                halted = schedule(canaries,states,worker,workers,min(2,args.max_attempts),publish,cancelled,
                                  auth_gate=authed)
                if halted:
                    return halt(halted)
                publish()
                if cancelled.is_set(): return 130
                if not all(states[j['id']]['status']=='machine_checked' for j in canaries): return 2
                review_file = root/'canary_review.json'
                if args.canary_only or not review_file.exists():
                    needed = {j['source']:dict(job=j['id'],stdout_sha256=states[j['id']]['result']['stdout_sha256'],
                              method='',evidence='',passed=False) for j in canaries}
                    save_json(root/'canary_review_required.json',dict(fingerprint=runtime['fingerprint'],sources=needed))
                    print(json.dumps(dict(status='canary_review_needed',run=str(root),template=str(root/'canary_review_required.json'))))
                    return 3
                reviewed = json.loads(review_file.read_text())
                if reviewed.get('fingerprint') != runtime['fingerprint']:
                    raise ValueError('Canary review belongs to a different runtime.')
                for job in canaries:
                    check = reviewed.get('sources',{}).get(job['source'],{})
                    if check.get('passed') is not True or not check.get('method') or not check.get('evidence') or check.get('stdout_sha256') != states[job['id']]['result']['stdout_sha256']:
                        raise ValueError('Canary requires a recorded source-content comparison, not just a format check.')
            halted = schedule(manifest['jobs'],states,worker,workers,args.max_attempts,publish,cancelled,
                              auth_gate=authed)
            if halted:
                return halt(halted)
            publish()
            if not manifest.get('parent'):
                deliver(out, getattr(args,'deliver',None))
            print(json.dumps(dict(run=str(root),counts=summary(states),review_status='unreviewed')))
            return 130 if cancelled.is_set() else (0 if all(s['status']=='machine_checked' for s in states.values()) else 2)
        finally:
            for sig,handler in previous.items(): signal.signal(sig,handler)


def parse_span(text: str) -> tuple[float, float]:
    def seconds(value: str) -> float:
        parts = [float(x) for x in value.split(':')]
        return sum(v*60**i for i, v in enumerate(reversed(parts)))
    a, b = text.split('-', 1)
    return seconds(a), seconds(b)


def recover(out: Path, args) -> int:
    """Re-transcribe failed, flagged or named spans of the main plan with short windows."""
    manifest, root, runtime = load(out)
    if not root:
        raise ValueError('Run the main plan before recover.')
    states = restore(manifest['jobs'], root, runtime['fingerprint'])
    chosen = {j['id'] for j in manifest['jobs'] if states[j['id']]['status'] != 'machine_checked'}
    if args.include_flagged and (root/'review_queue.json').exists():
        for item in json.loads((root/'review_queue.json').read_text()):
            if item.get('reason') in ('overlap_mismatch', 'language_mismatch') and not item.get('recovery'):
                chosen.add(item['job'])
                if item.get('previous'):
                    chosen.add(item['previous'])
    for a, b in map(parse_span, args.span or []):
        chosen |= {j['id'] for j in manifest['jobs'] if j['start'] < b and j['end'] > a}
    if not chosen:
        print(json.dumps(dict(status='nothing_to_recover')))
        return 0
    code = 0
    for source in manifest['sources']:
        jobs = [j for j in manifest['jobs'] if j['source'] == source['id'] and j['id'] in chosen]
        groups = []
        for job in jobs:
            if groups and job['index'] == groups[-1]['last'] + 1:
                groups[-1].update(end=job['end'], last=job['index'])
                groups[-1]['replaces'].append(job['id'])
            else:
                groups.append(dict(start=job['start'], end=job['end'], last=job['index'], replaces=[job['id']]))
        if not groups:
            continue
        folder = out/'recovery'/f'{len(list((out/"recovery").glob("[0-9][0-9]")))+1:02d}'
        plan([source['path']], folder, args.window, args.overlap, [(g['start'], g['end']) for g in groups],
             parent=dict(out=str(out), groups=[{k: g[k] for k in ('start', 'end', 'replaces')} for g in groups]))
        print(json.dumps(dict(recovery=str(folder), spans=[[clock(g['start']), clock(g['end'])] for g in groups])))
        result = execute(folder, args)
        if result in HALT_EXIT.values():
            return result
        code = code or result
    deliver(out, args.deliver)
    return code


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    p=commands.add_parser('plan',help='Local media inspection and persistent chunk plan; no upload')
    p.add_argument('sources',nargs='+'); p.add_argument('--out',required=True,type=Path)
    p.add_argument('--window',type=float,default=300); p.add_argument('--overlap',type=float,default=20)
    def dispatch_options(sub, window=None):
        sub.add_argument('--out',required=True,type=Path); sub.add_argument('--workers',type=int,help='Default: number of pending chunks, at most 20')
        sub.add_argument('--model'); sub.add_argument('--timeout',type=int,default=600)
        sub.add_argument('--max-attempts',type=int,choices=(1,2,3),default=2)
        sub.add_argument('--deliver',type=Path,help='Folder that receives the transcript and review list')
    r=commands.add_parser('run',help='Send planned audio to AGY; first invocation stops for canary review')
    dispatch_options(r); r.add_argument('--canary-only',action='store_true')
    v=commands.add_parser('recover',help='Re-transcribe failed, flagged or named spans with short windows')
    dispatch_options(v); v.add_argument('--window',type=float,default=60); v.add_argument('--overlap',type=float,default=10)
    v.add_argument('--span',action='append',help='START-END in seconds or HH:MM:SS; repeatable')
    v.add_argument('--include-flagged',action='store_true',help='Also redo overlap/language mismatch chunks')
    s=commands.add_parser('status',help='Read local status without contacting AGY')
    s.add_argument('--out',required=True,type=Path)
    args=parser.parse_args(); args.out=args.out.expanduser().resolve()
    if getattr(args,'deliver',None):
        args.deliver=args.deliver.expanduser().resolve()
    if args.command=='plan':
        m=plan(args.sources,args.out,args.window,args.overlap)
        print(json.dumps(dict(chunks=len(m['jobs']),sources=len(m['sources']),manifest=str(args.out/'manifest.json'))))
    elif args.command in ('run','recover'):
        if (args.workers is not None and not 1<=args.workers<=64) or not 30<=args.timeout<=1200:
            parser.error('workers must be 1..64 and timeout 30..1200 seconds')
        args.canary_only=getattr(args,'canary_only',False)
        return execute(args.out,args) if args.command=='run' else recover(args.out,args)
    else:
        latest=args.out/'latest_run.json'
        if not latest.exists():
            manifest=json.loads((args.out/'manifest.json').read_text())
            print(json.dumps(dict(status='planned',chunks=len(manifest['jobs']))))
        else:
            root=Path(json.loads(latest.read_text())['path'])
            status=json.loads((root/'status.json').read_text())
            print(json.dumps({k:v for k,v in status.items() if k!='jobs'},ensure_ascii=False))
    return 0


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as exc:
        print(f'error: {exc}',file=sys.stderr)
        raise SystemExit(2)
