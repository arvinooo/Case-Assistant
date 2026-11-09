from typing import *
import logging
import time
import sherpa_onnx
import os
import asyncio
import numpy as np

logger = logging.getLogger(__name__)
_asr_engines = {}


class ASRResult:
    def __init__(self, text: str, finished: bool, idx: int, start: float = 0.0, end: float = 0.0,
                 channel: Optional[int] = None):
        self.text = text
        self.finished = finished
        self.idx = idx
        self.start = start
        self.end = end
        self.channel = channel

    def to_dict(self):
        return {
            "text": self.text,
            "finished": self.finished,
            "idx": self.idx,
            "start": self.start,
            "end": self.end,
            "channel": self.channel,
        }


class ASRStream:
    """一条连接对应一个 stream。

    相对上游的改造点：
    1. VAD 每条流各一份，不再共用全局单例（两条连接会互相 reset）。
    2. close() 会 flush VAD，把最后半句吐出来——不丢面谈最后一句话。
    3. 离线模式时间戳用 segment.start（采样点）+ 采样数自己算。
    """

    def __init__(self, recognizer, sample_rate: int, vad=None) -> None:
        self.recognizer = recognizer
        self.inbuf: asyncio.Queue = asyncio.Queue()
        self.outbuf: asyncio.Queue = asyncio.Queue()
        self.sample_rate = sample_rate
        self.is_closed = False
        self.online = isinstance(recognizer, sherpa_onnx.OnlineRecognizer)
        self.vad = vad
        self._segment_id = 0
        self.base_offset = 0.0     # 导入录音时，片段相对整个文件的时间偏移

    async def start(self):
        if self.online:
            asyncio.create_task(self.run_online())
        else:
            asyncio.create_task(self.run_offline())

    async def run_online(self):
        stream = self.recognizer.create_stream()
        last_result = ""
        segment_id = 0
        logger.info("asr: start real-time recognize")
        while True:
            samples = await self.inbuf.get()
            if samples is None:
                break
            stream.accept_waveform(self.sample_rate, samples)
            while self.recognizer.is_ready(stream):
                self.recognizer.decode_stream(stream)

            is_endpoint = self.recognizer.is_endpoint(stream)
            result = self.recognizer.get_result(stream)

            if result and (last_result != result):
                last_result = result
                self.outbuf.put_nowait(ASRResult(result, False, segment_id))

            if is_endpoint:
                if result:
                    self.outbuf.put_nowait(ASRResult(result, True, segment_id))
                    segment_id += 1
                self.recognizer.reset(stream)
                last_result = ""

    def _offline_decode(self, segment) -> Optional[ASRResult]:
        seg_samples = np.asarray(segment.samples, dtype=np.float32)
        if seg_samples.size == 0:
            return None
        start_samples = int(getattr(segment, "start", 0))
        start = self.base_offset + start_samples / self.sample_rate
        end = start + seg_samples.size / self.sample_rate
        stream = self.recognizer.create_stream()
        stream.accept_waveform(self.sample_rate, seg_samples)
        self.recognizer.decode_stream(stream)
        text = stream.result.text.strip()
        if not text:
            return None
        idx = self._segment_id
        self._segment_id += 1
        return ASRResult(text, True, idx, round(start, 3), round(end, 3))

    async def run_offline(self):
        vad = self.vad
        if vad is None:
            logger.error("asr: offline mode without vad")
            return
        vad.reset()
        logger.info("asr: start vad+offline recognize")
        while True:
            samples = await self.inbuf.get()
            if samples is None:
                break
            vad.accept_waveform(samples)
            while not vad.empty():
                segment = vad.front
                vad.pop()
                if self.is_closed:
                    continue
                result = self._offline_decode(segment)
                if result:
                    self.outbuf.put_nowait(result)

    async def close(self):
        """关键改造：先把 VAD 里的尾巴吐出来，再收工。"""
        if self.is_closed:
            return
        self.is_closed = True
        if (not self.online) and self.vad is not None:
            try:
                self.vad.flush()
                while not self.vad.empty():
                    segment = self.vad.front
                    self.vad.pop()
                    result = self._offline_decode(segment)
                    if result:
                        self.outbuf.put_nowait(result)
            except Exception as exc:                       # noqa: BLE001
                logger.warning("asr: flush vad failed: %s", exc)
        self.inbuf.put_nowait(None)
        self.outbuf.put_nowait(None)

    async def write(self, pcm_bytes: bytes):
        if self.is_closed:
            return
        pcm_data = np.frombuffer(pcm_bytes, dtype=np.int16)
        samples = pcm_data.astype(np.float32) / 32768.0
        self.inbuf.put_nowait(samples)

    async def read(self) -> ASRResult:
        return await self.outbuf.get()


# ================= 模型加载 =================

