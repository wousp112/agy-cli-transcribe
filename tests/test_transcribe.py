"""Offline regression tests for failures observed in transcription histories."""
import importlib.util
import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest import mock
from argparse import Namespace

spec=importlib.util.spec_from_file_location('transcribe',Path(__file__).resolve().parents[1]/'scripts/transcribe.py')
tx=importlib.util.module_from_spec(spec)
spec.loader.exec_module(tx)

def raw(body='[00:00:00] Speaker 1: A real spoken sentence.'):
    return '# Chunk Transcript\n\n'+body+'\n\n# Unclear Sections\nNone\n# Quality Notes\nMachine output.\n'

def job(name='a',start=0,end=300,**extra):
    return dict(id=name,start=start,end=end,**extra)

def stream(response,audio='/private/tmp/agy_tx_x/a.wav',tools=('view_file',)):
    lines=[json.dumps(dict(event='init'))]
    for name in tools:
        params=dict(AbsolutePath=audio) if name=='view_file' else dict(CommandLine='ls')
        lines.append(json.dumps(dict(event='step_update',step_update=dict(step_type='tool',state='DONE',tool_name=name,tool_info=dict(parameters=params)))))
    lines.append(json.dumps(dict(event='result',result=dict(status='SUCCESS',response=response))))
    return '\n'.join(lines)+'\n'

