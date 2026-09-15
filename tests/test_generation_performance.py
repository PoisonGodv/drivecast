from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from drivecast import db
from drivecast.config import settings
from drivecast.concurrency import gather_cancel, request_limit
from drivecast.jobs import worker
from drivecast.models import Episode, Job, Plan, Topic
from drivecast.tts import quality, service, edge, doubao
from drivecast.web.api_audio import _quality
from test_long_episode import _plan_ep
from drivecast.schemas import ScriptSection
from drivecast.scriptwriter import service as scripts


class ConcurrentGenerationTest(unittest.IsolatedAsyncioTestCase):
    async def test_request_limits_shared_and_cancel_safe(self):
        with patch.object(settings, 'llm_max_concurrency', 2):
            gate = asyncio.Event()
            full = asyncio.Event()
            active = peak = 0

            async def call():
                nonlocal active, peak
                async with request_limit('llm'):
                    active += 1
                    peak = max(peak, active)
                    if active == 2:
                        full.set()
                    try:
                        await gate.wait()
                    finally:
                        active -= 1
            tasks = [asyncio.create_task(call()) for _ in range(8)]
            await asyncio.wait_for(full.wait(), 1)
            tasks[-1].cancel()
            gate.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.assertEqual(peak, 2)
            self.assertEqual(request_limit('llm')._value, 2)
            self.assertIs(edge._get_sem(), doubao._get_sem())
            self.assertEqual(edge._get_sem()._value, 1)

    async def test_provider_budget_covers_stream_and_wait_is_outside_timeout(self):
        from drivecast.llm.provider import OpenAICompatProvider
        entered = asyncio.Event()
        release = asyncio.Event()
        closed = []

        class Stream:
            def __aiter__(self):
                return self
            async def __anext__(self):
                await release.wait()
                raise StopAsyncIteration
            async def close(self):
                closed.append(True)

        async def create(**kwargs):
            entered.set()
            return Stream()

        def provider(timeout):
            p = object.__new__(OpenAICompatProvider)
            p.model, p.timeout, p.idle_timeout = 'fake', timeout, 2
            p._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=create))))
            return p

        with patch.object(settings, 'llm_max_concurrency', 1):
            first = provider(2)
            second = provider(0.01)
            t1 = asyncio.create_task(first._chat([], temperature=0, json_mode=False))
            await entered.wait()
            t2 = asyncio.create_task(second._chat([], temperature=0, json_mode=False))
            await asyncio.sleep(0.03)  # 超过第二个请求自身上限，但它只是在等预算
            self.assertFalse(t2.done())
            second._client.chat.completions.create.assert_not_called()
            release.set()
            await asyncio.gather(t1, t2)
            self.assertEqual(len(closed), 2)
            self.assertEqual(request_limit('llm')._value, 1)

    async def test_dag_unlocks_dependencies_without_waiting_for_other_sections(self):
        plan, ep = _plan_ep()
        entered = {k: asyncio.Event() for k in ('S0', 'S1', 'S2', 'S3', 'S4')}
        release = {k: asyncio.Event() for k in entered}

        async def section(plan, ep, key, *args, **kwargs):
            entered[key].set()
            await release[key].wait()
            return ScriptSection(key=key, title=key, content='正文'), [], 1
        with patch.object(scripts, '_gen_section_checked', side_effect=section), \
             patch.object(scripts, '_gen_section_split', side_effect=section):
            task = asyncio.create_task(scripts._gen_sections(plan, ep, 12000, []))
            await asyncio.wait_for(entered['S2'].wait(), 1)
            release['S2'].set()
            await asyncio.wait_for(entered['S3'].wait(), 1)
            self.assertFalse(entered['S4'].is_set())
            release['S1'].set()
            await asyncio.wait_for(entered['S4'].wait(), 1)
            self.assertFalse(task.done())
            for event in release.values():
                event.set()
            result = await task
            self.assertEqual([s.key for s in result.sections], ['S0', 'S1', 'S2', 'S3', 'S4'])

    async def test_failure_cleans_siblings(self):
        started = asyncio.Event()
        cleaned = asyncio.Event()
        async def slow():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        async def fail():
            await started.wait()
            raise ValueError('bad section')
        with self.assertRaises(ValueError):
            await gather_cancel(slow(), fail())
        self.assertTrue(cleaned.is_set())


