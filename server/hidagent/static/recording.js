// Uses the main relay's existing AirPlay feed. No second receiver or port.
(() => {
  const node = id => document.getElementById(id);
  let version = '', polling = false;
  const make = (tag, text) => { const e = document.createElement(tag); if(text) e.textContent=text; return e; };
  const url = (id, name) => `/api/recordings/${encodeURIComponent(id)}/${encodeURIComponent(name)}`;
  const pausePlayers = except => {
    node('record-results').querySelectorAll('audio, video').forEach(player => {
      if(player!==except) player.pause();
    });
  };
  // `play` does not bubble. Capture it so dynamically rendered results are covered too.
  node('record-results').addEventListener('play', event => {
    if(event.target.matches('audio, video')) pausePlayers(event.target);
  }, true);
  async function action(path) {
    if(path==='/api/recording/start') pausePlayers();
    node('record-start').disabled = node('record-stop').disabled = true;
    await post(path, {text:node('task').value});
    await refresh();
  }
  node('record-start').onclick = () => action('/api/recording/start');
  node('record-stop').onclick = () => action('/api/recording/stop');
  async function refresh() {
    if(polling) return; polling=true;
    try {
      const s=await (await fetch('/api/status')).json();
      node('recording-panel').hidden=!s.recording;
      if(!s.recording) return;
      if(s.recording.version!==2) {
        node('record-start').disabled=true;
        node('record-stop').disabled=!s.recording.current || s.recording.current.state!=='recording';
        node('record-state').textContent='请重启服务以启用新的音频采集';
        node('record-audio').textContent='旧录制仍可停止并保存；新采集需要重启后重新投屏。';
        return;
      }
      const c=s.recording.current;
      const active=c && ['recording','finalizing'].includes(c.state);
      node('record-start').disabled=!!active || !s.screen || s.agent.running;
      node('record-stop').disabled=!c || c.state!=='recording';
      const labels={recording:'正在采集',finalizing:'正在保存',ready:'已保存，待回放核验',interrupted:'录制中断，已保留部分',failed:'采集失败'};
      node('record-state').textContent=c ? `${labels[c.state]||c.state}${c.state==='recording'?' · '+Math.floor(c.elapsed_seconds)+' 秒':''}` : '等待采集';
      node('record-audio').textContent=c?.state==='recording'
        ? `已收到音频 ${c.audio_received_seconds} 秒 · ${s.recording.audio_recent?'音频流正在输入':'当前未收到新音频'}`
        : '已启用时间戳同步 · WAV 保留接收端音质。音频识别尚未接入。';
      const records=await (await fetch('/api/recordings')).json();
      const next=JSON.stringify(records);
      if(next===version) return;
      version=next;
      pausePlayers(); // Stop old media before detaching it; avoid orphaned background playback.
      node('record-results').replaceChildren();
      for(const r of records) {
        const box=make('div'); box.style.cssText='border-top:1px solid #ddd;padding:12px 0;margin-top:12px';
        box.append(make('b',new Date(r.started_at*1000).toLocaleString()));
        if(r.task) box.append(make('p',r.task));
        for(const error of r.errors) box.append(make('p',error));
        for(const f of r.files) {
          box.append(make('p',`${Number(f.duration_seconds).toFixed(1)} 秒 · ${f.has_audio?'有音轨':'未收到音轨'}`));
          if(r.audio_format) box.append(make('p',`${r.audio_format.sample_rate} Hz · ${r.audio_format.channels} 声道 · 无损音轨`));
          if(f.playback) {
            box.append(make('p','视频回放（含声音）'));
            const player=make('video'); player.controls=true; player.preload='metadata'; player.src=url(r.id,f.playback);
            player.style.cssText='display:block;max-width:100%;max-height:320px;background:#111'; box.append(player);
          }
          if(f.audio) {
            box.append(make('p','单独回放 WAV（自动暂停其他回放）'));
            const audio=make('audio'); audio.controls=true; audio.preload='metadata'; audio.src=url(r.id,f.audio); box.append(audio);
          }
          box.append(make('p',f.audio_levels?.signal_detected?'检测到声音，请回放核对内容、首尾和同步。':'未检测到明显声音，请检查音频是否缺失或静音。'));
          for(const [key,label] of [['playback','下载回放视频'],['audio','下载 WAV'],['source','下载采集母版']]) {
            if(f[key]) { const a=make('a',label); a.href=url(r.id,f[key]); a.download=f[key]; a.style.marginRight='16px'; box.append(a); }
          }
        }
        const report=make('a','诊断报告'); report.href=url(r.id,'result.json'); report.target='_blank'; box.append(report);
        if(r.timing_log) {
          const timing=make('a','时间戳记录'); timing.href=url(r.id,r.timing_log); timing.download=r.timing_log;
          timing.style.marginLeft='16px'; box.append(timing);
        }
        node('record-results').append(box);
      }
    } catch(e) { node('record-state').textContent='采集状态暂不可用'; }
    finally { polling=false; }
  }
  refresh(); setInterval(refresh,1000);
})();