def create_zipformer(samplerate: int, args) -> sherpa_onnx.OnlineRecognizer:
    d = os.path.join(args.models_root, "sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20")
    if not os.path.exists(d):
        raise ValueError("asr: model not found " + d)
    suffix = ".int8.onnx" if getattr(args, "use_int8", False) else ".onnx"
    return sherpa_onnx.OnlineRecognizer.from_transducer(
        tokens=os.path.join(d, "tokens.txt"),
        encoder=os.path.join(d, "encoder-epoch-99-avg-1" + suffix),
        decoder=os.path.join(d, "decoder-epoch-99-avg-1" + suffix),
        joiner=os.path.join(d, "joiner-epoch-99-avg-1" + suffix),
        provider=args.asr_provider,
        num_threads=args.threads,
        sample_rate=samplerate,
        feature_dim=80,
        enable_endpoint_detection=True,
        rule1_min_trailing_silence=2.4,
        rule2_min_trailing_silence=0.8,     # 上游 1.2，改快：整句早 0.4 秒出
        rule3_min_utterance_length=20,
    )


def create_sensevoice(samplerate: int, use_int8: bool, args) -> sherpa_onnx.OfflineRecognizer:
    d = os.path.join(args.models_root, "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17")
    if not os.path.exists(d):
        raise ValueError("asr: model not found " + d)
    model_path = os.path.join(d, "model.onnx")
    if use_int8 and os.path.exists(os.path.join(d, "model.int8.onnx")):
        model_path = os.path.join(d, "model.int8.onnx")
    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=model_path,
        tokens=os.path.join(d, "tokens.txt"),
        num_threads=args.threads,
        sample_rate=samplerate,
        use_itn=True,
        debug=0,
        language=args.asr_lang,
    )


def create_paraformer_trilingual(samplerate: int, args):
    d = os.path.join(args.models_root, "sherpa-onnx-paraformer-trilingual-zh-cantonese-en")
    if not os.path.exists(d):
        raise ValueError("asr: model not found " + d)
    return sherpa_onnx.OfflineRecognizer.from_paraformer(
        paraformer=os.path.join(d, "model.onnx"),
        tokens=os.path.join(d, "tokens.txt"),
        num_threads=args.threads,
        sample_rate=samplerate,
        debug=0,
        provider=args.asr_provider,
    )


def create_paraformer_en(samplerate: int, args):
    d = os.path.join(args.models_root, "sherpa-onnx-paraformer-en")
    if not os.path.exists(d):
        raise ValueError("asr: model not found " + d)
    return sherpa_onnx.OfflineRecognizer.from_paraformer(
        paraformer=os.path.join(d, "model.onnx"),
        tokens=os.path.join(d, "tokens.txt"),
        num_threads=args.threads,
        sample_rate=samplerate,
        use_itn=True,
        debug=0,
        provider=args.asr_provider,
    )


def create_fireredasr(samplerate: int, args):
    d = os.path.join(args.models_root, "sherpa-onnx-fire-red-asr-large-zh_en-2025-02-16")
    if not os.path.exists(d):
        raise ValueError("asr: model not found " + d)
    return sherpa_onnx.OfflineRecognizer.from_fire_red_asr(
        encoder=os.path.join(d, "encoder.int8.onnx"),
        decoder=os.path.join(d, "decoder.int8.onnx"),
        tokens=os.path.join(d, "tokens.txt"),
        debug=0,
        provider=args.asr_provider,
    )


def load_vad_engine(samplerate: int, args, min_silence_duration: float = 0.25,
                    buffer_size_in_seconds: int = 300) -> sherpa_onnx.VoiceActivityDetector:
    config = sherpa_onnx.VadModelConfig()
    d = os.path.join(args.models_root, "silero_vad")
    if not os.path.exists(d):
        raise ValueError("vad: model not found " + d)
    config.silero_vad.model = os.path.join(d, "silero_vad.onnx")
    config.silero_vad.min_silence_duration = min_silence_duration
    config.sample_rate = samplerate
    config.provider = args.asr_provider
    config.num_threads = args.threads
    return sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=buffer_size_in_seconds)


def load_asr_engine(samplerate: int, args):
    cache_engine = _asr_engines.get(args.asr_model)
    if cache_engine:
        return cache_engine
    st = time.time()
    name = args.asr_model
    if name == "zipformer-bilingual":
        cache_engine = create_zipformer(samplerate, args)
    elif name == "sensevoice":
        cache_engine = create_sensevoice(samplerate, False, args)
    elif name == "sensevoice-int8":
        cache_engine = create_sensevoice(samplerate, True, args)
    elif name == "paraformer-trilingual":
        cache_engine = create_paraformer_trilingual(samplerate, args)
    elif name == "paraformer-en":
        cache_engine = create_paraformer_en(samplerate, args)
    elif name == "fireredasr":
        cache_engine = create_fireredasr(samplerate, args)
    else:
        raise ValueError("asr: unknown model " + name)
    _asr_engines[name] = cache_engine
    logger.info("asr: engine loaded in %.2fs (%s)", time.time() - st, name)
    return cache_engine


