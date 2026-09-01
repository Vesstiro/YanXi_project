"""Offline desktop regression tests. Run: python -m unittest tests.test_desktop -v"""
import json
import asyncio
import os
import tempfile
import threading
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.desktop.audio import AudioSpeaker, LocalTranscriber, MicrophoneRecorder, collect_audio
from src.desktop.service import Job, run_batch, speech_text, readable_result
from src.desktop.settings import DesktopSettings, SettingsStore, build_config, safe_error


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="yanxi_desktop_test_")
        self.addCleanup(self.temp.cleanup)
        self.store = SettingsStore(self.temp.name)
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_first_launch_needs_key(self):
        self.assertEqual(self.store.load().api_key, "")

    def test_saved_key_reload_and_not_in_json_or_repr(self):
        s = DesktopSettings(api_key="sk-unit-test-not-real")
        self.store.save(s)
        loaded = self.store.load()
        self.assertEqual(loaded.api_key, s.api_key)
        self.assertNotIn(s.api_key, self.store.path.read_text(encoding="utf-8"))
        self.assertNotIn(s.api_key, repr(s))
        self.assertNotIn("api_key", s.public_dict())

    def test_edit_saved_key_overrides_old_process_value(self):
        self.store.save(DesktopSettings(api_key="sk-old-test"))
        self.store.save(DesktopSettings(api_key="sk-new-test"))
        self.assertEqual(self.store.load().api_key, "sk-new-test")
        self.assertEqual(os.environ["QWEN_API_KEY"], "sk-new-test")

    def test_runtime_only_removes_selected_saved_key(self):
        self.store.save(DesktopSettings(api_key="sk-saved-test"))
        self.store.save(DesktopSettings(api_key="sk-session-test", remember_key=False))
        self.assertNotIn("QWEN_API_KEY", self.store.env_path.read_text(encoding="utf-8"))
        del os.environ["QWEN_API_KEY"]
        self.assertEqual(self.store.load().api_key, "")

    def test_other_provider_keys_preserved(self):
        self.store.save(DesktopSettings(api_key="sk-qwen-test"))
        self.store.save(DesktopSettings(provider="deepseek", model="deepseek-chat", api_key="sk-deepseek-test"))
        self.assertEqual(self.store.saved_key("qwen"), "sk-qwen-test")

    def test_corrupt_preferences_do_not_break_startup(self):
        self.store.path.parent.mkdir(parents=True)
        for data in ("broken-json", '{"provider": {}}', '{"model": 12}', '{"stt_model": "bad"}'):
            self.store.path.write_text(data, encoding="utf-8")
            self.assertEqual(self.store.load().provider, "qwen")

    def test_validation(self):
        s = DesktopSettings(api_key="sk-test")
        for values in ({"api_key": ""}, {"api_key": "key\nsecret"}, {"model": "bad model"},
                       {"provider": "unknown"}, {"stt_engine": "unknown"}):
            with self.assertRaises(ValueError):
                replace(s, **values).validate()

    def test_config_uses_selected_key_and_only_selected_backend(self):
        os.environ["QWEN_API_KEY"] = "sk-stale-test"
        config = build_config(DesktopSettings(api_key="sk-current-test"))
        self.assertEqual(config["llm"]["qwen"]["api_key"], "sk-current-test")
        self.assertEqual(config["llm"]["fallback_chain"], ["qwen"])
        self.assertEqual(config["llm"]["deepseek"]["api_key_env"], "")

    def test_redaction(self):
        self.assertNotIn("abc-secret", safe_error("bad abc-secret", "abc-secret"))
        self.assertNotIn("sk-test-token", safe_error("bad sk-test-token"))