class QualityPolicyTest(unittest.TestCase):
    def test_supported_minutes_and_requested_vs_estimated(self):
        for minutes in (10, 15, 20, 30, 40):
            result = quality.report(minutes * 60, minutes * 60, minutes, -1, {'passed': True})
            self.assertEqual(result['status'], 'passed')
        result = quality.report(1200, 1200, 40, -1, {'passed': True})
        self.assertTrue(result['checks']['estimated_duration_error']['passed'])
        self.assertFalse(result['checks']['requested_duration_error']['passed'])
        self.assertTrue(_quality({}, 2400)['checks']['duration'])

    def test_custom_range_and_unknown(self):
        with patch.object(settings, 'quality_max_minutes', 35):
            result = quality.report(2400, None, None, -1, {'passed': None})
            self.assertFalse(result['checks']['duration_range']['passed'])
            self.assertIn('8–35', quality.message('duration_range', result['checks']))
        self.assertIn('8–35', quality.message('duration_range', result['checks']))
        self.assertEqual(quality.report(2400, None, None, -1, {'passed': None})['status'], 'unknown')
        self.assertTrue(quality.stale({'quality': result}))


class AudioRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_recheck_preserves_files_and_extensions(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(settings, 'data_dir', Path(tmp)):
            audio = settings.episode_audio_path('ep')
            audio.parent.mkdir(parents=True)
            audio.write_bytes(b'unchanged audio')
            meta_path = settings.meta_path('ep')
            meta_path.parent.mkdir(parents=True)
            meta_path.write_text(json.dumps({'pipeline_version': 7, 'human_qc': {'ok': True},
                                             'chapters': [{'title': '旧章节'}],
                                             'duration_estimate': {'estimated_sec': 2400}}))
            with patch.object(service, '_probe_duration', new=AsyncMock(return_value=2400)), \
                 patch.object(service, '_peak_db', new=AsyncMock(return_value=-1)), \
                 patch('drivecast.persist.upsert_audio', new=AsyncMock()) as persist, \
                 patch.object(service, '_concat', new=AsyncMock()) as concat, \
                 patch.object(service, '_synth_missing', new=AsyncMock()) as synth:
                result = await service.recheck_quality({'episode_id': 'ep', 'target_minutes': 40})
            self.assertEqual(audio.read_bytes(), b'unchanged audio')
            self.assertEqual(result['human_qc'], {'ok': True})
            self.assertEqual(result['chapters'], [{'title': '旧章节'}])
            self.assertEqual(result['pipeline_version'], 7)
            self.assertIsNone(result['quality']['checks']['chunks']['passed'])
            self.assertTrue(result['quality']['checks']['duration_range']['passed'])
            persist.assert_awaited_once()
            synth.assert_not_awaited()
            concat.assert_not_awaited()

    async def test_real_audio_recheck_does_not_reencode(self):
        import hashlib
        import subprocess
        with tempfile.TemporaryDirectory() as tmp, patch.object(settings, 'data_dir', Path(tmp)):
            audio = settings.episode_audio_path('real')
            audio.parent.mkdir(parents=True)
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                            'sine=frequency=440:duration=0.3', '-y', str(audio)], check=True)
            meta_path = settings.meta_path('real')
            meta_path.parent.mkdir(parents=True)
            meta_path.write_text(json.dumps({'pipeline_version': 7, 'human_qc': {'passed': True}}))
            before = hashlib.sha256(audio.read_bytes()).hexdigest()
            with patch('drivecast.persist.upsert_audio', new=AsyncMock()), \
                 patch.object(service, '_concat', new=AsyncMock()) as concat, \
                 patch.object(edge, 'synth_chunk', new=AsyncMock()) as remote:
                result = await service.recheck_quality({'episode_id': 'real', 'target_minutes': 40})
            self.assertGreater(result['duration_sec'], 0)
            self.assertIsNotNone(result['quality']['checks']['peak']['max_volume_db'])
            self.assertEqual(hashlib.sha256(audio.read_bytes()).hexdigest(), before)
            concat.assert_not_called()
            remote.assert_not_called()

    async def test_only_bad_chunk_is_synthesized_and_local_upgrade_never_calls_provider(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(settings, 'data_dir', Path(tmp)), \
             patch.object(settings, 'tts_provider', 'edge'):
            chunks = [{'seq': i, 'text': '正文', 'voice': 'v', 'rate': '-5%'} for i in (1, 2)]
            directory = settings.chunks_dir('ep')
            directory.mkdir(parents=True)
            (directory / '001.mp3').write_bytes(b'good')
            (directory / '002.mp3').write_bytes(b'bad')
            async def probe(path):
                return 10 if path.read_bytes() == b'good' else 0
            async def synth(text, voice, rate, path):
                path.write_bytes(b'good')
            with patch.object(service, '_probe_duration', side_effect=probe), \
                 patch.object(edge, 'synth_chunk', side_effect=synth) as remote, \
                 patch.object(edge, 'jitter_sleep', new=AsyncMock()):
                with self.assertRaises(RuntimeError):
                    await service._synth_missing('ep', chunks, local_only=True)
                remote.assert_not_called()
                await service._synth_missing('ep', chunks)
                self.assertEqual(remote.await_count, 1)
                self.assertEqual(remote.call_args.args[-1].name, '002.mp3')


class WorkerLaneTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(settings, 'data_dir', Path(self.tmp.name))
        self.patch.start()
        await db.init_db()
        async with db.session() as s:
            topic = Topic(title='lane', direction='backend_arch')
            s.add(topic)
            await s.flush()
            for pid in ('a', 'b'):
                plan = Plan(ext_id=pid, topic_id=topic.id, title=pid, status='active', episode_count=2)
                s.add(plan)
                await s.flush()
                for seq in (1, 2):
                    s.add(Episode(ext_id=f'{pid}{seq}', plan_id=plan.id, seq=seq, title='ep'))
                    s.add(Job(kind='script', ref_id=f'{pid}{seq}', payload=json.dumps({'plan_id': pid, 'seq': seq})))
            await s.commit()

    async def asyncTearDown(self):
        await db.close_db()
        self.patch.stop()
        self.tmp.cleanup()
        worker._claimed_jobs.clear()
        worker._shutdown_jobs.clear()

    async def test_lanes_overlap_and_scripts_remain_ordered(self):
        from drivecast.jobs.pipeline import enqueue
        audio_started = asyncio.Event()
        second_script = asyncio.Event()
        firsts = set()
        both_plans = asyncio.Event()
        events = []
        active_audio = peak_audio = 0
        async def script(job):
            payload = json.loads(job.payload)
            pid, seq = payload['plan_id'], payload['seq']
            events.append(('script', pid, seq))
            if seq == 1:
                firsts.add(pid)
                if len(firsts) == 2:
                    both_plans.set()
                await asyncio.wait_for(both_plans.wait(), 2)
            else:
                second_script.set()
                await asyncio.wait_for(audio_started.wait(), 2)
            await enqueue('tts', job.ref_id, payload)
            await worker._finish(job.id, 'done')
        async def audio(job):
            nonlocal active_audio, peak_audio
            active_audio += 1
            peak_audio = max(peak_audio, active_audio)
            audio_started.set()
            await asyncio.wait_for(second_script.wait(), 2)
            active_audio -= 1
            await worker._finish(job.id, 'done')
        with patch.dict(worker._EXECUTORS, script=script, tts=audio), \
             patch.object(settings, 'worker_poll_interval_sec', 0.001):
            await asyncio.wait_for(worker._dispatch(forever=False), 5)
        self.assertEqual(peak_audio, 1)
        self.assertTrue(both_plans.is_set())
        for pid in ('a', 'b'):
            self.assertLess(events.index(('script', pid, 1)), events.index(('script', pid, 2)))
        async with db.session() as s:
            from sqlalchemy import select
            jobs = list((await s.scalars(select(Job))).all())
            self.assertEqual(len(jobs), 8)
            self.assertTrue(all(j.status == 'done' for j in jobs))

    async def test_shutdown_recovers_running_jobs_and_stale_excludes_active(self):
        started = asyncio.Event()
        async def blocked(job):
            started.set()
            await asyncio.Event().wait()
        with patch.dict(worker._EXECUTORS, script=blocked), \
             patch.object(settings, 'worker_stale_running_sec', -1), \
             patch.object(settings, 'worker_poll_interval_sec', 0.001):
            task = asyncio.create_task(worker._dispatch(forever=True))
            await asyncio.wait_for(started.wait(), 2)
            self.assertEqual(await worker._reset_stale_running(), 0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        async with db.session() as s:
            from sqlalchemy import select
            jobs = list((await s.scalars(select(Job))).all())
            self.assertTrue(all(j.status == 'pending' for j in jobs))
            self.assertTrue(all(j.attempt == 0 for j in jobs))
