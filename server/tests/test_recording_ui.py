"""Native browser regression: video and WAV must never play over each other."""

import base64
import json
import re
import shutil
import subprocess
import wave
from pathlib import Path

import pytest


def test_recording_players_are_exclusive_and_detached_players_stop(tmp_path):
    chrome = next((str(p) for p in (
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path(shutil.which("chromium") or "/nonexistent"),
        Path(shutil.which("google-chrome") or "/nonexistent"),
    ) if p.is_file()), None)
    if not chrome or not shutil.which("ffmpeg"):
        pytest.skip("Chrome/Chromium and ffmpeg required for native media playback test")
    video, audio = tmp_path / "sample.mp4", tmp_path / "sample.wav"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=blue:s=160x288:r=10:d=5", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                    "-t", "5", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(video)],
                   check=True, capture_output=True, timeout=15)
    with wave.open(str(audio), "wb") as wav:
        wav.setparams((2, 2, 44100, 0, "NONE", "not compressed"))
        wav.writeframes(b"\0" * 44100 * 4 * 5)
    script = (Path(__file__).parents[1] / "hidagent/static/recording.js").read_text()
    html = """<!doctype html><meta charset="utf-8">
<div id="recording-panel"></div><input id="task"><button id="record-start"></button>
<button id="record-stop"></button><div id="record-state"></div><div id="record-audio"></div>
<div id="record-results"></div><pre id="test-result">PENDING</pre>
<script>
let records = [1,2].map(id=>({id:String(id),started_at:1,errors:[],files:[{
  duration_seconds:5,has_audio:true,playback:'sample.mp4',audio:'sample.wav'
}]}));
window.fetch=async path=>({json:async()=>path==='/api/status'
  ? {screen:true,agent:{running:false},recording:{version:2,current:null}}
  : records});
window.post=async()=>({});
</script><script>""" + script + "</script><script>\n" + f"""
const videoSource={json.dumps('data:video/mp4;base64,' + base64.b64encode(video.read_bytes()).decode())};
const audioSource={json.dumps('data:audio/wav;base64,' + base64.b64encode(audio.read_bytes()).decode())};
""" + """
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
function assert(value,message) { if(!value) throw Error(message); }
async function play(player) {
  // Exercise native play/pause events, including pending playback. Virtual time
  // can advance ahead of decoder startup; decoding is not the behavior under test.
  player.play().catch(()=>{});
  await delay(50); assert(!player.paused,'requested player paused');
}
function prepare() {
  const players=[...document.querySelectorAll('#record-results video,#record-results audio')];
  players.forEach(p=>{p.src=p.tagName==='VIDEO'?videoSource:audioSource;p.loop=true;});
  return players;
}
(async()=>{
  try {
    while(document.querySelectorAll('#record-results video').length!==2) await delay(10);
    const [video,audio,otherVideo]=prepare();
    await play(video);
    await play(audio);
    assert(video.paused,'video overlaps WAV from the same recording');
    await play(otherVideo);
    assert(audio.paused,'WAV overlaps another recording');
    // A new result replaces the DOM; the detached player must not keep producing sound.
    records=records.map(r=>({...r,task:'updated'}));
    await delay(1300);
    assert(!otherVideo.isConnected,'results did not refresh');
    assert(otherVideo.paused,'detached video keeps playing');
    const fresh=prepare();
    await play(fresh[1]);
    await document.getElementById('record-start').onclick();
    assert(fresh[1].paused,'starting capture left old audio playing');
    document.getElementById('test-result').textContent='PASS';
  } catch(error) { document.getElementById('test-result').textContent='FAIL: '+error.message; }
})();
</script>"""
    page = tmp_path / "test.html"
    page.write_text(html)
    result = subprocess.run([
        chrome, "--headless", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
        "--disable-background-networking", "--disable-extensions", "--mute-audio",
        "--autoplay-policy=no-user-gesture-required", f"--user-data-dir={tmp_path / 'chrome-profile'}",
        "--virtual-time-budget=12000", "--dump-dom", page.as_uri(),
    ], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr[-2000:]
    outcome = re.search(r'<pre id="test-result">(.*?)</pre>', result.stdout)
    assert outcome and outcome.group(1) == "PASS", outcome.group(1) if outcome else result.stderr[-1000:]
