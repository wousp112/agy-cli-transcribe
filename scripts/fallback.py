#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["google-genai>=2.34", "numpy", "httpx"]
# ///
"""API routes for when AGY fails or does not fit. Run a route only after the user agreed.

Routes (see references/routing.md for when to recommend which):
  elevenlabs         ElevenLabs Scribe v2, whole file in one request: speaker labels
                     consistent across the recording, word timestamps, best on
                     Chinese-English mixed speech in the 2026-10-31 test. Free plan
                     ≈ 150 audio minutes/month; paid $0.22/hour.
  gemini-transcribe  gemini-*-transcribe file endpoint, ~4.5-minute segments with
                     speaker labels (renumbered per segment) and word timestamps.
                     Free tier 25 requests/day. Pure Chinese only: on mixed audio it
                     dropped the Chinese part.
  gemini-live        gemini-*-transcribe-live. Unlimited requests, ~5 min per 46-min
                     recording; no speaker labels, one timestamp per ~2 minutes.

Commands:
  estimate AUDIO...  local estimate for every route plus remaining quota; uploads no audio
  models             Gemini model IDs each Gemini route would use now
  elevenlabs | gemini-transcribe | gemini-live AUDIO --deliver DIR

Keys: $ELEVENLABS_API_KEY / $GEMINI_API_KEY, else macOS Keychain items
service=elevenlabs-api-key / service=gemini-api-key.
"""
import argparse, asyncio, json, os, re, subprocess, sys, time
from pathlib import Path

import httpx
import numpy as np
from google import genai
from google.genai import errors, types

BPS = 32000            # 16 kHz mono s16le
CHUNK = 3200           # 100 ms
# Free-tier numbers read from AI Studio on 2026-10-31; override with flags if they change.
TRANSCRIBE_RPM, TRANSCRIBE_TPM, TRANSCRIBE_RPD = 3, 10_000, 25
LIVE_REALTIME_FACTOR = 9      # measured: 46-47 min audio in ~300 s with 4 sessions at 3x
TOKENS_PER_SECOND = 32        # gemini-3.5-transcribe countTokens on 2026-10-31; gemini-transcribe re-measures
ELEVEN_CREDITS_PER_MINUTE = 900 / 13.5   # 13.5 audio minutes cost 900 credits on the free plan, 2026-10-31
ELEVEN_USD_PER_HOUR = 0.22
ELEVEN_SPEED_FACTOR = 8       # measured: 270 s of audio in ~33 s
ELEVEN_API = 'https://api.elevenlabs.io/v1'


def key(env: str, service: str) -> str:
    if os.environ.get(env):
        return os.environ[env]
    try:
        return subprocess.run(['security', 'find-generic-password', '-s', service, '-w'],
                              check=True, capture_output=True, text=True).stdout.strip()
    except subprocess.CalledProcessError:
        sys.exit(f'No API key: set {env} or add Keychain item service={service}.')


def gemini_client():
    return genai.Client(api_key=key('GEMINI_API_KEY', 'gemini-api-key'))


def version(name: str) -> tuple:
    return tuple(int(x) for x in re.search(r'gemini-([\d.]+)-', name)[1].split('.'))


def pick_models(client) -> dict:
    """Newest transcribe models the key can see; a stable ID beats a preview of the same version."""
    found = {'gemini-live': [], 'gemini-transcribe': []}
    for m in client.models.list():
        name = m.name.removeprefix('models/')
        methods = m.supported_actions or []
        if re.fullmatch(r'gemini-[\d.]+-transcribe-live(-preview)?', name) and 'bidiGenerateContent' in methods:
            found['gemini-live'].append(name)
        elif re.fullmatch(r'gemini-[\d.]+-transcribe(-preview)?', name) and 'generateContent' in methods:
            found['gemini-transcribe'].append(name)
    pick = lambda names: max(names, key=lambda n: (version(n), 'preview' not in n)) if names else None
    return {route: pick(names) for route, names in found.items()} | {'available': found}


def to_pcm(audio: Path) -> bytes:
    return subprocess.run(['ffmpeg', '-loglevel', 'error', '-i', str(audio), '-ac', '1', '-ar', '16000',
                           '-f', 's16le', '-'], check=True, capture_output=True).stdout