class Regression(unittest.TestCase):
    def test_actual_45_chunks(self):
        chunks=tx.spans(12616.655238)
        self.assertEqual(len(chunks),45)
        self.assertEqual(chunks[-1],(12320,12616.655238))
        self.assertTrue(all(a[1]-b[0]==20 for a,b in zip(chunks,chunks[1:])))

    def test_overlap_count_not_naive_ceiling(self):
        self.assertEqual(len(tx.spans(1790,900,20)),3)
        self.assertEqual(tx.spans(900,900,20),[(0,900)])
        self.assertEqual(tx.spans(.4),[(0,.4)])

    def test_bad_planner_inputs(self):
        for args in [(0,300,20),(20,20,20),(float('nan'),300,20),(20,300,-1)]:
            with self.assertRaises(ValueError): tx.spans(*args)

    def test_available_model_selection(self):
        text='gemini-3.8-flash-high\tHigh\ngemini-3.8-flash-medium\tM\ngemini-3.10-flash-medium\tM\n'
        self.assertEqual(tx.select_model(text,None),'gemini-3.10-flash-medium')
        self.assertEqual(tx.select_model(text,'gemini-3.8-flash-high'),'gemini-3.8-flash-high')
        with self.assertRaises(ValueError): tx.select_model(text,'invented-model')

    def test_failed_exit_cannot_be_success(self):
        self.assertFalse(tx.validate(raw(),'',1,job())['ok'])

    def test_empty_zero_exit_cannot_be_success(self):
        self.assertEqual(tx.validate('','',0,job())['reason'],'empty')

    def test_terminal_no_output_warning(self):
        self.assertFalse(tx.validate(raw(),'warning: run ended with no output and no recorded error',0,job())['ok'])

    def test_initialization_warning_not_terminal(self):
        result=tx.validate(raw(),'W0922 Singleflight refresh: You are not logged into Antigravity.',0,job())
        self.assertTrue(result['ok'])

    def test_valid_audio_can_discuss_mcp(self):
        self.assertTrue(tx.validate(raw('[00:00:00] Speaker 1: We built an MCP server.'),'',0,job())['ok'])

    def test_spoken_error_messages_are_not_cli_failures(self):
        value=raw('[00:00:00] Speaker 1: The message said broken pipe and Too Many Requests.')
        self.assertTrue(tx.validate(value,'',0,job())['ok'])

    def test_safe_markdown_fence(self):
        self.assertTrue(tx.validate('```markdown\n'+raw()+'```','',0,job())['ok'])

    def test_preamble_preserved_in_original(self):
        value='5'+raw()
        self.assertTrue(tx.validate(value,'',0,job())['ok'])
        self.assertTrue(value.startswith('5#'))
        # Leaked prose before a complete answer is dropped; tool use is judged from AGY events.
        leaked=tx.validate('Everything heard clearly.\nLet me draft.\n'+raw(),'',0,job())
        self.assertTrue(leaked['ok'])
        self.assertNotIn('draft',leaked['clean'])
        self.assertFalse(tx.validate('+00:04:40.000\n[00:04:40] Speaker 1: 只剩后半段。','',0,job())['ok'])

    def test_end_marker_does_not_prove_coverage(self):
        result=tx.validate(raw('[00:00:00] Speaker 1: Hello.\n[00:05:00]'),'',0,job())
        self.assertTrue(result['ok'])
        self.assertEqual(result['last_content_timestamp'],0)
        self.assertIn('early_last_turn_check_audio_not_proof_of_missing_speech',result['warnings'])

    def test_bad_timestamps(self):
        for body in ['[00:01] Speaker 1: Hello.','[00:66:00] Speaker 1: Hello.',
                     '[00:01:00] Speaker 1: A.\n[00:00:00] Speaker 1: B.']:
            self.assertFalse(tx.validate(raw(body),'',0,job())['ok'])

    def test_timestamp_cutoff_cannot_delete_unique_words(self):
        prev=[dict(time=278,text='[00:04:38] Speaker 1: Unique beginning of a long utterance.')]
        curr=[dict(time=280,text='[00:04:40] Speaker 2: A different continuation that must survive.')]
        kept,note=tx.overlap_merge(prev,curr,280,300)
        self.assertEqual(kept,curr)
        self.assertTrue(note['review_needed'])

    def test_exact_overlap_is_deduplicated(self):
        phrase='Speaker 1: This sentence really is identical across both windows.'
        prev=[dict(time=281,text='[00:04:41] '+phrase)]
        curr=[dict(time=280,text='[00:04:40] '+phrase),dict(time=304,text='[00:05:04] Speaker 2: New content.')]
        kept,note=tx.overlap_merge(prev,curr,280,300)
        self.assertEqual(len(kept),1)
        self.assertEqual(note['removed_exact_blocks'],1)
        self.assertFalse(note['review_needed'])

    def test_short_repeated_words_not_deleted(self):
        a=[dict(time=281,text='[00:04:41] A: Yes.')]
        b=[dict(time=282,text='[00:04:42] A: Yes.')]
        self.assertEqual(tx.overlap_merge(a,b,280,300)[0],b)

    def test_freed_slot_immediately_starts_next(self):
        started,ended={},{}
        jobs=[job('slow'),job('fast'),job('next')]
        states={j['id']:dict(status='queued',attempts=0) for j in jobs}
        def worker(j,n):
            started[j['id']]=time.monotonic()
            time.sleep(.15 if j['id']=='slow' else .015)
            ended[j['id']]=time.monotonic()
            return dict(validation=dict(ok=True,reason=None,warnings=[]))
        tx.schedule(jobs,states,worker,2,2,lambda:None,threading.Event(),backoff=0)
        self.assertLess(started['next'],ended['slow'])
        self.assertTrue(all(s['status']=='machine_checked' for s in states.values()))

    def test_retry_does_not_block_queue(self):
        order=[]
        jobs=[job('retry'),job('next')]
        states={j['id']:dict(status='queued',attempts=0) for j in jobs}
        def worker(j,n):
            order.append((j['id'],n))
            good=j['id']!='retry' or n>1
            return dict(validation=dict(ok=good,reason=None if good else 'network',warnings=[]))
        tx.schedule(jobs,states,worker,1,2,lambda:None,threading.Event(),backoff=.01)
        self.assertEqual(order,[('retry',1),('next',1),('retry',2)])

    def test_retry_is_bounded_and_failure_persists(self):
        states={'a':dict(status='queued',attempts=0)}
        def worker(j,n): return dict(validation=dict(ok=False,reason='empty',warnings=[]))
        tx.schedule([job()],states,worker,1,2,lambda:None,threading.Event(),backoff=0)
        self.assertEqual(states['a']['attempts'],2)
        self.assertEqual(states['a']['status'],'failed')

    def test_nonretryable_failure_does_not_loop(self):
        states={'a':dict(status='queued',attempts=0)}
        tx.schedule([job()],states,lambda j,n:dict(validation=dict(ok=False,reason='media')),1,3,lambda:None,threading.Event(),backoff=0)
        self.assertEqual(states['a']['attempts'],1)

    def test_resume_verifies_immutable_raw(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); folder=root/'chunks/a/attempt_001'; folder.mkdir(parents=True)
            text=stream(raw()); (folder/'stdout.txt').write_text(text); (folder/'stderr.log').write_text('')
            tx.save_json(folder/'result.json',dict(fingerprint='fp',exit_code=0,attachment='a.wav',stdout_sha256=tx.digest(folder/'stdout.txt'),validation=tx.judge(text,'',0,job(),'a.wav')[0]))
            states=tx.restore([job()],root,'fp')
            self.assertEqual(states['a']['status'],'machine_checked')
            (folder/'stdout.txt').write_text(text+'changed')
            with self.assertRaises(ValueError): tx.restore([job()],root,'fp')

    def test_plan_does_not_overwrite_another_source(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); audio=root/'sound.wav'; audio.write_bytes(b'fixture')
            with mock.patch.object(tx,'checked',return_value='600'):
                a=tx.plan([str(audio)],root/'out',300,20)
                self.assertEqual(tx.plan([str(audio)],root/'out',300,20),a)
                audio.write_bytes(b'changed')
                with self.assertRaises(ValueError): tx.plan([str(audio)],root/'out',300,20)

    def test_process_timeout_preserves_logs(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            rc=tx.run_process([sys.executable,'-c','import time; print("started",flush=True); time.sleep(5)'],root,root/'out.txt',root/'err.txt',.1,threading.Event())
            self.assertEqual(rc,124)
            self.assertIn('started',(root/'out.txt').read_text())

    def test_help_may_be_on_stderr(self):
        result=subprocess.CompletedProcess([],0,stdout='',stderr='--add-dir')
        with mock.patch.object(tx.subprocess,'run',return_value=result):
            self.assertIn('--add-dir',tx.checked(['agy','--help'],include_stderr=True))

    def test_cancellation_does_not_leave_running_status(self):
        stop=threading.Event()
        states={'a':dict(status='queued',attempts=0)}
        def worker(j,n):
            stop.set()
            return dict(validation=dict(ok=False,reason='process',warnings=[]))
        tx.schedule([job()],states,worker,1,2,lambda:None,stop,backoff=0)
        self.assertNotEqual(states['a']['status'],'running')

    # Failures seen in the 2026-09-28 run.
    def test_answer_without_opening_audio_is_rejected(self):
        self.assertEqual(tx.judge(stream(raw(),tools=()),'',0,job(),'a.wav')[0]['reason'],'not_listened')
        self.assertEqual(tx.judge(stream(raw(),audio='/tmp/other.wav'),'',0,job(),'a.wav')[0]['reason'],'not_listened')
        self.assertTrue(tx.judge(stream(raw()),'',0,job(),'a.wav')[0]['ok'])

    def test_other_tools_are_rejected(self):
        verdict=tx.judge(stream(raw(),tools=('view_file','run_command')),'',0,job(),'a.wav')[0]
        self.assertEqual(verdict['reason'],'tool_use')

    def test_quota_is_classified_before_capacity(self):
        err='AGY_ERROR: RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 3h25m54s.'
        verdict=tx.judge(stream(raw()),err,3,job(),'a.wav')[0]
        self.assertEqual(verdict['reason'],'quota')
        self.assertIn('Resets in',verdict['detail'])

    def test_quota_halts_dispatch_without_spending_budget(self):
        calls=[]
        jobs=[job('a'),job('b'),job('c')]
        states={j['id']:dict(status='queued',attempts=0) for j in jobs}
        def worker(j,n):
            calls.append((j['id'],n))
            return dict(validation=dict(ok=False,reason='quota',warnings=[]))
        halted=tx.schedule(jobs,states,worker,1,2,lambda:None,threading.Event(),backoff=0)
        self.assertEqual(halted,'quota')
        self.assertEqual(calls,[('a',1)])
        self.assertEqual(states['a']['status'],'quota_wait')
        self.assertEqual(states['a']['attempts'],0)
        self.assertEqual(states['b']['status'],'queued')

    def test_expired_login_is_classified_as_auth(self):
        err='Authentication required. Please visit the URL to log in:\nerror: authentication failed or timed out'
        verdict=tx.judge('',err,1,job(),'a.wav')[0]
        self.assertEqual(verdict['reason'],'auth')

    def test_auth_halts_dispatch_without_spending_budget(self):
        calls=[]
        jobs=[job('a'),job('b'),job('c')]
        states={j['id']:dict(status='queued',attempts=0) for j in jobs}
        def worker(j,n):
            calls.append((j['id'],n))
            return dict(validation=dict(ok=False,reason='auth',warnings=[]))
        halted=tx.schedule(jobs,states,worker,1,2,lambda:None,threading.Event(),backoff=0)
        self.assertEqual(halted,'auth')
        self.assertEqual(calls,[('a',1)])
        self.assertEqual(states['a']['status'],'auth_wait')
        self.assertEqual(states['a']['attempts'],0)

    def test_auth_gate_runs_one_process_until_sign_in(self):
        jobs=[job(x) for x in 'abcd']
        states={j['id']:dict(status='queued',attempts=0) for j in jobs}
        gate,lock,live,peak=threading.Event(),threading.Lock(),[0],[]
        def worker(j,n):
            with lock:
                live[0]+=1; peak.append((gate.is_set(),live[0]))
            time.sleep(.05)
            gate.set()   # the first process signed in
            time.sleep(.2)
            with lock:
                live[0]-=1
            return dict(validation=dict(ok=True,reason=None,warnings=[]))
        tx.schedule(jobs,states,worker,4,1,lambda:None,threading.Event(),backoff=0,auth_gate=gate)
        self.assertEqual(peak[0],(False,1))
        self.assertTrue(all(opened for opened,_ in peak[1:]))
        self.assertGreater(max(n for _,n in peak),1)

    def test_auth_passed_reads_agy_log(self):
        with tempfile.TemporaryDirectory() as d:
            log=Path(d)/'agy_cli.log'
            self.assertFalse(tx.auth_passed(log))
            log.write_text('I1031 printmode.go:407] Print mode: not authenticated, trying silent auth\n')
            self.assertFalse(tx.auth_passed(log))
            log.write_text(log.read_text()+'I1031 printmode.go:409] Print mode: silent auth succeeded\n')
            self.assertTrue(tx.auth_passed(log))

    def test_no_speech_claim_over_voiced_audio_is_rejected(self):
        body=raw('[00:55:00] Speaker 1: [inaudible]\n[00:55:06] Speaker 1: [silence]')
        self.assertEqual(tx.validate(body,'',0,job(start=3300,end=3360,voiced_seconds=60))['reason'],'hollow_voiced')
        quiet=tx.validate(raw('[00:00:00] [无人声]'),'',0,job(start=0,end=60,voiced_seconds=4))
        self.assertTrue(quiet['ok'])
        self.assertIn('no_speech',quiet['warnings'])

    def test_xml_tag_sections_are_read_as_headings(self):
        value='}<Chunk Transcript>\n[00:00:00] Speaker 1: 有没有这样一些例子？\n</Chunk Transcript>\n<Unclear Sections>\nNone\n</Unclear Sections>\n<Quality Notes>\n清晰\n</Quality Notes>\n'
        self.assertTrue(tx.validate(value,'',0,job())['ok'])

    def test_leaked_reasoning_block_is_skipped(self):
        value=' []:thought\nLet me restate the format: # Chunk Transcript ...\n'+raw('[00:00:00] Speaker 1: 有没有这样一些例子？')
        result=tx.validate(value,'',0,job())
        self.assertTrue(result['ok'])
        self.assertNotIn('restate',result['clean'])

    def test_quota_snapshot_reads_gemini_buckets(self):
        fake=Path(__file__).with_name('fake_agy.py')
        with mock.patch.dict(tx.os.environ,{'FAKE_AGY_WEEKLY':'0.05'}):
            snap=tx.quota_snapshot(sys.executable+'_missing')
            self.assertIsNone(snap)
            result=subprocess.run([sys.executable,str(fake),'--print','/usage','--output-format','json'],capture_output=True,text=True)
            with mock.patch.object(tx,'checked',return_value=result.stdout):
                snap=tx.quota_snapshot('agy')
        self.assertEqual(snap['weekly']['remaining'],.05)
        self.assertEqual(snap['5h']['remaining'],.8)

    def test_unreadable_audio_marker_fails(self):
        self.assertEqual(tx.validate(raw('[00:00:00] [无法读取音频]'),'',0,job())['reason'],'audio_unread')

    def test_voiced_seconds_from_loudness(self):
        levels=[-51.0]*50+[-22.0]*10+[-120.0]*5
        self.assertEqual(tx.voiced(0,60,levels),10)
        self.assertEqual(tx.voiced(55,65,levels),5)

    def test_near_silent_chunk_is_not_sent_to_agy(self):
        with tempfile.TemporaryDirectory() as d:
            j=job('r000_c0000',0,60,source='r000',path='/nonexistent.m4a',voiced_seconds=0)
            meta=tx.attempt(j,1,Path(d),dict(fingerprint='fp'),30,threading.Event())
            self.assertTrue(meta['local_silence'])
            self.assertIn('local_silence_not_sent',meta['validation']['warnings'])
            self.assertEqual(tx.restore([j],Path(d),'fp')[j['id']]['status'],'machine_checked')

    def test_canary_skips_silent_opening(self):
        jobs=[job('r000_c0000',0,60,source='r000',voiced_seconds=5),job('r000_c0001',50,110,source='r000',voiced_seconds=55)]
        self.assertEqual([j['id'] for j in tx.canary_jobs(jobs)],['r000_c0001'])
        jobs[1]['voiced_seconds']=0
        self.assertEqual([j['id'] for j in tx.canary_jobs(jobs)],['r000_c0000'])

    def assembled(self,texts):
        jobs=[dict(id=f'r000_c{i:04d}',source='r000',index=i,start=50*i,end=50*i+60) for i in range(len(texts))]
        states={}
        for j,text in zip(jobs,texts):
            states[j['id']]=dict(status='machine_checked',result=dict(validation=tx.validate(raw(text),'',0,j)))
        manifest=dict(sources=[dict(id='r000',sha256='x',duration=50*len(texts)+10)],jobs=jobs)
        with tempfile.TemporaryDirectory() as d:
            tx.assemble(manifest,Path(d),states,dict(model='m'))
            return (Path(d)/'r000_transcript.md').read_text(),json.loads((Path(d)/'review_queue.json').read_text())

    def test_overlap_mismatch_and_language_are_flagged_not_deleted(self):
        zh='我们先聊一下你在上一家公司的项目经历，你在里面具体负责哪些工作，遇到过什么困难，最后是怎么解决的，结果怎么样。'
        texts=[f'[00:00:00] Speaker 1: {zh}\n[00:00:52] Speaker 1: 这个项目里你负责数据清洗和底稿整理对吧，具体是怎么分工的',
               '[00:00:50] Speaker 1: so when I was about twenty I lived in Madrid Spain for like three months and one night we were waiting for the night bus downtown',
               f'[00:01:40] Speaker 1: {zh}', f'[00:02:30] Speaker 1: {zh}', f'[00:03:20] Speaker 1: {zh}']
        text,review=self.assembled(texts)
        reasons={r['reason'] for r in review}
        self.assertIn('overlap_mismatch',reasons)
        self.assertIn('language_mismatch',reasons)
        self.assertIn('Madrid',text)
        self.assertIn(tx.MISMATCH_NOTE,text)

    def test_matching_overlap_not_flagged(self):
        texts=['[00:00:00] Speaker 1: 开场。\n[00:00:52] Speaker 1: 这个项目里你负责数据清洗和底稿整理对吧，具体做了哪些',
               '[00:00:50] Speaker 1: 这个项目里你负责数据清洗和底稿整理对吧，具体做了哪些事情\n[00:01:30] Speaker 2: 主要是对账。']
        self.assertNotIn('overlap_mismatch',{r['reason'] for r in self.assembled(texts)[1]})

    def test_span_parser(self):
        self.assertEqual(tx.parse_span('18:20-20:10'),(1100,1210))
        self.assertEqual(tx.parse_span('3300-3360'),(3300,3360))

    def test_end_to_end_local_cli_fixture_and_resume(self):
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
            self.skipTest('Installed ffmpeg and ffprobe required for media protocol test')
        with tempfile.TemporaryDirectory(prefix='agy_offline_') as d:
            root=Path(d)
            audio=root/'录音 with spaces.wav'
            with wave.open(str(audio),'wb') as stream:
                stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
                stream.writeframes(b'\0\0'*48000)
            fake=root/'fake-agy'
            shutil.copyfile(Path(__file__).with_name('fake_agy.py'),fake)
            fake.chmod(0o700)
            with mock.patch.object(tx,'loudness',return_value=[-20.0]*10):
                manifest=tx.plan([str(audio)],root/'out',1,.2)
            args=Namespace(model=None,workers=2,max_attempts=2,timeout=30,canary_only=True)
            with mock.patch.object(tx.shutil,'which',return_value=str(fake)),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(tx.execute(root/'out',args),3)
                run=Path(json.loads((root/'out/latest_run.json').read_text())['path'])
                review=json.loads((run/'canary_review_required.json').read_text())
                for item in review['sources'].values():
                    item.update(passed=True,method='offline_protocol_fixture',evidence='Synthetic silent WAV with known fixture output; no speech-accuracy claim.')
                tx.save_json(run/'canary_review.json',review)
                args.canary_only=False
                self.assertEqual(tx.execute(root/'out',args),0)
                before={str(p):tx.digest(p) for p in (run/'chunks').glob('*/attempt_*/stdout.txt')}
                self.assertEqual(len(before),len(manifest['jobs']))
                self.assertEqual(tx.execute(root/'out',args),0)
                after={str(p):tx.digest(p) for p in (run/'chunks').glob('*/attempt_*/stdout.txt')}
                self.assertEqual(before,after)
                self.assertEqual(json.loads((run/'status.json').read_text())['counts'],{'machine_checked':len(manifest['jobs'])})

    def test_end_to_end_failed_chunk_recovered_and_delivered(self):
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
            self.skipTest('Installed ffmpeg and ffprobe required for media protocol test')
        with tempfile.TemporaryDirectory(prefix='agy_offline_') as d:
            root=Path(d)
            audio=root/'面试 录音.wav'
            with wave.open(str(audio),'wb') as stream:
                stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
                stream.writeframes(b'\0\0'*16000*6)
            fake=root/'fake-agy'
            shutil.copyfile(Path(__file__).with_name('fake_agy.py'),fake)
            fake.chmod(0o700)
            with mock.patch.object(tx,'loudness',return_value=[-20.0]*10):
                tx.plan([str(audio)],root/'out',2,.5)
            args=Namespace(model=None,workers=2,max_attempts=1,timeout=30,canary_only=False,deliver=root/'交付')
            with mock.patch.object(tx.shutil,'which',return_value=str(fake)),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(tx.execute(root/'out',Namespace(**{**vars(args),'canary_only':True})),3)
                run=Path(json.loads((root/'out/latest_run.json').read_text())['path'])
                review=json.loads((run/'canary_review_required.json').read_text())
                for item in review['sources'].values():
                    item.update(passed=True,method='offline_protocol_fixture',evidence='fixture')
                tx.save_json(run/'canary_review.json',review)
                with mock.patch.dict(tx.os.environ,{'FAKE_AGY_FAIL_START':'00:00:03'}):
                    self.assertEqual(tx.execute(root/'out',args),2)
                self.assertIn('[此段转写失败，待补齐]',(root/'交付/面试 录音_机器转写.md').read_text())
                rec=Namespace(**{**vars(args),'window':1,'overlap':.2,'span':None,'include_flagged':False})
                self.assertEqual(tx.recover(root/'out',rec),0)
            final=(root/'交付/面试 录音_机器转写.md').read_text()
            self.assertNotIn('待补齐',final)
            self.assertIn('（短分段补转）',final)
            self.assertTrue((root/'交付/面试 录音_待核对项.json').is_file())

if __name__=='__main__': unittest.main(verbosity=2)