async def start_asr_stream(samplerate: int, args) -> ASRStream:
    recognizer = load_asr_engine(samplerate, args)
    vad = None
    if not isinstance(recognizer, sherpa_onnx.OnlineRecognizer):
        vad = load_vad_engine(samplerate, args)      # 每条流一份
    stream = ASRStream(recognizer, samplerate, vad=vad)
    await stream.start()
    return stream


async def process_asr_file(samples: np.ndarray, samplerate: int, args,
                           channel: Optional[int] = None) -> List[ASRResult]:
    """离线批量转写：整文件喂 VAD，逐段识别。"""
    recognizer = load_asr_engine(samplerate, args)
    vad = load_vad_engine(samplerate, args, buffer_size_in_seconds=600)
    samples = np.asarray(samples, dtype=np.float32)
    results: List[ASRResult] = []
    segment_id = 0
    chunk_size = max(1, int(samplerate * 0.02))

    def consume():
        nonlocal segment_id
        while not vad.empty():
            segment = vad.front
            seg_samples = np.asarray(segment.samples, dtype=np.float32)
            start_samples = int(getattr(segment, "start", 0))
            vad.pop()
            if seg_samples.size == 0:
                continue
            stream = recognizer.create_stream()
            stream.accept_waveform(samplerate, seg_samples)
            recognizer.decode_stream(stream)
            text = stream.result.text.strip()
            if text:
                start = start_samples / samplerate
                end = start + seg_samples.size / samplerate
                results.append(ASRResult(text, True, segment_id, round(start, 3), round(end, 3), channel))
                segment_id += 1

    for offset in range(0, len(samples), chunk_size):
        vad.accept_waveform(samples[offset:offset + chunk_size])
        consume()
    vad.flush()
    consume()

    if not results and samples.size:
        stream = recognizer.create_stream()
        stream.accept_waveform(samplerate, samples)
        recognizer.decode_stream(stream)
        text = stream.result.text.strip()
        if text:
            results.append(ASRResult(text, True, 0, 0.0, round(len(samples) / samplerate, 3), channel))
    return results



def process_asr_file_streaming(samples, samplerate, args, on_segment=None,
                               on_progress=None, step_seconds: float = 0.05,
                               progress_every: float = 0.5, should_stop=None) -> int:
    """整文件连续喂 VAD，逐句回调——不切块、不吞停顿。

    三个要点：
    1. 不按 60 秒切块：切块会把正在说的话切断，界面也只能等整块转完才动；
    2. 喂料步长必须细（默认 50ms）。步长太粗（比如 0.5 秒）时，VAD 会把块里的
       短停顿当成说话，几句话被合并成一句（实测段长从 2.7 秒变 7 秒）；
    3. 进度回调单独节流到 0.5 秒一次，不受喂料步长影响。
    """
    recognizer = load_asr_engine(samplerate, args)
    vad = load_vad_engine(samplerate, args, buffer_size_in_seconds=600)
    vad.reset()
    samples = np.asarray(samples, dtype=np.float32)
    step = max(1, int(samplerate * step_seconds))
    every = max(1, int(progress_every / step_seconds))
    segment_id = 0
    count = 0

    def drain():
        nonlocal segment_id, count
        while not vad.empty():
            segment = vad.front
            seg_samples = np.asarray(segment.samples, dtype=np.float32)
            start_samples = int(getattr(segment, "start", 0))
            vad.pop()
            if seg_samples.size == 0:
                continue
            stream = recognizer.create_stream()
            stream.accept_waveform(samplerate, seg_samples)
            recognizer.decode_stream(stream)
            text = stream.result.text.strip()
            if not text:
                continue
            start = start_samples / samplerate
            end = start + seg_samples.size / samplerate
            result = ASRResult(text, True, segment_id, round(start, 3), round(end, 3))
            segment_id += 1
            count += 1
            if on_segment:
                on_segment(result)

    pcm_buffer = []
    ticks = 0
    for offset in range(0, len(samples), step):
        if should_stop and should_stop():
            break
        piece = samples[offset:offset + step]
        vad.accept_waveform(piece)
        drain()
        if on_progress:
            pcm_buffer.append((piece * 32767).astype(np.int16).tobytes())
            ticks += 1
            if ticks >= every:
                on_progress(round((offset + len(piece)) / samplerate, 2), b"".join(pcm_buffer))
                pcm_buffer = []
                ticks = 0

    if not (should_stop and should_stop()):
        vad.flush()
        drain()
    if on_progress and pcm_buffer:
        on_progress(round(len(samples) / samplerate, 2), b"".join(pcm_buffer))
    return count