def split(pcm: bytes, seg_sec: float, search: float = 15) -> list[tuple[int, bytes]]:
    """Cut near every seg_sec at the quietest 100 ms within ±search seconds."""
    x = np.frombuffer(pcm[:len(pcm) // CHUNK * CHUNK], dtype='<i2').astype(np.float64)
    rms = np.sqrt((x.reshape(-1, 1600) ** 2).mean(axis=1))
    cuts, t, total = [0], seg_sec, len(rms) / 10
    while t < total - 10:
        lo, hi = int((t - search) * 10), min(int((t + search) * 10), len(rms))
        best = lo + int(np.argmin(rms[lo:hi]))
        cuts.append(best * CHUNK)
        t = best / 10 + seg_sec
    cuts.append(len(pcm))
    return [(cuts[i], pcm[cuts[i]:cuts[i + 1]]) for i in range(len(cuts) - 1)]


def clock(seconds: float) -> str:
    s = int(seconds)
    return f'{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}'


def wav(pcm: bytes) -> bytes:
    import io, wave
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(pcm)
    return buf.getvalue()


def join_words(words: list[str]) -> str:
    """Gemini returns words without spacing: put a space between two Latin words, none around CJK."""
    out = ''
    for w in words:
        if out and re.match(r'[A-Za-z0-9]', w) and re.search(r'[A-Za-z0-9.,?!;:\'"%)]$', out):
            out += ' '
        out += w
    return out


def speaker(label: str) -> str:
    """Gemini `spk:0` and ElevenLabs `speaker_0` both become 说话人1."""
    m = re.search(r'(\d+)$', label or '')
    if not m:
        return ''
    n = int(m[1]) + (0 if label.startswith('spk_') else 1)
    return f'说话人{n}：'


def write_out(audio: Path, deliver: Path, route: str, model: str, header: list[str], body: list[str]) -> Path:
    deliver.mkdir(parents=True, exist_ok=True)
    path = deliver / f'{audio.stem}_机器转写_{route}.md'
    path.write_text('\n'.join([f'# {audio.stem}（{route} 转写）', '',
                               f'- 模型：`{model}`；状态：machine_checked_unreviewed', *header, '', *body, '']))
    return path


TRANSIENT = (errors.ServerError, httpx.TransportError)


# ---------------------------------------------------------------- elevenlabs

def run_elevenlabs(audio: Path, args) -> Path:
    state_dir = args.deliver / '转写过程_ElevenLabs'
    state_dir.mkdir(parents=True, exist_ok=True)
    raw_path = state_dir / f'{audio.stem}.json'
    t0 = time.time()
    if raw_path.exists():
        data = json.loads(raw_path.read_text())
    else:
        form = dict(model_id=args.model, diarize='true', timestamps_granularity='word', tag_audio_events='false')
        if args.language:
            form['language_code'] = args.language
        for tries in range(3):
            try:
                with audio.open('rb') as f:
                    r = httpx.post(f'{ELEVEN_API}/speech-to-text', headers={'xi-api-key': key('ELEVENLABS_API_KEY', 'elevenlabs-api-key')},
                                   data=form, files={'file': (audio.name, f)}, timeout=3600)
                if r.status_code >= 500 and tries < 2:
                    time.sleep(10 * (tries + 1)); continue
                break
            except httpx.TransportError:
                if tries == 2:
                    raise
                time.sleep(10 * (tries + 1))
        if r.status_code != 200:
            status = 'quota_exhausted' if r.status_code in (401, 402, 429) or 'quota' in r.text.lower() else 'error'
            print(json.dumps(dict(status=status, http=r.status_code, detail=r.text[:300]), ensure_ascii=False))
            sys.exit(4 if status == 'quota_exhausted' else 1)
        data = r.json()
        raw_path.write_text(json.dumps(data, ensure_ascii=False))
    turns, cur = [], None
    for w in data.get('words', []):
        if w.get('type') == 'audio_event':
            continue
        if cur is None or w.get('speaker_id') != cur['spk']:
            cur = dict(spk=w.get('speaker_id', ''), start=w.get('start', 0), text='')
            turns.append(cur)
        cur['text'] += w['text']
    body = [f'[{clock(t["start"])}] {speaker(t["spk"])}{t["text"].strip()}' for t in turns if t['text'].strip()]
    header = [f'- 方式：整份录音一次请求；识别语言 {data.get("language_code")}（自动识别时只报主语言，中英混说仍会转出两种语言）',
              '- 说话人编号在整份录音内一致；每句时间戳来自逐词时间',
              f'- 用时 {time.time() - t0:.0f} 秒']
    return write_out(audio, args.deliver, 'ElevenLabs', args.model, header, body)


def eleven_quota() -> dict | None:
    try:
        d = httpx.get(f'{ELEVEN_API}/user/subscription', headers={'xi-api-key': key('ELEVENLABS_API_KEY', 'elevenlabs-api-key')},
                      timeout=20).json()
        left = d['character_limit'] - d['character_count']
        return dict(tier=d['tier'], credits_left=left, est_minutes_left=round(left / ELEVEN_CREDITS_PER_MINUTE))
    except Exception:
        return None


# ---------------------------------------------------------------- gemini live

async def live_segment(client, model: str, pcm: bytes, lang: list[str], speed: float, idle: float = 8) -> str:
    config = types.LiveConnectConfig(response_modalities=['TEXT'],
                                     input_audio_transcription=types.AudioTranscriptionConfig(language_codes=lang))
    finals, last, done, t0 = [], [time.time()], asyncio.Event(), time.time()
    async with client.aio.live.connect(model=model, config=config) as s:
        async def send():
            for i in range(0, len(pcm), CHUNK):
                await s.send_realtime_input(audio=types.Blob(data=pcm[i:i + CHUNK], mime_type='audio/pcm;rate=16000'))
                delay = t0 + (i + CHUNK) / BPS / speed - time.time()
                if delay > 0:
                    await asyncio.sleep(delay)
            await s.send_realtime_input(audio_stream_end=True)
            last[0] = time.time()
            done.set()

        async def recv():
            async for r in s.receive():
                last[0] = time.time()
                sc = r.server_content
                if sc and sc.input_transcription and sc.input_transcription.text:
                    finals.append(sc.input_transcription.text)

        async def watchdog():
            await done.wait()
            while time.time() - last[0] < idle:
                await asyncio.sleep(0.5)

        rt = asyncio.create_task(recv())
        await asyncio.gather(send(), watchdog())
        rt.cancel()
    return ''.join(finals)


async def run_live(client, model, audio: Path, args) -> Path:
    segs = split(to_pcm(audio), args.segment)
    sem, t0, failed = asyncio.Semaphore(args.parallel), time.time(), []
    lang = [args.language] if args.language else []

    async def one(i, start, pcm):
        async with sem:
            for attempt in range(3):
                try:
                    text = await live_segment(client, model, pcm, lang, args.speed)
                    print(json.dumps(dict(segment=i, start=clock(start / BPS), chars=len(text), attempt=attempt + 1)), flush=True)
                    return f'[{clock(start / BPS)}] {text}'
                except Exception as e:
                    print(json.dumps(dict(segment=i, error=f'{type(e).__name__}: {str(e)[:120]}', attempt=attempt + 1)), flush=True)
            failed.append(clock(start / BPS))
            return f'[{clock(start / BPS)}] [[本段转写失败]]'

    body = await asyncio.gather(*(one(i, st, p) for i, (st, p) in enumerate(segs)))
    header = [f'- 方式：Live API，约 {args.segment:.0f} 秒一段，在停顿处切分；每段只有开头一个时间戳',
              '- 没有说话人标注；不同人的话连在同一段里',
              f'- 失败段：{", ".join(failed) if failed else "无"}；用时 {time.time() - t0:.0f} 秒']
    return write_out(audio, args.deliver, 'Gemini-Live', model, header, body)


# ---------------------------------------------------------- gemini transcribe

def transcribe_chunk(client, model, pcm: bytes, lang, diarize: bool) -> list[dict]:
    cfg = types.AudioTranscriptionConfig(language_codes=lang, diarization=diarize, word_timestamp=diarize)
    r = client.models.generate_content(model=model, contents=[types.Part.from_bytes(data=wav(pcm), mime_type='audio/wav')],
                                       config=types.GenerateContentConfig(audio_transcription_config=cfg))
    turns = []
    for cand in r.candidates or []:
        for part in (cand.content.parts if cand.content else []) or []:
            tr = part.audio_transcription
            if tr and tr.words:
                start = tr.words[0].start_offset
                turns.append(dict(speaker=tr.speaker_label or '', start=start.total_seconds() if hasattr(start, 'total_seconds') else float(str(start).rstrip('s') or 0),
                                  text=join_words([w.word for w in tr.words])))
    if not turns and r.text:
        turns.append(dict(speaker='', start=0.0, text=r.text))
    return turns


def tokens_per_second(client, model, pcm: bytes) -> float:
    probe = pcm[:60 * BPS]
    counted = client.models.count_tokens(model=model, contents=[types.Part.from_bytes(data=wav(probe), mime_type='audio/wav')])
    return counted.total_tokens / (len(probe) / BPS)


def run_transcribe(client, model, audio: Path, args) -> Path:
    pcm = to_pcm(audio)
    tps = tokens_per_second(client, model, pcm)
    seg_sec = min(args.segment, 0.9 * args.tpm / tps)   # one request must fit the per-minute token limit
    segs = split(pcm, seg_sec)
    state_dir = args.deliver / '转写过程_Gemini-Transcribe' / audio.stem
    state_dir.mkdir(parents=True, exist_ok=True)
    lang, sent, body, failed, t0 = ([args.language] if args.language else []), [], [], [], time.time()
    for i, (start, pcm) in enumerate(segs):
        done = state_dir / f'seg_{i:03d}.json'
        if done.exists():
            turns = json.loads(done.read_text())
        else:
            need = len(pcm) / BPS * tps
            while True:   # stay under both requests/minute and tokens/minute
                recent = [(t, n) for t, n in sent if time.time() - t < 60]
                if len(recent) < args.rpm and sum(n for _, n in recent) + need <= args.tpm:
                    break
                time.sleep(1)
            try:
                for tries in range(3):
                    sent.append((time.time(), need))
                    try:
                        turns = transcribe_chunk(client, model, pcm, lang, not args.no_diarization)
                        break
                    except TRANSIENT:
                        if tries == 2:
                            raise
                        time.sleep(15 * (tries + 1))   # dropped connection or 5xx: same segment again
                    except errors.ClientError as e:
                        if e.code != 429 or tries:
                            raise
                        time.sleep(65)   # a per-minute limit clears; a daily one fails again
                done.write_text(json.dumps(turns, ensure_ascii=False))
            except errors.ClientError as e:
                if e.code == 429:
                    print(json.dumps(dict(status='quota_exhausted', segment=i, detail=str(e)[:300],
                                          resume='rerun the same command after the daily reset (midnight Pacific)')), flush=True)
                    failed.append(clock(start / BPS)); body.append(f'## {clock(start / BPS)}\n[[额度用完，本段及之后未转]]')
                    break
                raise
        print(json.dumps(dict(segment=i, start=clock(start / BPS), turns=len(turns))), flush=True)
        body.append(f'## {clock(start / BPS)}')
        body += [f'[{clock(start / BPS + t["start"])}] {speaker(t["speaker"])}{t["text"]}' for t in turns]
    header = [f'- 方式：文件接口，约 {seg_sec:.0f} 秒一段（每秒音频约 {tps:.0f} token）；' + ('开启说话人区分和逐词时间戳' if not args.no_diarization else '未开启说话人区分'),
              '- 说话人编号只在同一段内有效：下一段的"说话人1"不一定是同一个人',
              f'- 未完成段：{", ".join(failed) if failed else "无"}；用时 {time.time() - t0:.0f} 秒']
    return write_out(audio, args.deliver, 'Gemini-Transcribe', model, header, body)


# ------------------------------------------------------------------ commands

def duration(audio: Path) -> float:
    return float(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(audio)],
                                check=True, capture_output=True, text=True).stdout)