class BatchTests(unittest.TestCase):
    def service(self, process):
        return SimpleNamespace(settings=DesktopSettings(api_key="sk-unit-secret"), process=process)

    def test_failure_isolated_and_exports_sanitized(self):
        jobs = [Job("one"), Job("two"), Job("three")]
        def process(job, progress, allowed):
            job.transcript = "测试文字"
            progress("分析中")
            if job.source == "two":
                raise ValueError("API failed sk-unit-secret")
            return {"agent_reply": "测试回复"}
        events = []
        with tempfile.TemporaryDirectory() as directory:
            run_batch(jobs, self.service(process), threading.Event(), lambda *e: events.append(e), output_dir=directory)
            self.assertEqual([j.status for j in jobs], ["完成", "失败", "完成"])
            self.assertEqual(len(list(Path(directory).glob("*.json"))), 3)
            saved = json.loads((Path(directory) / f"{jobs[1].id}.json").read_text(encoding="utf-8"))
            self.assertNotIn("sk-unit-secret", saved["error"])
            self.assertEqual(saved["transcript"], "测试文字")
        self.assertEqual(events[-1][0], "batch_done")

    def test_stop_finishes_current_and_preserves_pending(self):
        stop, jobs = threading.Event(), [Job("a"), Job("b")]
        def process(*args):
            stop.set()
            return {"ok": True}
        run_batch(jobs, self.service(process), stop, lambda *args: None)
        self.assertEqual([j.status for j in jobs], ["完成", "待处理"])

    def test_precancelled_queue_does_not_run(self):
        stop = threading.Event()
        stop.set()
        jobs = [Job("a")]
        run_batch(jobs, self.service(lambda *a: self.fail("must not run")), stop, lambda *a: None)
        self.assertEqual(jobs[0].status, "待处理")

    def test_llm_fallback_is_explicitly_marked(self):
        job = Job("fallback")
        run_batch([job], self.service(lambda *a: {"desktop_warning": "网络失败，规则降级"}),
                  threading.Event(), lambda *a: None)
        self.assertEqual(job.status, "降级完成")

    def test_rerun_failed_job_clears_old_error(self):
        job = Job("a", error="old error", status="失败")
        run_batch([job], self.service(lambda *a: {"ok": True}), threading.Event(), lambda *a: None)
        self.assertEqual(job.error, "")
        self.assertEqual(job.status, "完成")

    def test_speech_fallback_when_scam_has_no_reply(self):
        result = {"agent_reply": "", "final_action": "reject", "call_type_name": "诈骗", "notification_card": {"title": "风险预警", "body": "请勿转账"}}
        text = speech_text(result)
        self.assertIn("请勿转账", text)
        self.assertIn("诈骗", readable_result(result))

    def test_speech_reply_and_full_mode(self):
        result = {"agent_reply": "您好", "call_type_name": "外卖"}
        self.assertEqual(speech_text(result), "您好")
        self.assertIn("外卖", speech_text(result, full=True))


