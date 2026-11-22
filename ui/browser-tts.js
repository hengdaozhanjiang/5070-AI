/* Browser speech synthesis (Web Speech API).
 *
 * Why this exists next to IndexTTS: the GPU can only hold one model at a time, so
 * "read this reply out loud" would otherwise force you to unload whatever is
 * running (a chat model, SD) just to hear a sentence. The browser's own voices
 * run on the CPU with zero setup and zero VRAM, which makes them the better
 * default for casual listening. IndexTTS stays for when you actually want your
 * cloned voice.
 *
 * Nothing here touches the hub: everything is local to the renderer.
 */
(function () {
  const synth = window.speechSynthesis;
  const available = !!synth && typeof SpeechSynthesisUtterance !== 'undefined';

  let voices = [];
  let ready = false;

  /* Voices load asynchronously in Chromium; the first call often returns [] and
     fires voiceschanged later. */
  function loadVoices() {
    if (!available) return [];
    const all = synth.getVoices() || [];
    if (!all.length) return voices;
    voices = all;
    ready = true;
    return voices;
  }

  if (available) {
    loadVoices();
    synth.addEventListener?.('voiceschanged', loadVoices);
    // older Chromium only exposes the event through the property
    synth.onvoiceschanged = loadVoices;
    setTimeout(loadVoices, 300);
    setTimeout(loadVoices, 1200);
  }

  function list() {
    if (!ready) loadVoices();
    return voices.map(v => ({
      name: v.name,
      lang: v.lang,
      local: !!v.localService,
      default: !!v.default,
    }));
  }

  /* Prefer an explicit choice, then Simplified Chinese, then the platform
     default. Traditional Chinese is a last resort: reading Simplified text with
     a zh-TW voice sounds wrong even though the language code matches. */
  function pickVoice(name) {
    if (!ready) loadVoices();
    if (name) {
      const hit = voices.find(v => v.name === name);
      if (hit) return hit;
    }
    const score = v => {
      let s = 0;
      if (/^zh[-_]?(CN|Hans|SG)/i.test(v.lang)) s += 100;
      else if (/^zh/i.test(v.lang)) s += 40;
      if (/natural/i.test(v.name)) s += 30;
      if (v.default) s += 10;
      if (v.localService) s += 5;
      return s;
    };
    const ranked = [...voices].sort((a, b) => score(b) - score(a));
    return ranked[0] || null;
  }

  const CHUNK = 240;   // Chromium truncates long utterances, so split on punctuation

  function split(text) {
    const out = [];
    let buf = '';
    const parts = String(text || '').split(/(?<=[。！？；.!?;\n])/);
    for (const p of parts) {
      if ((buf + p).length > CHUNK && buf) {
        out.push(buf);
        buf = p;
      } else {
        buf += p;
      }
    }
    if (buf.trim()) out.push(buf);
    return out.filter(s => s.trim());
  }

  let speaking = false;
  let stopped = false;

  /* Speak `text`; resolves when finished. `el` gets a live 停止 toggle. */
  function speak(text, opts, onDone) {
    if (!available) {
      if (onDone) onDone(false);
      return null;
    }
    stop();
    stopped = false;
    speaking = true;
    const pieces = split(text);
    const voice = pickVoice(opts && opts.voice);
    const rate = Math.max(0.5, Math.min(2, (opts && opts.rate) || 1));
    const pitch = Math.max(0, Math.min(2, (opts && opts.pitch) || 1));
    let index = 0;

    const finish = ok => {
      speaking = false;
      if (onDone) onDone(ok);
    };

    const next = () => {
      if (stopped) { finish(false); return; }
      if (index >= pieces.length) { finish(true); return; }
      const u = new SpeechSynthesisUtterance(pieces[index++]);
      if (voice) {
        u.voice = voice;
        u.lang = voice.lang;
      } else {
        u.lang = 'zh-CN';
      }
      u.rate = rate;
      u.pitch = pitch;
      u.onend = next;
      u.onerror = e => {
        // "interrupted" fires when we cancel on purpose; treat as a clean stop
        if (e && e.error && e.error !== 'interrupted' && e.error !== 'canceled') {
          finish(false);
        } else {
          finish(false);
        }
      };
      synth.speak(u);
    };

    next();

    return {
      stop() {
        stopped = true;
        try { synth.cancel(); } catch { /* ignore */ }
        speaking = false;
      },
      get speaking() { return speaking; },
    };
  }

  function stop() {
    stopped = true;
    if (available) {
      try { synth.cancel(); } catch { /* ignore */ }
    }
    speaking = false;
  }

  window.BrowserTTS = {
    available,
    list,
    speak,
    stop,
    get speaking() { return speaking; },
    get ready() { return ready; },
  };
})();