def estimate(audios: list[Path], args) -> dict:
    total = sum(duration(a) for a in audios)
    minutes = total / 60
    seg = min(270, 0.9 * args.tpm / TOKENS_PER_SECOND)   # no audio leaves the machine before the user agrees
    requests = int(-(-total // seg))
    quota = eleven_quota()
    try:
        models = pick_models(gemini_client())
    except SystemExit:
        models = {}
    return dict(
        audio_minutes=round(minutes, 1),
        elevenlabs=dict(model=args.model, quota=quota,
                        fits_free_quota=(quota['est_minutes_left'] >= minutes) if quota else None,
                        paid_cost_usd=round(minutes / 60 * ELEVEN_USD_PER_HOUR, 2),
                        est_minutes=round(minutes / ELEVEN_SPEED_FACTOR, 1),
                        speaker_labels='whole recording', timestamps='per word'),
        gemini_transcribe=dict(model=models.get('gemini-transcribe'), segment_seconds=round(seg), requests=requests,
                               requests_per_day=args.rpd, fits_today=requests <= args.rpd,
                               est_minutes=round(max(requests / args.rpm, total * TOKENS_PER_SECOND / args.tpm), 1),
                               speaker_labels='per ~4.5-min segment', timestamps='per word',
                               warning='pure Chinese only; dropped the Chinese part of mixed audio'),
        gemini_live=dict(model=models.get('gemini-live'), est_minutes=round(minutes / LIVE_REALTIME_FACTOR, 1),
                         speaker_labels=False, timestamps='one per ~2 minutes'))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('models')
    for name in ('estimate', 'elevenlabs', 'gemini-transcribe', 'gemini-live'):
        c = sub.add_parser(name)
        c.add_argument('audio', nargs='+' if name == 'estimate' else None, type=Path)
        if name in ('estimate', 'elevenlabs'):
            c.add_argument('--model', default=os.environ.get('ELEVENLABS_STT_MODEL', 'scribe_v2'),
                           help='ElevenLabs model ID (default scribe_v2)')
        else:
            c.add_argument('--model', help='override the auto-picked Gemini model ID')
        c.add_argument('--language', default='' if name in ('estimate', 'elevenlabs') else 'cmn-Hans-CN',
                       help='language hint; empty = auto-detect (ElevenLabs default). Gemini needs a hint')
        c.add_argument('--segment', type=float, default=270 if name == 'gemini-transcribe' else 120)
        c.add_argument('--rpm', type=int, default=TRANSCRIBE_RPM)
        c.add_argument('--tpm', type=int, default=TRANSCRIBE_TPM)
        c.add_argument('--rpd', type=int, default=TRANSCRIBE_RPD)
        if name != 'estimate':
            c.add_argument('--deliver', type=Path, required=True, help='folder that receives the transcript')
        if name == 'gemini-live':
            c.add_argument('--speed', type=float, default=3, help='times realtime; 10x dropped speech in tests')
            c.add_argument('--parallel', type=int, default=4)
        if name == 'gemini-transcribe':
            c.add_argument('--no-diarization', action='store_true')
    args = p.parse_args()
    if args.cmd == 'models':
        print(json.dumps(pick_models(gemini_client()), ensure_ascii=False, indent=2)); return 0
    if args.cmd == 'estimate':
        print(json.dumps(estimate([a.expanduser().resolve() for a in args.audio], args), ensure_ascii=False, indent=2)); return 0
    audio, args.deliver = args.audio.expanduser().resolve(), args.deliver.expanduser().resolve()
    if args.cmd == 'elevenlabs':
        out, model = run_elevenlabs(audio, args), args.model
    else:
        client = gemini_client()
        model = args.model or pick_models(client)[args.cmd]
        if not model:
            sys.exit(f'No {args.cmd} model visible to this key.')
        out = asyncio.run(run_live(client, model, audio, args)) if args.cmd == 'gemini-live' else run_transcribe(client, model, audio, args)
    print(json.dumps(dict(delivered=str(out), model=model), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
