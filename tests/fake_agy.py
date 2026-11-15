#!/usr/bin/env python3
"""Offline CLI protocol fixture. No network and no speech recognition.

FAKE_AGY_FAIL_START=HH:MM:SS makes the chunk starting there answer without
opening its audio, which the driver must reject as not_listened.
"""
import json
import os
import re
import sys
import wave
from pathlib import Path

args=sys.argv[1:]
if args==['--version']:
    print('offline-fixture-2')
elif args==['--help']:
    print('--add-dir --agent --output-format --print-timeout --disable-slash-commands --log-file --print')
elif args==['models']:
    print('gemini-3.8-flash-medium\tOffline fixture')
elif '/usage' in args:
    bucket=lambda w,r: dict(id=w,window=w,remaining_fraction=r,reset_time='2026-10-01T16:00:00Z')
    print(json.dumps(dict(status='SUCCESS',command=dict(name='usage',data=dict(groups=[
        dict(name='Gemini Models',buckets=[bucket('weekly',float(os.environ.get('FAKE_AGY_WEEKLY','0.9'))),bucket('5h',.8)])])))))
else:
    index=args.index('--print')
    assert index==len(args)-2, 'All flags must precede print.'
    assert '--agent=transcriber' in args and '--output-format=stream-json' in args
    assert (Path.cwd()/'.agents/agents/transcriber.md').is_file(), 'Must run from the skill directory.'
    prompt=args[index+1]
    audio=Path(re.search(r'audio file: (\S+)',prompt)[1])
    assert audio.is_file() and str(audio).isascii()
    assert f'--add-dir={audio.parent}' in args
    with wave.open(str(audio),'rb') as stream:
        assert stream.getframerate()==16000 and stream.getnchannels()==1
    start=re.search(r'interval: (\d{2}:\d{2}:\d{2})',prompt)[1]
    emit=lambda value: print(json.dumps(value))
    emit(dict(event='init',init=dict(agent='transcriber',tools=['view_file'])))
    if os.environ.get('FAKE_AGY_FAIL_START')!=start:
        emit(dict(event='step_update',step_update=dict(step_type='tool',state='DONE',tool_name='view_file',
                                                        tool_info=dict(name='view_file',parameters=dict(AbsolutePath=str(audio))))))
    body=(f'# Chunk Transcript\n\n[{start}] Speaker 1: Offline fixture output, not an actual transcription.\n'
          '\n# Unclear Sections\nNone\n\n# Quality Notes\nNo network or speech recognition was used.\n')
    emit(dict(event='result',result=dict(status='SUCCESS',response=body,num_turns=1)))