class AudioTests(unittest.TestCase):
    def test_collect_audio_files_folder_filter_and_dedup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "A.MP3").touch()
            (path / "b.wav").touch()
            (path / "notes.txt").touch()
            (path / "nested").mkdir()
            (path / "nested" / "hidden.mp3").touch()
            found = collect_audio([path, path / "A.MP3"])
            self.assertEqual(len(found), 2)
            self.assertTrue(all(Path(p).suffix.lower() in (".mp3", ".wav") for p in found))

    def test_microphone_capture_with_fake_stream(self):
        stop = threading.Event()
        class Stream:
            def __enter__(self):
                self.count = 0
                return self
            def __exit__(self, *args):
                return False
            def read(self, n):
                self.count += 1
                if self.count == 6:
                    stop.set()
                return b"\x01\x00" * n, False
        fake = SimpleNamespace(query_devices=lambda *a: {"default_samplerate": 16000},
            check_input_settings=lambda **k: None, RawInputStream=lambda **k: Stream(), PortAudioError=RuntimeError)
        with tempfile.TemporaryDirectory() as directory, patch("src.desktop.audio.ROOT", Path(directory)), patch.dict("sys.modules", {"sounddevice": fake}):
            output = MicrophoneRecorder().record(stop, lambda *a: None)
            with wave.open(output) as wav:
                self.assertEqual(wav.getframerate(), 16000)
                self.assertEqual(wav.getnchannels(), 1)
                self.assertEqual(wav.getnframes(), 6 * 1024)

    def test_faster_transcriber_combines_segments_and_loads_once(self):
        transcriber = LocalTranscriber("tiny", "faster-whisper")
        transcriber.model = SimpleNamespace(transcribe=lambda *a, **k: (
            iter([SimpleNamespace(text=" 你好 ", end=1), SimpleNamespace(text="外卖到了", end=2)]),
            SimpleNamespace(duration=2)))
        self.assertEqual(transcriber.transcribe("dummy", lambda *a: None), "你好外卖到了")

    def test_silence_is_failure_not_success(self):
        transcriber = LocalTranscriber("tiny", "faster-whisper")
        transcriber.model = SimpleNamespace(transcribe=lambda *a, **k: (iter([]), SimpleNamespace(duration=2)))
        with self.assertRaises(ValueError):
            transcriber.transcribe("dummy", lambda *a: None)

    def test_stop_during_synthesis_releases_temporary_directory(self):
        stop = threading.Event()
        stop.set()
        async def save(path):
            await asyncio.sleep(5)
        fake_edge = SimpleNamespace(Communicate=lambda *a: SimpleNamespace(save=save))
        fake_pygame = SimpleNamespace(mixer=SimpleNamespace(get_init=lambda: False))
        with patch.dict("sys.modules", {"edge_tts": fake_edge, "pygame": fake_pygame}):
            AudioSpeaker().speak("测试", "voice", stop)

    def test_stop_during_playback_unloads_audio_before_cleanup(self):
        stop, calls, paths = threading.Event(), [], []
        async def save(path):
            Path(path).write_bytes(b"test-audio")
            paths.append(path)
        fake_edge = SimpleNamespace(Communicate=lambda *a: SimpleNamespace(save=save))
        music = SimpleNamespace(load=lambda path: calls.append("load"), play=stop.set, get_busy=lambda: True,
            stop=lambda: calls.append("stop"), unload=lambda: calls.append("unload"))
        mixer = SimpleNamespace(init=lambda: calls.append("init"), get_init=lambda: "init" in calls,
            music=music, quit=lambda: calls.append("quit"))
        with patch.dict("sys.modules", {"edge_tts": fake_edge, "pygame": SimpleNamespace(mixer=mixer)}):
            AudioSpeaker().speak("测试", "voice", stop)
        self.assertEqual(calls, ["init", "load", "stop", "unload", "quit"])
        self.assertFalse(Path(paths[0]).exists())


class LLMErrorTests(unittest.TestCase):
    def test_tls_error_report_is_safe(self):
        from src.core.llm_client import LLMClient
        from src.core.enums import LLMBackend
        with patch.object(LLMClient, "_init_clients"):
            client = LLMClient({"llm": {"backend": "qwen", "fallback_chain": ["qwen"]}})
        client._clients = {LLMBackend.QWEN: {}}
        problem = RuntimeError("SSL handshake failed")
        with patch.object(client, "_call_backend", side_effect=problem), patch("src.core.llm_client.logger"):
            self.assertEqual(client.chat([]), "")
        self.assertIn("TLS", client.last_error)

    def test_successful_fallback_clears_previous_error(self):
        from src.core.llm_client import LLMClient
        from src.core.enums import LLMBackend
        with patch.object(LLMClient, "_init_clients"):
            client = LLMClient({"llm": {"backend": "qwen", "fallback_chain": ["qwen", "deepseek"]}})
        client._clients = {LLMBackend.QWEN: {}, LLMBackend.DEEPSEEK: {}}
        with patch.object(client, "_call_backend", side_effect=[RuntimeError("failed"), "OK"]), patch("src.core.llm_client.logger"):
            self.assertEqual(client.chat([]), "OK")
        self.assertIsNone(client.last_error)


if __name__ == "__main__":
    unittest.main()
