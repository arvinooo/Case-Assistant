import asyncio, json, subprocess, time
import numpy as np, wave, websockets, urllib.request

MP4 = '<你的mp4路径>'
WAV = '/tmp/flush16k.wav'
subprocess.run(['ffmpeg','-v','error','-y','-i',MP4,'-vn','-ac','1','-ar','16000','-c:a','pcm_s16le',WAV], check=True)

def count():
    with urllib.request.urlopen('http://127.0.0.1:8787/api/transcript') as r:
        return len(json.load(r)['segments'])

w = wave.open(WAV, 'rb'); sr = w.getframerate()
START, LEN = 399.0, 2.5      # 连续说话，结尾没有静音
w.setpos(int(START * sr))
pcm = np.frombuffer(w.readframes(int(LEN * sr)), dtype=np.int16)
w.close()

before = count()
print('断开前已有段数:', before)

async def main():
    async with websockets.connect('ws://127.0.0.1:8787/asr?samplerate=16000', max_size=None) as ws:
        step = int(sr * 0.1)
        for off in range(0, len(pcm), step):
            await ws.send(pcm[off:off+step].tobytes())
            await asyncio.sleep(0.05)
        print('  喂了 %.1f 秒连续语音（无尾部静音），直接断开' % LEN)

asyncio.run(main())
time.sleep(2.0)
after = count()
print('断开后段数:', after, '→ 新增', after - before)
with urllib.request.urlopen('http://127.0.0.1:8787/api/transcript') as r:
    segs = json.load(r)['segments']
if segs:
    print('最后一段:', segs[-1]['text'], '| %.2fs~%.2fs' % (segs[-1]['start'], segs[-1]['end']))
print('结论:', 'flush 生效，最后一句没丢' if after > before else '❌ 最后一句丢了')