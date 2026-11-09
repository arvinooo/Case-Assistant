import asyncio, json, subprocess, time
import numpy as np, wave, websockets

MP4 = '<你的mp4路径>'
WAV = '/tmp/ws_test16k.wav'
subprocess.run(['ffmpeg','-v','error','-y','-i',MP4,'-vn','-ac','1','-ar','16000','-c:a','pcm_s16le',WAV], check=True)
w = wave.open(WAV, 'rb'); sr = w.getframerate()
START, LEN = 335.0, 18.0
w.setpos(int(START * sr))
pcm = np.frombuffer(w.readframes(int(LEN * sr)), dtype=np.int16)
w.close()
got = []

async def main():
    t0 = time.time()
    async with websockets.connect('ws://127.0.0.1:8787/asr?samplerate=16000', max_size=None) as ws:
        async def receiver():
            try:
                while True:
                    d = json.loads(await ws.recv())
                    if d.get('type') == 'segment':
                        got.append(d['segment'])
                        print('  [%.2fs] %s  %s' % (time.time()-t0, d['segment']['start'], d['segment']['text']))
            except Exception:
                pass
        rt = asyncio.create_task(receiver())
        step = int(sr * 0.1)
        for off in range(0, len(pcm), step):
            await ws.send(pcm[off:off+step].tobytes())
            target = (off + step) / sr
            wait = target - (time.time() - t0)
            if wait > 0:
                await asyncio.sleep(wait)
        print('  --- 音频发完，立刻断开（最后一句没说完）---')
        rt.cancel()

asyncio.run(main())
print('')
print('收到段数:', len(got))
if got:
    print('最后一段:', got[-1]['text'])
    print('时间范围: %.1f ~ %.1f 秒' % (got[0]['start'], got[-1]['end']))
else:
    print('没有任何段')